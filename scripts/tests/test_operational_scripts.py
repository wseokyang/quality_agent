"""Offline tests; these do not claim to test Docker, PostgreSQL or CDEP."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from generate_env import generate
from verify_backup import sha256, verify
import backup


class EnvironmentTests(unittest.TestCase):
    def test_independent_private_secrets_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".env"
            capture = io.StringIO()
            with redirect_stdout(capture):
                generate(destination)
            values = dict(line.split("=", 1) for line in destination.read_text().splitlines()
                          if line and not line.startswith("#"))
            keys = [value for key, value in values.items() if key.endswith("_SECRET")]
            self.assertEqual(4, len(keys))
            self.assertEqual(4, len(set(keys)))
            self.assertTrue(all(len(key) >= 32 for key in keys))
            self.assertTrue(all(key not in capture.getvalue() for key in keys))
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
            before = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                generate(destination)
            self.assertEqual(before, destination.read_bytes())

    def test_reject_symlink_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            original.write_text("preserve")
            link = Path(directory) / ".env"
            link.symlink_to(original)
            with self.assertRaises(FileExistsError):
                generate(link)
            self.assertEqual("preserve", original.read_text())


class BackupIntegrityTests(unittest.TestCase):
    def make_backup(self, directory, member="artifacts/report.md", symlink=False):
        directory = Path(directory)
        (directory / "database.dump").write_bytes(b"example dump transport")
        with tarfile.open(directory / "artifacts.tar", "w:") as archive:
            entry = tarfile.TarInfo(member)
            if symlink:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                archive.addfile(entry)
            else:
                entry.size = 6
                archive.addfile(entry, io.BytesIO(b"report"))
        manifest = {"format": "quality-agent-offline-backup-v1", "created_utc": "test",
                    "files": {name: {"sha256": sha256(directory / name),
                                     "size_bytes": (directory / name).stat().st_size}
                              for name in ("database.dump", "artifacts.tar")}}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return directory

    def test_accept_complete_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual("test", verify(self.make_backup(directory))["created_utc"])

    def test_reject_tampered_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_backup(directory)
            (path / "database.dump").write_bytes(b"corruption")
            with self.assertRaisesRegex(ValueError, "Integrity check failed"):
                verify(path)

    def test_reject_traversal_and_links_even_with_matching_checksum(self):
        for member, symlink in [("artifacts/../../etc/passwd", False),
                                ("/etc/passwd", False), ("artifacts/link", True)]:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, "Unsafe path"):
                    verify(self.make_backup(directory, member, symlink))


class BackupWorkflowTests(unittest.TestCase):
    def execute(self, directory, fail_dump=False):
        directory = Path(directory)
        environment = directory / ".env"
        environment.write_text("QUALITY_RECOVERY_EPOCH=test-epoch\n")
        output = directory / "backup"
        commands = []

        def check_output(command, **kwargs):
            if "ps" in command:
                return "db\nquality-api\n"  # Worker was intentionally not running.
            return "quality-agent:test\npostgres:16\n"

        def run(command, **kwargs):
            commands.append(command)
            if "pg_dump" in command:
                if fail_dump:
                    raise subprocess.CalledProcessError(1, command)
                kwargs["stdout"].write(b"dump")
            elif backup.ARTIFACT_ARCHIVE in command:
                with tarfile.open(fileobj=kwargs["stdout"], mode="w:") as archive:
                    entry = tarfile.TarInfo("artifacts")
                    entry.type = tarfile.DIRTYPE
                    archive.addfile(entry)
            return subprocess.CompletedProcess(command, 0)

        argv = ["backup.py", "--env-file", str(environment), "--output", str(output)]
        with patch.object(backup, "ROOT", directory), patch.object(sys, "argv", argv), \
                patch.object(backup.subprocess, "check_output", side_effect=check_output), \
                patch.object(backup.subprocess, "run", side_effect=run), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if fail_dump:
                with self.assertRaises(subprocess.CalledProcessError):
                    backup.main()
            else:
                backup.main()
        return commands, output

    def test_resume_only_previously_running_service_after_verified_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, output = self.execute(directory)
            verify(output)
            start = [command for command in commands if "start" in command]
            self.assertEqual(1, len(start))
            self.assertEqual(["start", "quality-api"], start[0][-2:])

    def test_dump_failure_does_not_resume_or_publish_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, output = self.execute(directory, fail_dump=True)
            self.assertFalse(any("start" in command for command in commands))
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
