#!/usr/bin/env python3
"""Stop API/worker, capture PostgreSQL+artifacts, verify, resume prior services.

Run only during an announced maintenance window. On failure services stay stopped.
Never writes to the existing Open WebUI backup or copies its database.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from verify_backup import sha256, verify

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ARCHIVE = """import sys, tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|', dereference=False) as archive:
    archive.add('/var/lib/quality/artifacts', arcname='artifacts')
"""


def read_epoch(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("QUALITY_RECOVERY_EPOCH="):
            epoch = line.partition("=")[2].strip().strip("\"'")
            if epoch:
                return epoch
    raise ValueError("QUALITY_RECOVERY_EPOCH must be explicitly set in the private env file")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory, never an existing backup")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--project", default="quality-agent")
    args = parser.parse_args()
    output = args.output.absolute()
    env_file = args.env_file.resolve(strict=True)
    epoch = read_epoch(env_file)
    compose = ["docker", "compose", "--project-name", args.project,
               "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml")]
    lock_path = ROOT / ".quality-backup.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock_path, flags, 0o600), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, "Another backup is already running.\n")
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        running = subprocess.check_output(
            compose + ["ps", "--status", "running", "--services"], text=True).splitlines()
        resume = [service for service in ("quality-api", "quality-worker") if service in running]
        try:
            print("Stopping quality API and worker for a consistent backup.", flush=True)
            subprocess.run(compose + ["stop", "quality-api", "quality-worker"], check=True)
            commands = {
                "database.dump": compose + ["exec", "-T", "db", "pg_dump", "-U", "quality",
                                               "-d", "quality", "--format=custom", "--no-owner", "--no-acl"],
                "artifacts.tar": compose + ["run", "--rm", "--no-deps", "-T", "--pull", "never",
                                               "--entrypoint", "python", "quality-api", "-c", ARTIFACT_ARCHIVE],
            }
            files = {}
            for filename, command in commands.items():
                path = output / filename
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                    subprocess.run(command, stdout=handle, check=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                files[filename] = {"sha256": sha256(path), "size_bytes": path.stat().st_size}
            images = subprocess.check_output(compose + ["config", "--images"], text=True).splitlines()
            manifest = {
                "format": "quality-agent-offline-backup-v1",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "recovery_epoch": epoch,
                "compose_project": args.project,
                "image_references": sorted(set(images)),
                "files": files,
            }
            manifest_path = output / "manifest.json"
            with os.fdopen(os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            verify(output)
            if resume:
                subprocess.run(compose + ["start", *resume], check=True)
            print(f"Verified offline backup saved to {output}.")
        except Exception:
            print("Backup failed. API/worker remain stopped; inspect before manually starting them. "
                  "A directory without a verified manifest is incomplete.", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
