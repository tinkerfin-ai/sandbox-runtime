"""Verify container admission and startup deadlines with controlled evidence."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import lifetime
import supervise
from test_workspace_egress import Deadlines


class LifetimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.mount = Path(self.temporary.name).resolve()
        self.directory = self.mount / "control"
        self.namespace = str(uuid4())

    def test_only_an_owned_tmpfs_mount_can_be_claimed_once(self) -> None:
        for filesystem in ("overlay", "tmpfs"):
            mountinfo = f"42 1 0:42 / {self.mount} rw - {filesystem} source rw\n"
            with mock.patch.object(Path, "read_text", return_value=mountinfo):
                if filesystem == "overlay":
                    with self.assertRaisesRegex(RuntimeError, "tmpfs"):
                        lifetime.claim(self.directory)
                    self.assertFalse(self.directory.exists())
                else:
                    lifetime.claim(self.directory)
                    with self.assertRaises(FileExistsError):
                        lifetime.claim(self.directory)
        self.assertFalse((self.directory / "ready").exists())

    def test_readiness_requires_complete_private_canonical_identity(self) -> None:
        self.directory.mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            lifetime.current_namespace(self.directory)
        lifetime.publish_namespace(self.namespace, self.directory)
        self.assertEqual(lifetime.current_namespace(self.directory), self.namespace)
        with self.assertRaises(FileExistsError):
            lifetime.publish_namespace(str(uuid4()), self.directory)
        ready = self.directory / "ready"
        for content in ("", "not-a-uuid", self.namespace + "x", self.namespace.upper()):
            ready.write_text(content)
            with self.assertRaises(ValueError):
                lifetime.current_namespace(self.directory)
        ready.write_text(self.namespace)
        ready.chmod(0o644)
        with self.assertRaises(RuntimeError):
            lifetime.current_namespace(self.directory)
        ready.chmod(0o600)
        os.link(ready, self.directory / "alias")
        with self.assertRaises(RuntimeError):
            lifetime.current_namespace(self.directory)
        ready.unlink()
        ready.symlink_to(self.directory / "alias")
        with self.assertRaises(OSError):
            lifetime.current_namespace(self.directory)

    def test_shared_storage_and_unknown_container_roots_are_rejected(self) -> None:
        root = self.mount / "registry"
        overlay = "10 1 0:10 / / rw - overlay overlay rw\n"
        with mock.patch.object(Path, "read_text", return_value=overlay):
            lifetime.require_private_storage(root)
        for directory in (root.parent, root, root / "records", root / "projects"):
            mounts = (
                overlay + f"11 10 0:10 /shared {directory} rw - overlay overlay rw\n"
            )
            with (
                self.subTest(directory=directory),
                mock.patch.object(Path, "read_text", return_value=mounts),
                self.assertRaisesRegex(RuntimeError, "overlap"),
            ):
                lifetime.require_private_storage(root)
        unrelated = overlay + f"11 10 0:11 / {root}-other rw - tmpfs tmpfs rw\n"
        with mock.patch.object(Path, "read_text", return_value=unrelated):
            lifetime.require_private_storage(root)
        for mounts in (
            overlay.replace("overlay", "ext4"),
            overlay.replace("/ / rw", "/ / ro"),
            overlay + overlay,
        ):
            with (
                mock.patch.object(Path, "read_text", return_value=mounts),
                self.assertRaises(RuntimeError),
            ):
                lifetime.require_private_storage(root)

    def test_nested_or_shared_control_mounts_cannot_supply_lifetime_evidence(
        self,
    ) -> None:
        mounted = f"10 1 0:10 / {self.mount} rw - tmpfs tmpfs rw\n"
        for mounts in (
            mounted + mounted,
            mounted.replace(" / ", " /shared "),
            mounted + f"11 10 0:11 / {self.directory} rw - tmpfs tmpfs rw\n",
            mounted + f"11 10 0:11 / {self.directory}/ready rw - tmpfs tmpfs rw\n",
        ):
            with (
                mock.patch.object(Path, "read_text", return_value=mounts),
                self.assertRaises(RuntimeError),
            ):
                lifetime.claim(self.directory)
            self.assertFalse(self.directory.exists())

    def test_mount_paths_are_decoded_before_component_comparison(self) -> None:
        root = self.mount / "with space"
        encoded = str(root).replace(" ", r"\040")
        mounts = "10 1 0:10 / / rw - overlay overlay rw\n"
        mounts += f"11 10 0:11 / {encoded}/records rw - tmpfs tmpfs rw\n"
        with (
            mock.patch.object(Path, "read_text", return_value=mounts),
            self.assertRaisesRegex(RuntimeError, "overlap"),
        ):
            lifetime.require_private_storage(root)

    def test_untrusted_directory_cannot_publish_or_supply_readiness(self) -> None:
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(RuntimeError):
            lifetime.publish_namespace(self.namespace, self.directory)
        with self.assertRaises(RuntimeError):
            lifetime.current_namespace(self.directory)
        self.directory.rmdir()
        self.directory.symlink_to(self.mount, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            lifetime.publish_namespace(self.namespace, self.directory)


class ExecdReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_use_local_authentication_and_close_the_connection(
        self,
    ) -> None:
        namespace = str(uuid4())
        content = json.dumps(
            {"available": True, "session_namespace": namespace}
        ).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.0 200 OK\r\n\r\n" + content)
        reader.feed_eof()
        writer = mock.Mock(spec=asyncio.StreamWriter)
        writer.drain = mock.AsyncMock()
        writer.wait_closed = mock.AsyncMock()
        with mock.patch.object(
            asyncio, "open_connection", return_value=(reader, writer)
        ) as connection:
            self.assertEqual(await supervise._execd_namespace("a" * 64), namespace)
        connection.assert_awaited_once_with("127.0.0.1", 44772, limit=8192)
        self.assertIn(
            b"X-EXECD-ACCESS-TOKEN: " + b"a" * 64, writer.write.call_args.args[0]
        )
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_invalid_capabilities_fail_closed_and_close_the_connection(
        self,
    ) -> None:
        responses = (
            b"HTTP/1.0 401 Unauthorized\r\n\r\n{}",
            b"HTTP/1.0 200 OK\r\n\r\n[]",
            b'HTTP/1.0 200 OK\r\n\r\n{"available":false,"session_namespace":"00000000-0000-0000-0000-000000000001"}',
            b'HTTP/1.0 200 OK\r\n\r\n{"available":true,"session_namespace":"wrong"}',
            b"HTTP/1.0 200 OK\r\n\r\n" + b"x" * 8192,
        )
        for response in responses:
            with self.subTest(response=response[:40]):
                reader = asyncio.StreamReader()
                reader.feed_data(response)
                reader.feed_eof()
                writer = mock.Mock(spec=asyncio.StreamWriter)
                writer.drain = mock.AsyncMock()
                writer.wait_closed = mock.AsyncMock()
                with (
                    mock.patch.object(
                        asyncio, "open_connection", return_value=(reader, writer)
                    ),
                    self.assertRaises((ValueError, TypeError, RuntimeError)),
                ):
                    await supervise._execd_namespace("a" * 64)
                writer.close.assert_called_once()
                writer.wait_closed.assert_awaited_once()

    async def test_readiness_waits_only_for_connection_refusal(self) -> None:
        namespace = str(uuid4())
        with (
            mock.patch.dict(os.environ, {"EXECD_ACCESS_TOKEN": "a" * 64}),
            mock.patch.object(
                supervise,
                "_execd_namespace",
                side_effect=[ConnectionRefusedError(), namespace],
            ) as capabilities,
            mock.patch.object(asyncio, "sleep") as wait,
        ):
            self.assertEqual(await supervise._wait_execd_namespace(), namespace)
            self.assertEqual(capabilities.await_count, 2)
            wait.assert_awaited_once_with(0.05)
        with (
            mock.patch.dict(os.environ, {"EXECD_ACCESS_TOKEN": "a" * 64}),
            mock.patch.object(
                supervise,
                "_execd_namespace",
                side_effect=ValueError("invalid capabilities"),
            ) as capabilities,
            mock.patch.object(asyncio, "sleep") as wait,
        ):
            with self.assertRaises(ValueError):
                await supervise._wait_execd_namespace()
            capabilities.assert_awaited_once()
            wait.assert_not_awaited()

    async def test_invalid_authentication_fails_before_connecting(self) -> None:
        for token in ("", "short", "a" * 63 + "\n", "A" * 64):
            with (
                self.subTest(token=token),
                mock.patch.dict(os.environ, {"EXECD_ACCESS_TOKEN": token}),
                mock.patch.object(supervise, "_execd_namespace") as capabilities,
                self.assertRaises(ValueError),
            ):
                try:
                    await supervise._wait_execd_namespace()
                finally:
                    capabilities.assert_not_awaited()

    async def test_startup_deadline_and_cancellation_close_owned_io(self) -> None:
        for expire in (True, False):
            with self.subTest(expire=expire):
                deadlines = Deadlines()
                reading = asyncio.Event()
                closed = asyncio.Event()

                async def capabilities(
                    token: str,
                    reading: asyncio.Event = reading,
                    closed: asyncio.Event = closed,
                ) -> str:
                    del token
                    reading.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closed.set()
                    raise AssertionError("The controlled wait must be interrupted")

                with (
                    mock.patch.dict(os.environ, {"EXECD_ACCESS_TOKEN": "a" * 64}),
                    mock.patch.object(supervise, "_execd_namespace", new=capabilities),
                    mock.patch.object(asyncio, "timeout", new=deadlines.timeout),
                ):
                    task = asyncio.create_task(supervise._wait_execd_namespace())
                    try:
                        await reading.wait()
                        if expire:
                            (await deadlines.active(30)).expire()
                        else:
                            task.cancel()
                        with self.assertRaises(
                            TimeoutError if expire else asyncio.CancelledError
                        ):
                            await task
                        self.assertTrue(closed.is_set())
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
