#!/usr/bin/env python3
"""Build a deterministic Node-first ZIP from a Bref /opt export and Rust binary.

This builder validates archive structure and ELF machine types. It does not run
the artifact, test native library resolution, or contact AWS.
"""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import struct
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MACHINES = {"x86_64": 62, "aarch64": 183}
MAX_BYTES = 512 * 1024 * 1024
SYSTEM_CERT_LINK = ("runtime/bref/ssl/cert.pem", "/etc/ssl/cert.pem")
IGNORED_DIRS = {".git", ".hg", ".svn", ".aws", ".ssh", ".idea", ".vscode",
                ".github", "node_modules", "tests", "test", "target", "build",
                "results", "__pycache__"}
SECRET_NAMES = {".env", ".npmrc", ".netrc", ".pypirc", "auth.json",
                "composer-auth.json", "id_rsa", "id_ed25519"}


def relative_path(value, allow_dot=False):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not path.parts:
        if allow_dot and value == ".":
            return "."
        raise ValueError(f"Expected a safe relative package path: {value}")
    return str(path)


def elf_machine(data, name, architecture):
    if data[:4] != b"\x7fELF":
        return None
    if len(data) < 20 or data[4] != 2 or data[5] != 1:
        raise ValueError(f"Unsupported or truncated 64-bit ELF: {name}")
    machine = struct.unpack(("<" if data[5] == 1 else ">") + "H", data[18:20])[0]
    if machine != MACHINES[architecture]:
        raise ValueError(f"ELF architecture mismatch for {name}: e_machine={machine}, expected {MACHINES[architecture]}")
    return machine


