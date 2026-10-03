"""Verify trusted project admission, deletion ownership, and filesystem boundaries."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace-runtime"))

import registry as registry_module
from registry import ProjectRecord, ProjectRegistry, RegistryError, SessionOwner

SOURCE = Path(__file__).resolve().parents[1] / "workspace-runtime" / "registry.py"


class RegistryTest(unittest.TestCase):
    """Use private temporary directories without native sessions or Docker."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-registry-test-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.namespace = str(uuid4())
        self.registry = ProjectRegistry(
            self.base / "state",
            uid=os.getuid(),
            gid=os.getgid(),
            session_namespace=self.namespace,
        )
        self.project = sha256(b"project-a").hexdigest()
        self.other = sha256(b"project-b").hexdigest()

    def owner(self) -> SessionOwner:
        return SessionOwner(str(uuid4()), self.namespace)

    def reserve(self, project: str, owner: SessionOwner) -> ProjectRecord:
        prepared = self.registry.prepare(project)
        return self.registry.reserve(project, prepared.incarnation, owner)

    def restarted(self) -> ProjectRegistry:
        return ProjectRegistry(
            self.base / "state",
            uid=os.getuid(),
            gid=os.getgid(),
            session_namespace=str(uuid4()),
        )

    def test_stale_requests_are_rejected_with_or_without_a_project_record(self) -> None:
        owner = self.owner()
        prepared = self.registry.prepare(self.project)
        restarted = self.restarted()
        for project in (self.project, self.other):
            for operation in (restarted.reserve, restarted.session_request):
                with self.subTest(project=project, operation=operation.__name__):
                    with self.assertRaises(RegistryError) as rejected:
                        operation(project, prepared.incarnation, owner)
                    self.assertEqual(rejected.exception.reason, "stale")
        self.assertEqual(list((self.base / "state" / "projects").iterdir()), [])
        self.assertFalse((self.base / "state" / "locks" / self.other).exists())

    def test_invalid_current_namespace_does_not_create_registry(self) -> None:
        root = self.base / "invalid"
        with self.assertRaises(ValueError):
            ProjectRegistry(
                root,
                uid=os.getuid(),
                gid=os.getgid(),
                session_namespace="not-a-namespace",
            )
        self.assertFalse(root.exists())

    def test_retire_sessions_preserves_project_data_and_unowned_temporary_entries(
        self,
    ) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        sibling_owner = self.owner()
        sibling = self.reserve(self.other, sibling_owner)
        root = self.registry.project_path(self.project, record.incarnation)
        retained = [
            root / "files" / "hello.py",
            root / "home" / "saved",
            root / "cache" / "saved",
            root / "dependencies" / "python" / "installed",
            root / "dependencies" / "node" / "installed",
            root / "dependencies" / f".python-{uuid4()}" / "unowned",
            root / "runs" / self.namespace / str(uuid4()) / "unowned",
        ]
        for sentinel in retained:
            sentinel.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            sentinel.write_bytes(b"original-project-data")
        incomplete = root / "dependencies" / f".python-{owner.session_id}"
        incomplete.mkdir(mode=0o700)
        (incomplete / "escape").symlink_to(root / "files", target_is_directory=True)
        run = root / "runs" / self.namespace / owner.session_id
        (run / "private").write_bytes(b"temporary")
        restarted = self.restarted()
        retired = restarted.retire_sessions()
        self.assertEqual(set(retired), {owner, sibling_owner})
        current = restarted.prepare(self.project)
        self.assertEqual(current, record)
        self.assertEqual(restarted.prepare(self.other), sibling)
        self.assertFalse(run.exists())
        self.assertFalse(incomplete.exists())
        for sentinel in retained:
            self.assertEqual(sentinel.read_bytes(), b"original-project-data")
        for registry in (self.registry, restarted):
            for operation in (registry.reserve, registry.session_request):
                with self.assertRaises(RegistryError) as rejected:
                    operation(self.project, record.incarnation, owner)
                self.assertEqual(rejected.exception.reason, "stale")
        self.assertEqual(set(restarted.retire_sessions()), set(retired))
        self.assertEqual(restarted.prepare(self.project), current)
        next_owner = SessionOwner(str(uuid4()), restarted.session_namespace)
        restarted.reserve(self.project, record.incarnation, next_owner)
        restarted.release(self.project, record.incarnation, owner)
        self.assertEqual(restarted.prepare(self.project).sessions, (next_owner,))
        self.assertTrue(
            (
                root / "runs" / next_owner.session_namespace / next_owner.session_id
            ).is_dir()
        )
        self.assertEqual(retained[0].read_bytes(), b"original-project-data")

    def test_retire_sessions_keeps_deletion_sealed_and_preserves_its_identity(
        self,
    ) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        root = self.registry.project_path(self.project, record.incarnation)
        sentinel = root / "files" / "saved"
        sentinel.write_bytes(b"retained")
        restarted = self.restarted()
        self.assertEqual(restarted.retire_sessions(), (owner,))
        current = restarted.begin_delete(self.project)
        self.assertEqual(current, deleting)
        self.assertEqual(sentinel.read_bytes(), b"retained")
        with self.assertRaises(RegistryError) as rejected:
            restarted.prepare(self.project)
        self.assertEqual(rejected.exception.reason, "busy")
        restarted.finish_delete(
            self.project, record.incarnation, deleting.deletion_id, (owner,)
        )
        deleted = restarted.begin_delete(self.project)
        assert deleted is not None
        self.assertEqual(deleted.phase, "deleted")
        self.assertFalse(root.exists())

    def test_retire_sessions_rejects_a_current_namespace_owner(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        current = ProjectRegistry(
            self.base / "state",
            uid=os.getuid(),
            gid=os.getgid(),
            session_namespace=self.namespace,
        )
        with self.assertRaises(RegistryError):
            current.retire_sessions()
        self.assertEqual(self.registry.prepare(self.project), record)
        self.assertFalse(self.registry._is_cancelled(self.project, owner))
        self.assertTrue(
            (
                self.registry.project_path(self.project, record.incarnation)
                / "runs"
                / self.namespace
                / owner.session_id
            ).is_dir()
        )

    def test_retire_sessions_retries_partial_cleanup_with_all_ownership_retained(
        self,
    ) -> None:
        owners = (self.owner(), self.owner())
        self.reserve(self.project, owners[0])
        record = self.reserve(self.project, owners[1])
        restarted = self.restarted()
        remove_session = restarted._remove_session_data

        def fail_second(project: str, incarnation: str, owner: SessionOwner) -> None:
            self.assertTrue(
                all(restarted._is_cancelled(project, item) for item in owners)
            )
            if owner == owners[1]:
                raise PermissionError("controlled cleanup failure")
            remove_session(project, incarnation, owner)

        with (
            patch.object(restarted, "_remove_session_data", fail_second),
            self.assertRaises(PermissionError),
        ):
            restarted.retire_sessions()
        self.assertEqual(self.registry.prepare(self.project), record)
        next_start = self.restarted()
        self.assertEqual(set(next_start.retire_sessions()), set(owners))
        self.assertEqual(next_start.prepare(self.project), record)
        for owner in owners:
            with self.assertRaises(RegistryError):
                self.registry.reserve(self.project, record.incarnation, owner)

    def test_retire_sessions_rejects_capacity_before_cancelling_any_session(
        self,
    ) -> None:
        owners = (self.owner(), self.owner())
        self.reserve(self.project, owners[0])
        record = self.reserve(self.project, owners[1])
        restarted = self.restarted()
        with (
            patch.object(registry_module, "_MAX_RETIRED_SESSIONS", 1),
            self.assertRaises(RegistryError),
        ):
            restarted.retire_sessions()
        self.assertEqual(self.registry.prepare(self.project), record)
        for owner in owners:
            self.assertFalse(restarted._is_cancelled(self.project, owner))

    def test_retire_sessions_deduplicates_confirmed_native_identities(self) -> None:
        owner = self.owner()
        first = self.reserve(self.project, owner)
        second = self.reserve(self.other, owner)
        restarted = self.restarted()
        with patch.object(registry_module, "_MAX_RETIRED_SESSIONS", 1):
            self.assertEqual(restarted.retire_sessions(), (owner,))
        self.assertEqual(restarted.prepare(self.project), first)
        self.assertEqual(restarted.prepare(self.other), second)

    def test_retire_sessions_accepts_reservation_before_directory_creation(
        self,
    ) -> None:
        owner = self.owner()
        prepared = self.registry.prepare(self.project)
        with (
            patch.object(self.registry, "_directories", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            self.registry.reserve(self.project, prepared.incarnation, owner)
        restarted = self.restarted()
        self.assertEqual(restarted.retire_sessions(), (owner,))
        self.assertEqual(restarted.prepare(self.project).sessions, (owner,))
        self.assertFalse((self.base / "state" / "projects" / self.project).exists())

    def test_retire_sessions_rejects_corrupt_records_without_removing_data(
        self,
    ) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        path = self.base / "state" / "records" / self.project
        original = path.read_bytes()
        corrupt = (b"{", b'{"phase":"deleted"}', b"null")
        for content in corrupt:
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises((RegistryError, ValueError)):
                    self.restarted().retire_sessions()
                self.assertEqual(path.read_bytes(), content)
                self.assertTrue(
                    (
                        self.registry.project_path(self.project, record.incarnation)
                        / "runs"
                        / self.namespace
                        / owner.session_id
                    ).is_dir()
                )
                self.assertFalse(self.registry._is_cancelled(self.project, owner))
        path.write_bytes(original)

    def test_retire_sessions_rejects_unknown_record_entries(self) -> None:
        unexpected = self.base / "state" / "records" / "unexpected"
        unexpected.write_bytes(b"unrecognized state")
        unexpected.chmod(0o600)
        with self.assertRaises(RegistryError):
            self.restarted().retire_sessions()
        self.assertEqual(unexpected.read_bytes(), b"unrecognized state")

    def test_retire_sessions_rejects_symlinked_ownership_files(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        self.registry.cancel(self.project, owner)
        outside = self.base / "outside"
        outside.write_bytes(b"untouched")
        original = self.base / "original"
        for path in (
            self.base / "state" / "records" / self.project,
            self.registry._cancellation_path(self.project, owner),
        ):
            with self.subTest(path=path):
                path.rename(original)
                path.symlink_to(outside)
                try:
                    with self.assertRaises((RegistryError, OSError)):
                        self.restarted().retire_sessions()
                finally:
                    path.unlink()
                    original.rename(path)
                self.assertEqual(self.registry.prepare(self.project), record)
                self.assertEqual(outside.read_bytes(), b"untouched")
                self.assertTrue(
                    (
                        self.registry.project_path(self.project, record.incarnation)
                        / "runs"
                        / self.namespace
                        / owner.session_id
                    ).is_dir()
                )

    def test_retire_sessions_does_not_follow_replaced_cleanup_directories(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        root = self.registry.project_path(self.project, record.incarnation)
        run = root / "runs" / self.namespace / owner.session_id
        temporary = root / "dependencies" / f".python-{owner.session_id}"
        temporary.mkdir(mode=0o700)
        sentinel = run / "saved"
        sentinel.write_bytes(b"untouched")
        for path in (
            self.base / "state" / "records",
            self.base / "state" / "cancelled",
            self.base / "state" / "projects",
            root.parent,
            root,
            root / "runs",
            run.parent,
            run,
            root / "dependencies",
        ):
            with self.subTest(path=path):
                restarted = self.restarted()
                saved = path.with_name(f"{path.name}-retained")
                path.rename(saved)
                path.symlink_to(saved, target_is_directory=True)
                try:
                    with self.assertRaises((RegistryError, OSError)):
                        restarted.retire_sessions()
                finally:
                    path.unlink()
                    saved.rename(path)
                self.assertEqual(self.registry.prepare(self.project), record)
                self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_session_cleanup_unlinks_temporary_symlink_without_following_target(
        self,
    ) -> None:
        for operation in ("release", "retire_sessions"):
            with self.subTest(operation=operation):
                owner = self.owner()
                record = self.reserve(self.project, owner)
                root = self.registry.project_path(self.project, record.incarnation)
                sentinel = root / "files" / "saved"
                sentinel.write_bytes(b"retained")
                temporary = root / "dependencies" / f".python-{owner.session_id}"
                temporary.symlink_to(root / "files", target_is_directory=True)
                if operation == "release":
                    self.registry.release(self.project, record.incarnation, owner)
                else:
                    self.assertEqual(self.restarted().retire_sessions(), (owner,))
                self.assertFalse(temporary.is_symlink())
                self.assertEqual(sentinel.read_bytes(), b"retained")

    def test_reservation_is_idempotent_and_shares_only_its_project(self) -> None:
        first_owner, second_owner = self.owner(), self.owner()
        first = self.reserve(self.project, first_owner)
        second = self.reserve(self.project, second_owner)
        repeated = self.reserve(self.project, first_owner)
        unrelated = self.reserve(self.other, self.owner())
        self.assertEqual(first.incarnation, second.incarnation)
        self.assertEqual(second, repeated)
        self.assertNotEqual(first.incarnation, unrelated.incarnation)
        self.assertEqual(set(repeated.sessions), {first_owner, second_owner})

    def test_concurrent_registries_do_not_lose_reservations(self) -> None:
        barrier = threading.Barrier(2)
        owners = (self.owner(), self.owner())

        def reserve(owner: SessionOwner) -> ProjectRecord:
            registry = ProjectRegistry(
                self.base / "state",
                uid=os.getuid(),
                gid=os.getgid(),
                session_namespace=self.namespace,
            )
            prepared = registry.prepare(self.project)
            barrier.wait()
            return registry.reserve(self.project, prepared.incarnation, owner)

        with ThreadPoolExecutor(max_workers=2) as executor:
            records = tuple(executor.map(reserve, owners))
        self.assertEqual(records[0].incarnation, records[1].incarnation)
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None
        self.assertEqual(set(deleting.sessions), set(owners))

    def test_delete_seals_admission_and_keeps_stable_lock(self) -> None:
        owner = self.owner()
        first = self.reserve(self.project, owner)
        lock = self.base / "state" / "locks" / self.project
        lock_inode = lock.stat().st_ino
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        self.assertEqual(deleting, self.registry.begin_delete(self.project))
        with self.assertRaises(RegistryError):
            self.registry.reserve(self.project, first.incarnation, self.owner())
        with self.assertRaises(RegistryError):
            self.registry.finish_delete(
                self.project, first.incarnation, deleting.deletion_id, ()
            )
        self.assertTrue(
            self.registry.project_path(self.project, first.incarnation).is_dir()
        )
        self.registry.release(self.project, first.incarnation, owner)
        self.assertEqual(deleting, self.registry.begin_delete(self.project))
        self.registry.finish_delete(
            self.project, first.incarnation, deleting.deletion_id, (owner,)
        )
        self.assertFalse(
            self.registry.project_path(self.project, first.incarnation).exists()
        )
        self.registry.finish_delete(
            self.project, first.incarnation, deleting.deletion_id, (owner,)
        )
        self.assertEqual(lock.stat().st_ino, lock_inode)

    def test_cancel_before_reserve_prevents_late_directory_creation(self) -> None:
        owner = self.owner()
        prepared = self.registry.prepare(self.project)
        self.assertIsNone(self.registry.cancel(self.project, owner))
        with self.assertRaises(RegistryError):
            self.registry.reserve(self.project, prepared.incarnation, owner)
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None
        self.assertEqual(deleting.sessions, ())
        self.assertFalse((self.base / "state" / "projects" / self.project).exists())

    def test_session_view_selects_only_its_project_and_readonly_toolchain(self) -> None:
        owner = self.owner()
        reservation = self.reserve(self.project, owner)
        other = self.reserve(self.other, self.owner())
        shared = self.base / "toolchain"
        shared.mkdir()
        with tempfile.TemporaryDirectory(prefix="ws-", dir="/tmp") as directory:
            proxy_path = Path(directory) / "proxy"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(proxy_path))
                with (
                    patch.object(registry_module, "_SYSTEM_MOUNTS", (str(shared),)),
                    patch.object(registry_module, "_EGRESS_SOCKET", proxy_path),
                ):
                    request = self.registry.session_request(
                        self.project, reservation.incarnation, owner
                    )
        project_root = self.registry.project_path(self.project, reservation.incarnation)
        sibling_root = self.registry.project_path(self.other, other.incarnation)
        self.assertEqual(
            request["workspace"], {"path": str(project_root / "files"), "mode": "rw"}
        )
        self.assertFalse(request["share_net"])
        self.assertEqual(request["uid_mode"], "setpriv")
        self.assertEqual(request["env_passthrough"], {"mode": "allow", "keys": []})
        mounts = request["binds"]
        assert isinstance(mounts, list)
        for mount in mounts:
            assert isinstance(mount, dict)
            source = Path(mount["source"])
            self.assertFalse(source.is_relative_to(sibling_root))
            self.assertNotIn(mount["dest"], ("/dev", "/dev/null", "/dev/shm", "/proc"))
            if not source.is_relative_to(project_root):
                self.assertIn(source, (shared, proxy_path))
                self.assertTrue(mount["readonly"])
        self.registry.cancel(self.project, owner)
        with self.assertRaises(RegistryError):
            self.registry.session_request(self.project, reservation.incarnation, owner)

    def test_session_view_rejects_wrong_incarnation_or_unreserved_owner(self) -> None:
        owner = self.owner()
        reservation = self.reserve(self.project, owner)
        for incarnation, requested_owner in (
            (str(uuid4()), owner),
            (reservation.incarnation, self.owner()),
        ):
            with self.assertRaises(RegistryError):
                self.registry.session_request(
                    self.project, incarnation, requested_owner
                )

    def test_cancel_locates_lost_reserve_response_and_does_not_claim_cleanup(
        self,
    ) -> None:
        owner = self.owner()
        reservation = self.reserve(self.project, owner)
        found = self.registry.cancel(self.project, owner)
        self.assertEqual(found, reservation)
        self.assertEqual(self.registry.cancel(self.project, owner), reservation)
        self.assertTrue(
            self.registry.project_path(self.project, reservation.incarnation).is_dir()
        )
        self.registry.release(self.project, reservation.incarnation, owner)
        self.assertIsNone(self.registry.cancel(self.project, owner))
        with self.assertRaises(RegistryError):
            self.registry.reserve(self.project, reservation.incarnation, owner)

    def test_old_deletion_cannot_touch_recreated_project_or_sibling(self) -> None:
        owner = self.owner()
        first = self.reserve(self.project, owner)
        sibling = self.reserve(self.other, self.owner())
        sibling_file = (
            self.registry.project_path(self.other, sibling.incarnation)
            / "files"
            / "sentinel"
        )
        sibling_file.write_bytes(b"project-b")
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        self.registry.finish_delete(
            self.project, first.incarnation, deleting.deletion_id, (owner,)
        )
        with self.assertRaises(RegistryError):
            self.registry.reserve(self.project, first.incarnation, owner)
        recreated = self.reserve(self.project, self.owner())
        with self.assertRaises(RegistryError):
            self.registry.finish_delete(
                self.project, first.incarnation, deleting.deletion_id, (owner,)
            )
        self.assertTrue(
            self.registry.project_path(self.project, recreated.incarnation).is_dir()
        )
        self.assertEqual(sibling_file.read_bytes(), b"project-b")

    def test_removal_does_not_follow_workload_symlinks(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"untouched")
        files = self.registry.project_path(self.project, record.incarnation) / "files"
        (files / "escape").symlink_to(outside, target_is_directory=True)
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        self.registry.finish_delete(
            self.project, record.incarnation, deleting.deletion_id, (owner,)
        )
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_removal_failure_leaves_retryable_sealed_record(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        with (
            patch.object(registry_module.shutil, "rmtree", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            self.registry.finish_delete(
                self.project, record.incarnation, deleting.deletion_id, (owner,)
            )
        self.assertEqual(self.registry.begin_delete(self.project), deleting)

    def test_concurrent_missing_child_does_not_leave_a_deleted_project_tree(
        self,
    ) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        root = self.registry.project_path(self.project, record.incarnation)
        (root / "files" / "first").write_bytes(b"first")
        (root / "files" / "second").write_bytes(b"second")
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        unlink = os.unlink
        removed = False

        def concurrent_unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
            nonlocal removed
            unlink(path, dir_fd=dir_fd)
            if path == "first" and not removed:
                removed = True
                raise FileNotFoundError("another cleanup removed this entry")

        with patch.object(registry_module.os, "unlink", concurrent_unlink):
            self.registry.finish_delete(
                self.project, record.incarnation, deleting.deletion_id, (owner,)
            )
        self.assertTrue(removed)
        self.assertFalse(root.exists())
        deleted = self.registry.begin_delete(self.project)
        assert deleted is not None
        self.assertEqual(deleted.phase, "deleted")

    def test_release_retains_shared_files_and_ignores_stale_incarnation(self) -> None:
        first_owner, second_owner = self.owner(), self.owner()
        record = self.reserve(self.project, first_owner)
        self.reserve(self.project, second_owner)
        root = self.registry.project_path(self.project, record.incarnation)
        sentinel = root / "files" / "saved"
        sentinel.write_bytes(b"saved")
        self.registry.release(self.project, str(uuid4()), first_owner)
        self.registry.release(self.project, record.incarnation, first_owner)
        self.assertEqual(sentinel.read_bytes(), b"saved")
        self.assertFalse(
            (root / "runs" / self.namespace / first_owner.session_id).exists()
        )
        self.assertTrue(
            (root / "runs" / self.namespace / second_owner.session_id).is_dir()
        )
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None
        self.assertEqual(deleting.sessions, (second_owner,))

    def test_killed_record_writer_does_not_block_container_recovery(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        root = self.registry.project_path(self.project, record.incarnation)
        (root / "files" / "keep.txt").write_text("committed data")
        bootstrap = (
            "import os,signal,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(SOURCE.parent)!r})\n"
            "from registry import ProjectRegistry,SessionOwner\n"
            "def pending(source,destination):\n"
            "    print('write_pending',flush=True)\n"
            "    signal.pause()\n"
            "    raise AssertionError('writer must be killed')\n"
            "os.replace=pending\n"
            f"registry=ProjectRegistry(Path({str(self.registry.root)!r}),uid=os.getuid(),gid=os.getgid(),session_namespace={self.namespace!r})\n"
            f"registry.reserve({self.project!r},{record.incarnation!r},SessionOwner({str(uuid4())!r},{self.namespace!r}))\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", bootstrap],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline(), b"write_pending\n")
            process.kill()
            process.communicate()
            restarted = ProjectRegistry(
                self.registry.root,
                uid=os.getuid(),
                gid=os.getgid(),
                session_namespace=str(uuid4()),
            )
            self.assertEqual(restarted.retire_sessions(), (owner,))
            self.assertEqual(restarted.prepare(self.project), record)
            self.assertEqual(
                (root / "files" / "keep.txt").read_text(), "committed data"
            )
            self.assertEqual(list((self.registry.root / "staging").iterdir()), [])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    @unittest.skipUnless(
        os.geteuid() == 0, "requires separate parent and workload UIDs"
    )
    def test_recovery_preserves_incomplete_directory_ownership_handoff(self) -> None:
        namespace = str(uuid4())
        registry = ProjectRegistry(
            self.base / "handoff", uid=1000, gid=1000, session_namespace=namespace
        )
        record = registry.prepare(self.project)
        owner = SessionOwner(str(uuid4()), namespace)
        chown = os.chown

        def interrupted_handoff(path: Path, uid: int, gid: int) -> None:
            if Path(path).name == "dependencies":
                raise InterruptedError("stopped before workload ownership handoff")
            chown(path, uid, gid)

        with (
            patch.object(registry_module.os, "chown", new=interrupted_handoff),
            self.assertRaises(InterruptedError),
        ):
            registry.reserve(self.project, record.incarnation, owner)
        root = registry.project_path(self.project, record.incarnation)
        self.assertEqual((root / "dependencies").stat().st_uid, 0)
        self.assertEqual((root / "files").stat().st_uid, 1000)
        restarted = ProjectRegistry(
            registry.root, uid=1000, gid=1000, session_namespace=str(uuid4())
        )
        self.assertEqual(restarted.retire_sessions(), (owner,))
        current = SessionOwner(str(uuid4()), restarted.session_namespace)
        restarted.reserve(self.project, record.incarnation, current)
        self.assertEqual((root / "dependencies").stat().st_uid, 1000)

    def test_untrusted_staging_is_not_discarded_during_recovery(self) -> None:
        staging = self.registry.root / "staging"
        unknown = staging / "unknown"
        unknown.write_text("retain evidence")
        with self.assertRaises(RegistryError):
            self.registry.retire_sessions()
        self.assertEqual(unknown.read_text(), "retain evidence")
        unknown.unlink()
        staged = staging / "record-interrupted.pending"
        staged.symlink_to(self.base / "outside")
        with self.assertRaises(RegistryError):
            self.registry.retire_sessions()
        self.assertTrue(staged.is_symlink())

    def test_corrupt_record_fails_closed_without_removing_data(self) -> None:
        record = self.reserve(self.project, self.owner())
        path = self.base / "state" / "records" / self.project
        path.write_bytes(b'{"phase":"deleted"}')
        with self.assertRaises(RegistryError):
            self.registry.begin_delete(self.project)
        self.assertTrue(
            self.registry.project_path(self.project, record.incarnation).exists()
        )

    def test_delete_fences_an_unreceived_reservation_from_a_crashed_caller(
        self,
    ) -> None:
        owner = self.owner()
        prepared = self.registry.prepare(self.project)
        self.assertFalse((self.base / "state" / "projects" / self.project).exists())
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        self.registry.finish_delete(
            self.project, prepared.incarnation, deleting.deletion_id, ()
        )
        replacement = self.registry.prepare(self.project)
        self.assertNotEqual(replacement.incarnation, prepared.incarnation)
        with self.assertRaises(RegistryError):
            self.registry.reserve(self.project, prepared.incarnation, owner)
        self.assertFalse((self.base / "state" / "projects" / self.project).exists())
        admitted = self.registry.reserve(
            self.project, replacement.incarnation, self.owner()
        )
        self.assertEqual(admitted.incarnation, replacement.incarnation)

    def test_cli_reads_complete_input_until_eof(self) -> None:
        request = json.dumps(
            {"operation": "begin_delete", "project": self.project, "arguments": {}}
        ).encode()
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                (
                    "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                    "import lifetime;namespace=sys.argv[2];"
                    "lifetime.current_namespace=lambda:namespace;"
                    "sys.argv=sys.argv[3:];runpy.run_path(sys.argv[0],run_name='__main__')"
                ),
                str(SOURCE.parent),
                self.namespace,
                str(SOURCE),
                "--root",
                str(self.base / "cli"),
                "--uid",
                str(os.getuid()),
                "--gid",
                str(os.getgid()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(b" " * 131072)
            process.stdin.flush()
            output, error = process.communicate(request)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(json.loads(output), None)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_cli_rejects_untrusted_admission_before_creating_registry(self) -> None:
        root = self.base / "unready"
        for failure in (FileNotFoundError, RuntimeError, ValueError):
            with self.subTest(failure=failure):
                bootstrap = (
                    "import runpy,sys\n"
                    "sys.path.insert(0,sys.argv[1])\n"
                    "import lifetime\n"
                    "def unavailable():\n"
                    f"    raise {failure.__name__}('workspace admission is not ready')\n"
                    "lifetime.current_namespace=unavailable\n"
                    "sys.argv=sys.argv[2:]\n"
                    "runpy.run_path(sys.argv[0],run_name='__main__')\n"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        bootstrap,
                        str(SOURCE.parent),
                        str(SOURCE),
                        "--root",
                        str(root),
                    ],
                    input=json.dumps(
                        {
                            "operation": "prepare",
                            "project": self.project,
                            "arguments": {},
                        }
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["error"]["reason"], "unavailable"
                )
                self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
