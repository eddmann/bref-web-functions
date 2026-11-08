#!/usr/bin/env python3
"""Validate and extract this project's ZIP, preserving executable modes/symlinks.

The destination must be empty. No external symlink is accepted except the
explicit system CA bundle symlink included by the builder.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import sys
import zipfile

SYSTEM_CERT_LINK = ("runtime/bref/ssl/cert.pem", "/etc/ssl/cert.pem")


def extract(archive_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Extraction destination must be empty")
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive entries")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts:
                raise ValueError(f"Unsafe archive path: {name}")
        manifest = json.loads(archive.read("package-manifest.json"))
        if manifest.get("schema") != 1 or set(names) != set(manifest["files"]) | {"package-manifest.json"}:
            raise ValueError("Archive contents do not match the manifest")
        contents = {}
        symlinks = set()
        for member in members:
            name = member.filename
            data = archive.read(member)
            mode = member.external_attr >> 16
            kind = "symlink" if stat.S_ISLNK(mode) else "file"
            if not stat.S_ISREG(mode) and kind != "symlink":
                raise ValueError(f"Unsupported archive file type: {name}")
            allowed_modes = {0o777} if kind == "symlink" else {0o644, 0o755}
            if stat.S_IMODE(mode) not in allowed_modes:
                raise ValueError(f"Unsupported archive mode: {name}")
            if name != "package-manifest.json":
                record = manifest["files"][name]
                if record["kind"] != kind or record["bytes"] != len(data) or record["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ValueError(f"Checksum/type mismatch: {name}")
                if int(record["mode"], 8) != stat.S_IMODE(mode):
                    raise ValueError(f"Mode mismatch: {name}")
            if kind == "symlink":
                target = data.decode()
                if manifest["files"][name].get("target") != target:
                    raise ValueError(f"Symlink target differs from manifest: {name}")
                if (name, target) != SYSTEM_CERT_LINK:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                        raise ValueError(f"Symlink escapes archive: {name}")
                    if resolved not in names and not any(item.startswith(resolved + "/") for item in names):
                        raise ValueError(f"Dangling archive symlink: {name}")
                symlinks.add(name)
            contents[name] = (data, mode, kind)
        # Prevent archive members from writing through any symlinked parent.
        for name in names:
            if any(str(parent) in contents for parent in PurePosixPath(name).parents):
                raise ValueError(f"Archive member has a file or symlink parent: {name}")
        for name, (data, mode, kind) in contents.items():
            if kind != "file":
                continue
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(stat.S_IMODE(mode))
        for name in symlinks:
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(contents[name][0].decode(), output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        manifest = extract(args.archive, args.destination)
        print(json.dumps({"destination": str(args.destination.resolve()),
                          "architecture": manifest["architecture"],
                          "verified_files": len(manifest["files"]),
                          "native_execution": "not performed"}, indent=2))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        parser.exit(1, f"Package extraction failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
