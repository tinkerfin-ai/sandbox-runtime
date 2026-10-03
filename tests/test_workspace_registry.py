"""Verify trusted project admission, deletion ownership, and filesystem boundaries."""

from __future__ import annotations

import importlib.util
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

SOURCE = Path(__file__).resolve().parents[1] / "workspace-runtime" / "registry.py"
SPEC = importlib.util.spec_from_file_location("workspace_registry", SOURCE)
assert SPEC is not None and SPEC.loader is not None
registry_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry_module
SPEC.loader.exec_module(registry_module)
ProjectRegistry = registry_module.ProjectRegistry
RegistryError = registry_module.RegistryError
SessionOwner = registry_module.SessionOwner


class RegistryTest(unittest.TestCase):
    """Use private temporary directories without native sessions or Docker."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-registry-test-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.registry = ProjectRegistry(
            self.base / "state", uid=os.getuid(), gid=os.getgid()
        )
        self.project = sha256(b"project-a").hexdigest()
        self.other = sha256(b"project-b").hexdigest()
        self.namespace = str(uuid4())

    def owner(self):
        return SessionOwner(str(uuid4()), self.namespace)

    def reserve(self, project, owner):
        prepared = self.registry.prepare(project)
        return self.registry.reserve(project, prepared.incarnation, owner)

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

        def reserve(owner):
            registry = ProjectRegistry(
                self.base / "state", uid=os.getuid(), gid=os.getgid()
            )
            prepared = registry.prepare(self.project)
            barrier.wait()
            return registry.reserve(self.project, prepared.incarnation, owner)

        with ThreadPoolExecutor(max_workers=2) as executor:
            records = tuple(executor.map(reserve, owners))
        self.assertEqual(records[0].incarnation, records[1].incarnation)
        deleting = self.registry.begin_delete(self.project)
        self.assertEqual(set(deleting.sessions), set(owners))

    def test_delete_seals_admission_and_keeps_stable_lock(self) -> None:
        owner = self.owner()
        first = self.reserve(self.project, owner)
        lock = self.base / "state" / "locks" / self.project
        lock_inode = lock.stat().st_ino
        deleting = self.registry.begin_delete(self.project)
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
        self.assertEqual(self.registry.begin_delete(self.project).sessions, ())
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
        for mount in request["binds"]:
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
        self.registry.finish_delete(
            self.project, record.incarnation, deleting.deletion_id, (owner,)
        )
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_removal_failure_leaves_retryable_sealed_record(self) -> None:
        owner = self.owner()
        record = self.reserve(self.project, owner)
        deleting = self.registry.begin_delete(self.project)
        with patch.object(
            registry_module.shutil, "rmtree", side_effect=PermissionError
        ):
            with self.assertRaises(PermissionError):
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
        unlink = os.unlink
        removed = False

        def concurrent_unlink(path, *, dir_fd=None):
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
        self.assertEqual(self.registry.begin_delete(self.project).phase, "deleted")

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
        self.assertEqual(
            self.registry.begin_delete(self.project).sessions, (second_owner,)
        )

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


if __name__ == "__main__":
    unittest.main()
