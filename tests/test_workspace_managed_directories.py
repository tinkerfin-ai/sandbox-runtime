"""Verify publication preserves local files and seals writers until settlement."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace-runtime"))

import managed
from managed import ManagedDirectories, ManagedDirectoryError
from registry import ProjectRegistry, SessionOwner


class ManagedDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="managed-directory-")
        self.addCleanup(temporary.cleanup)
        self.registry = ProjectRegistry(
            Path(temporary.name) / "state",
            uid=os.getuid(),
            gid=os.getgid(),
            session_namespace=str(uuid4()),
        )
        self.project = hashlib.sha256(b"project").hexdigest()
        self.record = self.registry.prepare(self.project)
        self.managed = ManagedDirectories(self.registry)
        self.root = (
            self.registry.project_path(self.project, self.record.incarnation) / "files"
        )

    def begin(self, files: dict[str, bytes], path: str = "/skills") -> str:
        operation = str(uuid4())
        desired = {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        }
        record = self.managed.begin(self.project, operation, path, desired)
        if record["status"] == "prepared":
            upload = Path(str(record["stage"])) / "upload"
            for name, content in files.items():
                target = upload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        return operation

    def publish(self, files: dict[str, bytes], path: str = "/skills") -> None:
        operation = self.begin(files, path)
        try:
            self.managed.commit(self.project, operation)
        finally:
            self.managed.end(self.project, operation)

    def test_initial_publication_creates_writable_parents(self) -> None:
        self.publish({"SKILL.md": b"content"}, "/skills/example")
        self.assertEqual(
            (self.root / "skills/example/SKILL.md").read_bytes(), b"content"
        )
        self.assertEqual((self.root / "skills").stat().st_uid, self.registry.uid)

    def test_identical_declaration_preserves_local_edits_during_execution(self) -> None:
        files = {"example/SKILL.md": b"installed"}
        self.publish(files)
        (self.root / "skills/example/SKILL.md").write_bytes(b"local")
        owner = SessionOwner(str(uuid4()), self.registry.session_namespace)
        self.registry.reserve(self.project, self.record.incarnation, owner)
        desired = {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        }
        result = self.managed.begin(self.project, str(uuid4()), "/skills", desired)
        self.assertEqual(result, {"status": "unchanged"})
        with self.assertRaises(ManagedDirectoryError) as failure:
            self.begin({"example/SKILL.md": b"changed"})
        self.assertEqual(failure.exception.reason, "busy")
        self.assertEqual((self.root / "skills/example/SKILL.md").read_bytes(), b"local")

    def test_changed_managed_content_conflicts_without_losing_local_files(self) -> None:
        self.publish({"example/SKILL.md": b"installed"})
        target = self.root / "skills/example/SKILL.md"
        target.write_bytes(b"local")
        for files in ({"example/SKILL.md": b"update"}, {}):
            operation = self.begin(files)
            with self.assertRaises(ManagedDirectoryError) as failure:
                self.managed.commit(self.project, operation)
            self.assertEqual(failure.exception.reason, "changed")
            self.managed.end(self.project, operation)
            self.assertEqual(target.read_bytes(), b"local")

    @unittest.skipUnless(sys.platform == "linux", "atomic exchange requires Linux")
    def test_updates_and_removals_preserve_unmanaged_files_and_modes(self) -> None:
        self.publish({"example/SKILL.md": b"installed", "example/old": b"obsolete"})
        local = self.root / "skills/notes"
        local.write_bytes(b"keep")
        local.chmod(0o640)
        empty = self.root / "skills/my-empty-directory"
        empty.mkdir(mode=0o750)
        self.publish({"example/SKILL.md": b"updated"})
        self.assertEqual(
            (self.root / "skills/example/SKILL.md").read_bytes(), b"updated"
        )
        self.assertFalse((self.root / "skills/example/old").exists())
        self.assertEqual(local.read_bytes(), b"keep")
        self.assertEqual(local.stat().st_mode & 0o777, 0o640)
        self.assertEqual(empty.stat().st_mode & 0o777, 0o750)
        self.publish({})
        self.assertFalse((self.root / "skills/example").exists())
        self.assertTrue(empty.is_dir())
        self.assertEqual(local.read_bytes(), b"keep")

    def test_file_and_directory_entry_limits_are_independent(self) -> None:
        with (
            patch.object(managed, "MAX_FILES", 2),
            patch.object(managed, "MAX_ENTRIES", 4),
        ):
            self.publish({"one/file.txt": b"one", "two/file.txt": b"two"})
        self.assertEqual(len(managed.tree(self.root / "skills")), 2)

    def test_symbolic_links_never_redirect_publication(self) -> None:
        self.registry._data_directories(self.project, self.record)
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "keep").write_bytes(b"private")
        (self.root / "skills").symlink_to(reports)
        operation = self.begin({"SKILL.md": b"content"}, "/skills/example")
        with self.assertRaises(ManagedDirectoryError):
            self.managed.commit(self.project, operation)
        self.managed.end(self.project, operation)
        self.assertEqual(list(reports.iterdir()), [reports / "keep"])

    def test_cancelled_begin_cannot_acquire_later(self) -> None:
        operation = str(uuid4())
        self.managed.end(self.project, operation)
        with self.assertRaises(ManagedDirectoryError) as failure:
            self.managed.begin(self.project, operation, "/skills", {})
        self.assertEqual(failure.exception.reason, "stale")

    def test_stage_creation_failure_can_be_settled(self) -> None:
        operation = str(uuid4())
        original = Path.mkdir

        def fail_upload(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == "upload":
                raise OSError("disk unavailable")
            original(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_upload), self.assertRaises(OSError):
            self.managed.begin(self.project, operation, "/skills", {})
        self.managed.end(self.project, operation)
        self.assertFalse(self.managed.marker(self.project).exists())
        self.assertFalse(
            (self.registry.root / "directory-staging" / operation).exists()
        )

    def test_end_is_repeatable_after_staging_was_removed(self) -> None:
        operation = self.begin({"example/SKILL.md": b"content"})
        self.managed.commit(self.project, operation)
        original = Path.unlink
        marker = self.managed.marker(self.project)

        def fail_marker(path: Path, *args: object, **kwargs: object) -> None:
            if path == marker:
                raise OSError("interrupted cleanup")
            original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_marker), self.assertRaises(OSError):
            self.managed.end(self.project, operation)
        self.managed.end(self.project, operation)
        self.assertFalse(marker.exists())

    def test_end_cannot_unseal_an_in_progress_commit(self) -> None:
        operation = self.begin({"example/SKILL.md": b"content"})
        entered, release = threading.Event(), threading.Event()
        original = managed.tree

        def paused_tree(path: Path) -> dict[str, str]:
            entered.set()
            release.wait()
            return original(path)

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(managed, "tree", paused_tree),
        ):
            publishing = executor.submit(self.managed.commit, self.project, operation)
            entered.wait()
            try:
                with self.assertRaises(ManagedDirectoryError) as failure:
                    self.managed.end(self.project, operation)
                self.assertEqual(failure.exception.reason, "busy")
                owner = SessionOwner(str(uuid4()), self.registry.session_namespace)
                with self.assertRaises(ManagedDirectoryError):
                    self.registry.reserve(self.project, self.record.incarnation, owner)
            finally:
                release.set()
            publishing.result()
        self.managed.end(self.project, operation)
        self.assertFalse(self.managed.marker(self.project).exists())

    @unittest.skipUnless(sys.platform == "linux", "atomic exchange requires Linux")
    def test_lost_publication_response_is_settled_before_new_admission(self) -> None:
        self.publish({"example/SKILL.md": b"original"})
        operation = self.begin({"example/SKILL.md": b"update"})
        original = managed.write_record
        baseline = self.managed.baseline(self.project, "/skills")

        def fail_baseline(path: Path, value: object) -> None:
            if path == baseline:
                raise OSError("interrupted baseline write")
            original(path, value)

        with (
            patch.object(managed, "write_record", fail_baseline),
            self.assertRaises(OSError),
        ):
            self.managed.commit(self.project, operation)
        self.managed.end(self.project, operation)
        self.assertEqual(
            (self.root / "skills/example/SKILL.md").read_bytes(), b"update"
        )
        desired = {"example/SKILL.md": hashlib.sha256(b"update").hexdigest()}
        self.assertEqual(
            self.managed.begin(self.project, str(uuid4()), "/skills", desired),
            {"status": "unchanged"},
        )


if __name__ == "__main__":
    unittest.main()
