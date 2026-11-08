#!/usr/bin/env python3
"""Archive extraction regressions using intentionally malformed ZIP inputs."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import warnings
import zipfile

SPEC = importlib.util.spec_from_file_location("package_extract", Path(__file__).with_name("extract.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def zip_fixture(destination, entries, mutate=None, duplicate=None):
    records = {}
    for name, payload, kind, mode in entries:
        record = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                  "kind": kind, "mode": oct(mode)}
        if kind == "symlink":
            record["target"] = payload.decode()
        records[name] = record
    with zipfile.ZipFile(destination, "w") as archive:
        for name, payload, kind, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = ((stat.S_IFLNK if kind == "symlink" else stat.S_IFREG) | mode) << 16
            archive.writestr(info, mutate(payload) if mutate and name == "index.js" else payload)
            if duplicate == name:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    archive.writestr(info, payload)
        manifest = zipfile.ZipInfo("package-manifest.json")
        manifest.create_system = 3
        manifest.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(manifest, json.dumps({"schema": 1, "architecture": "x86_64", "files": records}))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "input.zip"
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def rejected(self, entries, pattern, **options):
        zip_fixture(self.archive, entries, **options)
        with self.assertRaisesRegex(ValueError, pattern):
            MODULE.extract(self.archive, self.output)
        self.assertEqual(list(self.output.iterdir()), [], "Invalid ZIP wrote partial files")

    def test_restores_executable_and_certificate_link_without_dereference(self):
        zip_fixture(self.archive, [
            ("bin/gateway", b"executable fixture", "file", 0o755),
            ("index.js", b"entry fixture", "file", 0o644),
            ("runtime/bref/ssl/cert.pem", b"/etc/ssl/cert.pem", "symlink", 0o777),
        ])
        MODULE.extract(self.archive, self.output)
        self.assertEqual((self.output / "bin/gateway").stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(self.output / "runtime/bref/ssl/cert.pem"), "/etc/ssl/cert.pem")

    def test_rejects_corrupt_payload_before_writing(self):
        self.rejected([("index.js", b"original", "file", 0o644)], "Checksum/type mismatch", mutate=lambda data: data + b"changed")

    def test_rejects_path_traversal_before_writing(self):
        self.rejected([("../escaped", b"bad", "file", 0o644)], "Unsafe archive path")
        self.assertFalse((self.root / "escaped").exists())

    def test_rejects_duplicate_member(self):
        self.rejected([("index.js", b"original", "file", 0o644)], "Duplicate archive entries", duplicate="index.js")

    def test_rejects_linked_parent(self):
        self.rejected([
            ("other/normal", b"safe", "file", 0o644),
            ("runtime/link", b"../other", "symlink", 0o777),
            ("runtime/link/injected", b"bad", "file", 0o644),
        ], "file or symlink parent")

    def test_rejects_other_external_link_at_certificate_path(self):
        self.rejected([("runtime/bref/ssl/cert.pem", b"/etc/passwd", "symlink", 0o777)], "Symlink escapes archive")

    def test_rejects_dangling_internal_link(self):
        self.rejected([("runtime/link", b"missing", "symlink", 0o777)], "Dangling archive symlink")

    def test_rejects_special_permission_bits(self):
        self.rejected([("bin/gateway", b"bad", "file", 0o4755)], "Unsupported archive mode")


class EntryTests(unittest.TestCase):
    def check_entry(self, module_type):
        source = Path(__file__).resolve().parents[1] / "runtime/launcher"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "launcher").mkdir()
            (root / "index.js").write_bytes((source / "web-entry.js").read_bytes())
            (root / "launcher/entry.cjs").write_bytes((source / "web-entry.cjs").read_bytes())
            (root / "package.json").write_text(json.dumps({"type": module_type}))
            (root / "runtime-config.json").write_text(json.dumps({"front_controller": "web/front.php", "document_root": "web"}))
            (root / "launcher/index.cjs").write_text(
                "exports.main=async function(options){console.log(JSON.stringify({"
                "initializeRust:options.initializeRust,root:process.env.LAMBDA_TASK_ROOT,"
                "script:process.env.BREF_WEB_SCRIPT_FILENAME,documentRoot:process.env.BREF_WEB_DOCUMENT_ROOT}));};"
            )
            environment = dict(os.environ)
            environment.pop("BREF_WEB_SCRIPT_FILENAME", None)
            environment.pop("BREF_WEB_DOCUMENT_ROOT", None)
            result = subprocess.run(["node", str(root / "index.js")], env=environment,
                                    capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(json.loads(result.stdout), {
                "initializeRust": True, "root": str(root),
                "script": str(root / "web/front.php"), "documentRoot": str(root / "web"),
            })

    def test_node_first_entry_with_commonjs_application_metadata(self):
        self.check_entry("commonjs")

    def test_node_first_entry_with_es_module_application_metadata(self):
        self.check_entry("module")


if __name__ == "__main__":
    unittest.main(verbosity=2)