class Archive:
    def __init__(self, architecture):
        self.architecture = architecture
        self.entries = {}

    def add(self, name, data, mode=0o644, kind="file", source=None):
        name = relative_path(name)
        if name in self.entries:
            raise ValueError(f"Package path collision: {name}")
        entry = {"data": data, "mode": mode, "kind": kind}
        if kind == "file":
            machine = elf_machine(data, name, self.architecture)
            if machine is not None:
                entry["elf_machine"] = machine
        if source is not None:
            entry["source"] = str(source)
        self.entries[name] = entry

    def source(self, source, name, source_root=None):
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(source)
            if (name, target) != SYSTEM_CERT_LINK:
                if target.startswith("/opt/") and name.startswith("runtime/"):
                    target = posixpath.relpath("runtime/" + target[len("/opt/"):], posixpath.dirname(name))
                elif target.startswith("/"):
                    raise ValueError(f"External absolute symlink is not allowed: {name} -> {target}")
                if source_root is not None:
                    resolved = (source.parent / os.readlink(source)).resolve()
                    if not resolved.is_relative_to(source_root.resolve()):
                        raise ValueError(f"Application symlink escapes its staging directory: {name}")
                resolved_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if resolved_name == ".." or resolved_name.startswith("../") or target.startswith("/"):
                    raise ValueError(f"Symlink escapes archive: {name} -> {target}")
            self.add(name, target.encode(), 0o777, "symlink")
        elif stat.S_ISREG(info.st_mode):
            self.add(name, source.read_bytes(), 0o755 if info.st_mode & 0o111 else 0o644)
        else:
            raise ValueError(f"Special files cannot be packaged: {source}")

    def tree(self, source, prefix="", app=False, excluded=()):
        source = source.resolve()
        for base, directories, files in os.walk(source, followlinks=False):
            base = Path(base)
            for name in list(directories):
                item = base / name
                relative = item.relative_to(source).as_posix()
                if app and excluded_app(relative, True, excluded):
                    directories.remove(name)
                    continue
                if item.is_symlink():
                    directories.remove(name)
                    self.source(item, posixpath.join(prefix, relative), source if app else None)
            for name in files:
                item = base / name
                relative = item.relative_to(source).as_posix()
                if app and excluded_app(relative, False, excluded):
                    continue
                if not app and relative in {"bootstrap", "bref/bootstrap.php"}:
                    continue
                self.source(item, posixpath.join(prefix, relative), source if app else None)

    def validate(self):
        for name, entry in self.entries.items():
            if any(str(parent) in self.entries for parent in PurePosixPath(name).parents):
                raise ValueError(f"Archive entry has a file or symlink parent: {name}")
            if entry["kind"] != "symlink":
                continue
            target = entry["data"].decode()
            if (name, target) == SYSTEM_CERT_LINK:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if resolved not in self.entries and not any(item.startswith(resolved + "/") for item in self.entries):
                raise ValueError(f"Dangling archive symlink: {name} -> {target}")
        total = sum(len(entry["data"]) for entry in self.entries.values())
        if total > MAX_BYTES:
            raise ValueError(f"Archive contents exceed the 512 MiB Web Function package limit: {total} bytes")

    def manifest(self, definition):
        files = {}
        for name, entry in sorted(self.entries.items()):
            data = entry["data"]
            record = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                      "kind": entry["kind"], "mode": oct(entry["mode"])}
            if "elf_machine" in entry:
                record["elf_machine"] = entry["elf_machine"]
            if entry["kind"] == "symlink":
                record["target"] = data.decode()
                if (name, record["target"]) == SYSTEM_CERT_LINK:
                    record["external_system_certificate"] = True
            files[name] = record
        return {"schema": 1, "architecture": self.architecture,
                "managed_runtime": "nodejs24.x", "managed_node_included": False,
                "deployment_target": "lambda-web" if self.architecture == "aarch64" else "local-test-only",
                "validation": {"elf_machine": "checked", "native_execution": "not performed by builder",
                               "aws_execution": "not performed"},
                "configuration": definition, "files": files}

    def write(self, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, entry in sorted(self.entries.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    file_type = stat.S_IFLNK if entry["kind"] == "symlink" else stat.S_IFREG
                    info.external_attr = (file_type | entry["mode"]) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, entry["data"], compresslevel=9)
            temporary.replace(destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def excluded_app(relative, directory, patterns):
    path = PurePosixPath(relative)
    if directory and path.name in IGNORED_DIRS:
        return True
    if not directory and (path.name in SECRET_NAMES or path.name.startswith(".env.") or path.name.endswith(".log")):
        return True
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path, help="Extracted Bref image /opt contents")
    parser.add_argument("--rust-binary", required=True, type=Path)
    parser.add_argument("--architecture", choices=MACHINES, required=True)
    parser.add_argument("--app-dir", type=Path, help="Explicit production application staging directory; defaults to safe smoke app")
    parser.add_argument("--front-controller", default="public/index.php")
    parser.add_argument("--document-root", default="public")
    parser.add_argument("--exclude", action="append", default=[], help="Additional app-relative glob to omit")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        controller = relative_path(args.front_controller)
        document_root = relative_path(args.document_root, allow_dot=True)
        runtime = args.runtime_dir.resolve()
        for required in ("bin/php", "bin/php-fpm", "bref/etc/php/conf.d/bref.ini"):
            if not (runtime / required).is_file():
                raise ValueError(f"Missing required Bref runtime file: {required}")
        for required in ("bin/php", "bin/php-fpm"):
            if elf_machine((runtime / required).read_bytes(), required, args.architecture) is None:
                raise ValueError(f"Bref runtime binary must be a Linux 64-bit ELF executable: {required}")
        rust = args.rust_binary.read_bytes()
        if elf_machine(rust, "bref-web-gateway", args.architecture) is None:
            raise ValueError("Rust gateway must be a Linux 64-bit ELF executable")
        archive = Archive(args.architecture)
        app = args.app_dir.resolve() if args.app_dir else ROOT / "examples/minimal"
        if not app.is_dir():
            raise ValueError(f"Application staging directory does not exist: {app}")
        archive.tree(app, app=True, excluded=args.exclude)
        if controller not in archive.entries or archive.entries[controller]["kind"] != "file":
            raise ValueError(f"Application front controller is missing or excluded: {controller}")
        if not any(item.startswith(document_root + "/") for item in archive.entries) and document_root != ".":
            raise ValueError(f"Document root is missing: {document_root}")
        archive.tree(runtime, "runtime")
        archive.add("bin/bref-web-gateway", rust, 0o755)
        archive.source(ROOT / "runtime/launcher/web-entry.js", "index.js")
        archive.source(ROOT / "runtime/launcher/web-entry.cjs", "launcher/entry.cjs")
        archive.source(ROOT / "runtime/launcher/index.js", "launcher/index.cjs")
        archive.source(ROOT / "runtime/launcher/config.cjs", "launcher/config.cjs")
        for name in ("entrypoint.sh", "configure.php", "init.php"):
            archive.source(ROOT / "runtime/php" / name, "compat/" + name)
        definition = {"front_controller": controller, "document_root": document_root}
        archive.add("runtime-config.json", (json.dumps(definition, sort_keys=True, indent=2) + "\n").encode())
        archive.validate()
        manifest = archive.manifest(definition)
        archive.add("package-manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
        archive.validate()
        archive.write(args.output)
        checksum = hashlib.sha256(args.output.read_bytes()).hexdigest()
        args.output.with_suffix(args.output.suffix + ".sha256").write_text(f"{checksum}  {args.output.name}\n")
        print(json.dumps({"archive": str(args.output.resolve()), "sha256": checksum,
                          "architecture": args.architecture, "files": len(archive.entries),
                          "compressed_bytes": args.output.stat().st_size,
                          "uncompressed_bytes": sum(len(entry["data"]) for entry in archive.entries.values()),
                          "elf_files": sum("elf_machine" in entry for entry in archive.entries.values()),
                          "validation": manifest["validation"]}, indent=2))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Package build failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
