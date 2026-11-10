#!/usr/bin/env python3
"""Verify a complete offline backup before operator-controlled recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Manifest must not be a symbolic link")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "quality-agent-offline-backup-v1":
        raise ValueError("Unknown or incomplete backup format")
    files = manifest.get("files", {})
    if set(files) != {"database.dump", "artifacts.tar"}:
        raise ValueError("Backup must contain exactly the database and artifact manifest entries")
    for filename, metadata in files.items():
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Invalid backup file: {filename}")
        if path.stat().st_size != metadata["size_bytes"] or sha256(path) != metadata["sha256"]:
            raise ValueError(f"Integrity check failed: {filename}")
    seen = set()
    with tarfile.open(directory / "artifacts.tar", "r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "artifacts" or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe path, link, or special file in artifact archive")
            if member.name in seen:
                raise ValueError("Duplicate artifact entry")
            seen.add(member.name)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        manifest = verify(args.directory)
    except (ValueError, OSError, KeyError, tarfile.TarError) as exc:
        parser.exit(2, f"Backup verification failed: {exc}\n")
    print(f"Verified backup from {manifest['created_utc']}; no data was restored.")


if __name__ == "__main__":
    main()
