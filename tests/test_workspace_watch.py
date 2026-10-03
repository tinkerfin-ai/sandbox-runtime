"""Verify fixed project ownership and bounded, non-authoritative change streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from _thread import RLock
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import ClassVar, Self, TypeVar
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace-runtime"))

import supervise
import watch
from registry import ProjectRegistry, RegistryError, SessionOwner

_Awaited = TypeVar("_Awaited")


async def _settle(task: asyncio.Task[_Awaited]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _bootstrap_lock() -> RLock:
    lock = vars(threading)["_active_limbo_lock"]
    assert isinstance(lock, RLock)
    return lock


def _asyncio_with_join_signal(
    sources: Callable[[], Iterable[watch._Source]],
) -> SimpleNamespace:
    async def sleep(delay: float) -> None:
        assert delay == watch._JOIN_INTERVAL
        current = asyncio.current_task()
        source = next(source for source in sources() if source.closing is current)
        await asyncio.to_thread(source.thread.join)

    return SimpleNamespace(**{**vars(asyncio), "sleep": sleep})


class ObservationTest(unittest.TestCase):
    """Exercise read-only observation without Linux resources or native sessions."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-watch-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "state"
        self.project = sha256(b"project-a").hexdigest()
        self.other = sha256(b"project-b").hexdigest()
        self.registry = ProjectRegistry(self.root, uid=os.getuid(), gid=os.getgid())

    def reserve(self, project: str):
        prepared = self.registry.prepare(project)
        owner = SessionOwner(str(uuid4()), str(uuid4()))
        return self.registry.reserve(project, prepared.incarnation, owner)

    def test_missing_registry_or_project_is_not_created(self) -> None:
        missing = Path(self.directory.name) / "missing"
        for root in (missing, self.root):
            with (
                self.assertRaises(FileNotFoundError),
                ProjectRegistry.open_existing(root, self.project),
            ):
                self.fail("missing project was observed")
        self.assertFalse(missing.exists())
        self.assertEqual(list((self.root / "records").iterdir()), [])
        self.assertEqual(list((self.root / "locks").iterdir()), [])

    def test_prepared_project_does_not_create_files(self) -> None:
        self.registry.prepare(self.project)
        with (
            self.assertRaises(FileNotFoundError),
            ProjectRegistry.open_existing(self.root, self.project),
        ):
            self.fail("uninitialized files were observed")
        self.assertEqual(list((self.root / "projects").iterdir()), [])

    def test_atomic_record_replacement_detects_deletion_and_recreation(self) -> None:
        record = self.reserve(self.project)
        with ProjectRegistry.open_existing(self.root, self.project) as observation:
            self.assertEqual(observation.incarnation, record.incarnation)
            deleting = self.registry.begin_delete(self.project)
            assert deleting is not None and deleting.deletion_id is not None
            with self.assertRaises(RegistryError) as rejected:
                observation.validate()
            self.assertEqual(rejected.exception.reason, "busy")
            self.registry.finish_delete(
                self.project, record.incarnation, deleting.deletion_id, record.sessions
            )
            self.reserve(self.project)
            with self.assertRaises((FileNotFoundError, RegistryError)):
                observation.validate()

    def test_files_root_replacement_never_rebinds_to_sibling(self) -> None:
        record = self.reserve(self.project)
        other = self.reserve(self.other)
        files = self.registry.project_path(self.project, record.incarnation) / "files"
        sibling = self.registry.project_path(self.other, other.incarnation) / "files"
        (files / "sentinel").write_bytes(b"project-a")
        (sibling / "sentinel").write_bytes(b"project-b")
        with ProjectRegistry.open_existing(self.root, self.project) as observation:
            files.rename(files.with_name("pinned"))
            files.symlink_to(sibling, target_is_directory=True)
            with self.assertRaises(RegistryError):
                observation.validate()
            descriptor = os.open("sentinel", os.O_RDONLY, dir_fd=observation.files)
            with os.fdopen(descriptor, "rb") as source:
                self.assertEqual(source.read(), b"project-a")
            borrowed = observation.files
        with self.assertRaises(OSError):
            os.fstat(borrowed)
        with (
            self.assertRaises(OSError),
            ProjectRegistry.open_existing(self.root, self.project),
        ):
            self.fail("replacement symlink was followed")

    def test_deleted_record_is_stale_and_observation_preserves_sessions(self) -> None:
        record = self.reserve(self.project)
        with ProjectRegistry.open_existing(self.root, self.project) as observation:
            observation.validate()
        deleting = self.registry.begin_delete(self.project)
        assert deleting is not None and deleting.deletion_id is not None
        self.assertEqual(deleting.sessions, record.sessions)
        self.registry.finish_delete(
            self.project, record.incarnation, deleting.deletion_id, record.sessions
        )
        with (
            self.assertRaises(RegistryError) as rejected,
            ProjectRegistry.open_existing(self.root, self.project),
        ):
            self.fail("deleted project was observed")
        self.assertEqual(rejected.exception.reason, "stale")

    def test_open_snapshot_survives_an_atomic_active_record_replacement(self) -> None:
        record = self.reserve(self.project)
        replaced = False
        original_open = os.open
        with ProjectRegistry.open_existing(self.root, self.project) as observation:

            def replace_record(
                path: str | Path,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    path == self.project
                    and dir_fd == observation.records
                    and not replaced
                ):
                    replaced = True
                    self.registry.reserve(
                        self.project,
                        record.incarnation,
                        SessionOwner(str(uuid4()), str(uuid4())),
                    )
                return descriptor

            with patch.object(os, "open", replace_record):
                observation.validate()
            self.assertTrue(replaced)
            observation.validate()

    def test_observation_rejects_hardlinked_records(self) -> None:
        self.reserve(self.project)
        with ProjectRegistry.open_existing(self.root, self.project) as observation:
            os.link(self.root / "records" / self.project, self.root / "borrowed-record")
            with self.assertRaises(RegistryError):
                observation.validate()

    def test_registry_root_replacement_is_rejected_before_arming(self) -> None:
        record = self.reserve(self.project)
        files = self.registry.project_path(self.project, record.incarnation) / "files"
        (files / "sentinel").write_bytes(b"original")
        with ProjectRegistry.open_existing(self.root, self.project) as observation:
            self.root.rename(self.root.with_name("retired-state"))
            self.root.mkdir(mode=0o700)
            with self.assertRaises(RegistryError):
                observation.validate()
            descriptor = os.open("sentinel", os.O_RDONLY, dir_fd=observation.files)
            with os.fdopen(descriptor, "rb") as source:
                self.assertEqual(source.read(), b"original")


class ParserTest(unittest.TestCase):
    def test_record_removal_and_recreation_ends_the_existing_source(self) -> None:
        effects = watch._effects(
            (
                watch._Event(3, watch._DELETE, b"project"),
                watch._Event(3, watch._CREATE, b"project"),
            ),
            {3: "records"},
            "project",
        )
        self.assertTrue(effects.closed)

    def test_active_registry_updates_are_not_file_changes(self) -> None:
        effects = watch._effects(
            (watch._Event(3, watch._MOVED_TO, b"project"),),
            {3: "records"},
            "project",
        )
        self.assertEqual(effects, watch._Effects())

    def test_inotify_records_keep_only_masks_and_names(self) -> None:
        payload = watch._EVENT.pack(7, watch._CREATE, 19, 8) + b"item\0\0\0\0"
        payload += watch._EVENT.pack(-1, watch._OVERFLOW, 0, 0)
        self.assertEqual(
            watch._events(payload),
            (
                watch._Event(7, watch._CREATE, b"item"),
                watch._Event(-1, watch._OVERFLOW, b""),
            ),
        )
        for invalid in (
            payload[:-1],
            watch._EVENT.pack(7, watch._CREATE, 0, 257) + bytes(257),
            watch._EVENT.pack(7, watch._CREATE, 0, 4) + b"name",
            watch._EVENT.pack(7, watch._CREATE, 0, 4) + b"a/b\0",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                watch._events(invalid)

    def test_unknown_descriptors_and_directory_moves_require_resync(self) -> None:
        watches: dict[int, watch.Kind] = {1: "files", 2: "directory", 3: "records"}
        for event, reason in (
            (watch._Event(9, watch._MODIFY, b"item"), "overflow"),
            (watch._Event(-1, watch._OVERFLOW, b""), "overflow"),
            (watch._Event(2, watch._MOVE_SELF, b""), "topology"),
            (watch._Event(1, watch._CREATE | watch._ISDIR, b"sub"), "topology"),
        ):
            with self.subTest(event=event):
                effects = watch._effects((event,), watches, "project")
                self.assertEqual(effects.resync, reason)
        for descriptor in (1, 3):
            effects = watch._effects(
                (watch._Event(descriptor, watch._DELETE_SELF, b""),), watches, "project"
            )
            self.assertTrue(effects.closed)

    def test_http_route_rejects_paths_bodies_and_duplicate_headers(self) -> None:
        digest = sha256(b"project").hexdigest()
        request = f"GET /projects/{digest}/changes HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
        self.assertEqual(watch._project_request(request), digest)
        for invalid in (
            request.replace(digest.encode(), b"../other"),
            request.replace(digest.encode(), digest.upper().encode()),
            request.replace(b"GET", b"POST", 1),
            request.replace(b"Host:", b"Content-Length: 1\r\nHost:", 1),
            request.replace(b"Host:", b"Transfer-Encoding: chunked\r\nHost:", 1),
            request.replace(b"Host:", b"Host: duplicate\r\nHost:", 1),
            request + b"body",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(watch.Unavailable):
                watch._project_request(invalid)


class NoticeTest(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_changes_do_not_reset_heartbeat_deadline(self) -> None:
        deadlines = _Deadlines()
        notices = watch._Notices()
        notices.put("changed")
        service = watch.WorkspaceWatch(
            limits=replace(watch._DEFAULT_LIMITS, heartbeat=33)
        )
        received: list[str] = []
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)

        async def send(endpoint: socket.socket, notice: dict[str, str]) -> None:
            received.append(notice["type"])
            if notice["type"] == "heartbeat" or len(received) == 3:
                notices.put("closed")
            elif notice["type"] == "changed":
                deadlines.now += 33
                notices.put("changed")

        with (
            patch.object(watch, "asyncio", deadlines.module()),
            patch.object(service, "_send", send),
        ):
            await service._stream(writer, notices)
        self.assertEqual(received, ["changed", "heartbeat", "changed", "closed"])

    async def test_terminal_notices_take_precedence_over_a_due_heartbeat(self) -> None:
        deadlines = _Deadlines()
        notices = watch._Notices()
        notices.put("changed")
        service = watch.WorkspaceWatch(
            limits=replace(watch._DEFAULT_LIMITS, heartbeat=33)
        )
        received: list[str] = []
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)

        async def send(endpoint: socket.socket, notice: dict[str, str]) -> None:
            received.append(notice["type"])
            if notice["type"] == "changed":
                deadlines.now += 33
                notices.put("resync", "source_closed")
                notices.put("closed")

        with (
            patch.object(watch, "asyncio", deadlines.module()),
            patch.object(service, "_send", send),
        ):
            await service._stream(writer, notices)
        self.assertEqual(received, ["changed", "resync", "closed"])

    async def test_slow_consumer_merges_hints_and_preserves_terminal_resync(
        self,
    ) -> None:
        notices = watch._Notices()
        notices.put("changed")
        notices.put("changed")
        notices.put("resync", "overflow")
        notices.put("changed")
        notices.put("changed")
        self.assertEqual(len(notices.pending), 2)
        notices.put("resync", "source_closed")
        notices.put("closed")
        notices.put("changed")
        self.assertEqual(
            await notices.get(), {"type": "resync", "reason": "source_closed"}
        )
        self.assertEqual(await notices.get(), {"type": "closed"})
        self.assertEqual(len(notices.pending), 0)


class _ManualInotify:
    instances: ClassVar[list[_ManualInotify]] = []
    initial: tuple[watch._Event, ...] = ()

    def __init__(self, budget: watch._Budget, maximum: int) -> None:
        self.budget = budget
        self.maximum = maximum
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.descriptor = self.reader.fileno()
        self.watches: dict[int, watch.Kind] = {}
        self.identities: set[tuple[int, int]] = set()
        self.pending: deque[tuple[watch._Event, ...]] = deque()
        self.lock = threading.Lock()
        self.instances.append(self)
        if self.initial:
            self.emit(self.initial)

    def add(self, directory: int, kind: watch.Kind) -> None:
        if len(self.watches) >= self.maximum:
            raise watch.Unavailable()
        self.budget.acquire()
        self.watches[len(self.watches) + 1] = kind
        info = os.fstat(directory)
        self.identities.add((info.st_dev, info.st_ino))

    def emit(self, events: tuple[watch._Event, ...]) -> None:
        with self.lock:
            self.pending.append(events)
            self.writer.send(b"x")

    def read(self) -> tuple[watch._Event, ...]:
        with self.lock:
            if not self.pending:
                return ()
            self.reader.recv(1)
            return self.pending.popleft()

    def close(self) -> None:
        if self.descriptor >= 0:
            self.reader.close()
            self.writer.close()
            self.descriptor = -1
            self.budget.release(len(self.watches))
            self.watches.clear()

    def descriptor_for(self, kind: watch.Kind) -> int:
        return next(
            descriptor for descriptor, value in self.watches.items() if value == kind
        )


class SourceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-watch-source-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "state"
        self.project = sha256(b"project-a").hexdigest()
        self.registry = ProjectRegistry(self.root, uid=os.getuid(), gid=os.getgid())
        prepared = self.registry.prepare(self.project)
        self.record = self.registry.reserve(
            self.project, prepared.incarnation, SessionOwner(str(uuid4()), str(uuid4()))
        )
        self.files = (
            self.registry.project_path(self.project, self.record.incarnation) / "files"
        )
        self.budget = watch._Budget(128)
        self.owned_sources: list[watch._Source] = []
        clock = patch.object(
            watch, "asyncio", _asyncio_with_join_signal(lambda: self.owned_sources)
        )
        clock.start()
        self.addCleanup(clock.stop)
        _ManualInotify.instances = []
        self.backend = patch.object(watch, "_Inotify", _ManualInotify)
        self.backend.start()
        self.addCleanup(self.backend.stop)

    async def source(
        self, limits: watch.Limits = watch._DEFAULT_LIMITS
    ) -> watch._Source:
        source = watch._Source(self.root, self.project, self.budget, limits)
        self.owned_sources.append(source)
        self.addAsyncCleanup(source.aclose)
        source.start()
        return source

    async def test_ready_arms_existing_tree_without_following_symlinks_or_fifo(
        self,
    ) -> None:
        nested = self.files / "nested"
        nested.mkdir()
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (nested / "link").symlink_to(outside, target_is_directory=True)
        os.mkfifo(nested / "fifo")
        source = await self.source()
        self.assertEqual(
            await source.opened, watch._Opened(200, self.record.incarnation)
        )
        backend = _ManualInotify.instances[-1]
        info = nested.stat()
        self.assertIn((info.st_dev, info.st_ino), backend.identities)
        info = outside.stat()
        self.assertNotIn((info.st_dev, info.st_ino), backend.identities)
        await source.aclose()
        self.assertTrue(source.completed.done())
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(source.stop_reader.fileno(), -1)
        self.assertEqual(source.stop_writer.fileno(), -1)
        self.assertEqual(self.budget.used, 0)

    async def test_changed_then_topology_rebuild_stays_pinned_to_root(self) -> None:
        source = await self.source()
        notices = watch._Notices()
        source.listeners.add(notices)
        await source.opened
        backend = _ManualInotify.instances[-1]
        backend.emit(
            (watch._Event(backend.descriptor_for("files"), watch._MODIFY, b"value"),)
        )
        self.assertEqual(await notices.get(), {"type": "changed"})
        nested = self.files / "new"
        nested.mkdir()
        backend.emit(
            (
                watch._Event(
                    backend.descriptor_for("files"),
                    watch._CREATE | watch._ISDIR,
                    b"new",
                ),
            )
        )
        self.assertEqual(await notices.get(), {"type": "resync", "reason": "topology"})
        replacement = _ManualInotify.instances[-1]
        self.assertIsNot(backend, replacement)
        self.assertEqual(backend.descriptor, -1)
        info = nested.stat()
        self.assertIn((info.st_dev, info.st_ino), replacement.identities)
        replacement.emit((watch._Event(-1, watch._OVERFLOW, b""),))
        self.assertEqual(await notices.get(), {"type": "resync", "reason": "overflow"})

    async def test_atomic_deletion_closes_source_without_removing_files(self) -> None:
        source = await self.source()
        notices = watch._Notices()
        source.listeners.add(notices)
        await source.opened
        backend = _ManualInotify.instances[-1]
        self.registry.begin_delete(self.project)
        backend.emit(
            (
                watch._Event(
                    backend.descriptor_for("records"),
                    watch._MOVED_TO,
                    self.project.encode(),
                ),
            )
        )
        self.assertEqual(
            await notices.get(), {"type": "resync", "reason": "source_closed"}
        )
        self.assertEqual(await notices.get(), {"type": "closed"})
        await source.aclose()
        self.assertTrue(self.files.exists())
        self.assertEqual(self.budget.used, 0)

    async def test_incomplete_initial_topology_never_sends_ready(self) -> None:
        with patch.object(
            _ManualInotify, "initial", (watch._Event(-1, watch._OVERFLOW, b""),)
        ):
            source = await self.source()
            self.assertEqual(await source.opened, watch._Opened(503))
            await source.aclose()
        self.assertEqual(self.budget.used, 0)

    async def test_directory_capacity_releases_every_armed_watch(self) -> None:
        source = await self.source(replace(watch._DEFAULT_LIMITS, directories=1))
        self.assertEqual(await source.opened, watch._Opened(503))
        await source.aclose()
        self.assertEqual(self.budget.used, 0)

    async def test_cancellation_during_scan_waits_for_worker_and_propagates(
        self,
    ) -> None:
        entered = asyncio.Event()
        stopping = asyncio.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        loop = asyncio.get_running_loop()
        original = watch._Tree._scan

        def scan(tree: watch._Tree, descriptor: int, depth: int) -> None:
            loop.call_soon_threadsafe(entered.set)
            tree.stopping.wait()
            loop.call_soon_threadsafe(stopping.set)
            release.wait()
            original(tree, descriptor, depth)

        with patch.object(watch._Tree, "_scan", scan):
            source = await self.source()
            await entered.wait()
            closing = asyncio.create_task(source.aclose())
            await stopping.wait()
            closing.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await closing
        self.assertTrue(source.completed.done())
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(self.budget.used, 0)

    async def test_failed_thread_start_has_no_job_and_closes_owned_sockets(
        self,
    ) -> None:
        source = watch._Source(
            self.root, self.project, self.budget, watch._DEFAULT_LIMITS
        )
        self.owned_sources.append(source)
        self.addAsyncCleanup(source.aclose)
        with (
            patch.object(source.thread, "start", side_effect=RuntimeError("no thread")),
            self.assertRaises(watch.Unavailable),
        ):
            source.start()
        await source.aclose()
        self.assertTrue(source.completed.done())
        self.assertIsNone(source.thread.ident)
        self.assertEqual(source.stop_reader.fileno(), -1)
        self.assertEqual(source.stop_writer.fileno(), -1)
        self.assertEqual(self.budget.used, 0)

    async def test_partial_thread_start_failure_still_stops_and_joins(self) -> None:
        source = watch._Source(
            self.root, self.project, self.budget, watch._DEFAULT_LIMITS
        )
        self.owned_sources.append(source)
        self.addAsyncCleanup(source.aclose)
        original = source.thread.start

        def start_then_fail() -> None:
            original()
            raise RuntimeError("start acknowledgement failed")

        with (
            patch.object(source.thread, "start", start_then_fail),
            self.assertRaises(watch.Unavailable),
        ):
            source.start()
        await source.aclose()
        self.assertTrue(source.completed.done())
        self.assertIsNotNone(source.thread.ident)
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(source.stop_reader.fileno(), -1)
        self.assertEqual(source.stop_writer.fileno(), -1)
        self.assertEqual(self.budget.used, 0)

    async def test_cancelled_close_callers_retain_one_cleanup_until_thread_exit(
        self,
    ) -> None:
        source = await self.source()
        await source.opened
        joining = asyncio.Event()
        second_stopped = asyncio.Event()
        release = asyncio.Event()
        original_join = source.thread.join
        original_stop = source._stop
        stop_calls = 0

        async def sleep(delay: float) -> None:
            self.assertEqual(delay, watch._JOIN_INTERVAL)
            joining.set()
            await release.wait()

        def stop() -> None:
            nonlocal stop_calls
            stop_calls += 1
            original_stop()
            if stop_calls == 2:
                second_stopped.set()

        with (
            patch.object(
                watch,
                "asyncio",
                SimpleNamespace(**{**vars(watch.asyncio), "sleep": sleep}),
            ),
            patch.object(source, "_stop", side_effect=stop),
        ):
            with _bootstrap_lock():
                first = asyncio.create_task(source.aclose())
                await joining.wait()
                cleanup = source.closing
                second = asyncio.create_task(source.aclose())
                await second_stopped.wait()
                self.assertIs(source.closing, cleanup)
                self.assertTrue(source.completed.done())
                self.assertTrue(source.thread.is_alive())
                self.assertFalse(source.released)
                self.assertGreaterEqual(source.stop_reader.fileno(), 0)
                first.cancel()
                second.cancel()
            await asyncio.to_thread(original_join)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertTrue(
                all(isinstance(result, asyncio.CancelledError) for result in results)
            )
        self.assertFalse(source.thread.is_alive())
        self.assertTrue(source.released)
        self.assertEqual(source.stop_reader.fileno(), -1)

    async def test_other_project_record_updates_do_not_notify_or_close_sources(
        self,
    ) -> None:
        first = await self.source()
        first_notices = watch._Notices()
        first.listeners.add(first_notices)
        await first.opened
        first_backend = _ManualInotify.instances[-1]
        other = sha256(b"project-b").hexdigest()
        prepared = self.registry.prepare(other)
        self.registry.reserve(
            other, prepared.incarnation, SessionOwner(str(uuid4()), str(uuid4()))
        )
        second = watch._Source(self.root, other, self.budget, watch._DEFAULT_LIMITS)
        self.owned_sources.append(second)
        self.addAsyncCleanup(second.aclose)
        second_notices = watch._Notices()
        second.listeners.add(second_notices)
        second.start()
        await second.opened
        second_backend = _ManualInotify.instances[-1]
        loop = asyncio.get_running_loop()
        checked = {self.project: asyncio.Event(), other: asyncio.Event()}
        original = watch.ProjectObservation.validate

        def validate(observed: watch.ProjectObservation) -> None:
            original(observed)
            loop.call_soon_threadsafe(checked[observed.project].set)

        with patch.object(watch.ProjectObservation, "validate", validate):
            self.registry.reserve(
                other, prepared.incarnation, SessionOwner(str(uuid4()), str(uuid4()))
            )
            for backend in (first_backend, second_backend):
                backend.emit(
                    (
                        watch._Event(
                            backend.descriptor_for("records"),
                            watch._MOVED_TO,
                            other.encode(),
                        ),
                    )
                )
            await asyncio.gather(*(event.wait() for event in checked.values()))
            self.assertFalse(first.closed)
            self.assertFalse(second.closed)
            await asyncio.gather(first.aclose(), second.aclose())
        self.assertEqual(list(first_notices.pending), [{"type": "closed"}])
        self.assertEqual(list(second_notices.pending), [{"type": "closed"}])
        self.assertEqual(self.budget.used, 0)


class _Deadline:
    def __init__(self, clock: _Deadlines, delay: float) -> None:
        self.clock = clock
        self.delay = delay
        self.active = False
        self.expired = False
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.current_task()
        self.active = True
        self.clock.changed.set()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False
        if self.expired and isinstance(exception, asyncio.CancelledError):
            assert self.task is not None
            self.task.uncancel()
            raise TimeoutError from exception

    def expire(self) -> None:
        assert self.task is not None and self.active
        self.expired = True
        self.task.cancel()


class _Deadlines:
    def __init__(self) -> None:
        self.now = 0.0
        self.created: list[_Deadline] = []
        self.changed = asyncio.Event()

    def timeout(self, delay: float) -> _Deadline:
        deadline = _Deadline(self, delay)
        self.created.append(deadline)
        return deadline

    async def active(self, delay: float) -> _Deadline:
        while True:
            for deadline in self.created:
                if deadline.active and deadline.delay == delay:
                    return deadline
            self.changed.clear()
            await self.changed.wait()

    def module(self) -> SimpleNamespace:
        loop = asyncio.get_running_loop()

        def current_loop() -> SimpleNamespace:
            return SimpleNamespace(
                time=lambda: self.now,
                create_future=loop.create_future,
                call_soon_threadsafe=loop.call_soon_threadsafe,
                sock_accept=loop.sock_accept,
                sock_recv=loop.sock_recv,
                sock_sendall=loop.sock_sendall,
            )

        return SimpleNamespace(
            **{
                **vars(watch.asyncio),
                "timeout": self.timeout,
                "get_running_loop": current_loop,
            }
        )


@dataclass(frozen=True, slots=True)
class _Retiring:
    service: watch.WorkspaceWatch
    source: watch._Source
    admissions: asyncio.Queue[None]
    deadlines: _Deadlines


class HttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-watch-http-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "state"
        self.project = sha256(b"project-a").hexdigest()
        self.registry = ProjectRegistry(self.root, uid=os.getuid(), gid=os.getgid())
        prepared = self.registry.prepare(self.project)
        self.record = self.registry.reserve(
            self.project, prepared.incarnation, SessionOwner(str(uuid4()), str(uuid4()))
        )
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.addCleanup(self.listener.close)
        self.address = self.listener.getsockname()
        _ManualInotify.instances = []
        backend = patch.object(watch, "_Inotify", _ManualInotify)
        backend.start()
        self.addCleanup(backend.stop)
        self.service: watch.WorkspaceWatch | None = None
        self.serving: asyncio.Task[None] | None = None
        clock = patch.object(
            watch,
            "asyncio",
            _asyncio_with_join_signal(
                lambda: () if self.service is None else self.service.sources.values()
            ),
        )
        clock.start()
        self.addCleanup(clock.stop)

    async def start(
        self, limits: watch.Limits = watch._DEFAULT_LIMITS
    ) -> watch.WorkspaceWatch:
        service = watch.WorkspaceWatch(self.root, listener=self.listener, limits=limits)
        self.service = service
        self.serving = asyncio.create_task(service.serve())
        self.addAsyncCleanup(self.stop)
        return service

    async def stop(self) -> None:
        if self.serving is not None:
            self.serving.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.serving
        if self.service is not None:
            self.assertEqual(self.service.sources, {})
            self.assertEqual(self.service.budget.used, 0)

    async def connect(
        self, project: str | None = None
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection(*self.address)
        self.addAsyncCleanup(self.close_writer, writer)
        writer.write(
            f"GET /projects/{project or self.project}/changes HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
        )
        await writer.drain()
        return reader, writer

    async def close_writer(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    async def ready(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers = await reader.readuntil(b"\r\n\r\n")
        self.assertTrue(headers.startswith(b"HTTP/1.1 200 OK\r\n"), headers)
        self.assertIn(b"Content-Type: application/x-ndjson\r\n", headers)
        notice = json.loads(await reader.readline())
        self.assertEqual(set(notice), {"type", "source", "incarnation"})
        self.assertEqual(notice["type"], "ready")
        self.assertEqual(notice["incarnation"], self.record.incarnation)
        return notice

    @contextlib.asynccontextmanager
    async def retiring_project(
        self, limits: watch.Limits = watch._DEFAULT_LIMITS
    ) -> AsyncIterator[_Retiring]:
        service = await self.start(
            replace(limits, request_timeout=11, write_timeout=22, heartbeat=33)
        )
        first, writer = await self.connect()
        await self.ready(first)
        source = service.sources[self.project]
        request = next(iter(service.clients))
        native_exit = asyncio.get_running_loop().run_in_executor(
            None, source.thread.join
        )
        joined = asyncio.Event()
        release = asyncio.Event()
        admissions: asyncio.Queue[None] = asyncio.Queue(maxsize=64)
        deadlines = _Deadlines()
        original_sleep = watch.asyncio.sleep

        async def sleep(delay: float) -> None:
            if asyncio.current_task() is source.closing:
                self.assertEqual(delay, watch._JOIN_INTERVAL)
                joined.set()
                await release.wait()
            else:
                await original_sleep(delay)

        def shield(value: asyncio.Future[_Awaited]) -> asyncio.Future[_Awaited]:
            if value is source.closing and asyncio.current_task() is not request:
                admissions.put_nowait(None)
            return asyncio.shield(value)

        module = deadlines.module()
        module.sleep = sleep
        module.shield = shield
        with patch.object(watch, "asyncio", module):
            try:
                with _bootstrap_lock():
                    await self.close_writer(writer)
                    checking = asyncio.create_task(joined.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (checking, request), return_when=asyncio.FIRST_COMPLETED
                        )
                        if request in done:
                            request.result()
                            self.fail(
                                "retirement ended before thread exit confirmation"
                            )
                    finally:
                        await _settle(checking)
                    yield _Retiring(service, source, admissions, deadlines)
            finally:
                source._stop()
                await native_exit
                release.set()
                await request

    async def waits_for_retirement(
        self, retiring: _Retiring, opening: asyncio.Task[_Awaited]
    ) -> None:
        waiting = asyncio.create_task(retiring.admissions.get())
        try:
            done, _ = await asyncio.wait(
                (waiting, opening), return_when=asyncio.FIRST_COMPLETED
            )
            if opening in done:
                opening.result()
                self.fail("admission finished before the previous source retired")
        finally:
            await _settle(waiting)

    async def test_reopening_waits_for_retirement_and_shares_one_fresh_source(
        self,
    ) -> None:
        async with self.retiring_project(
            replace(watch._DEFAULT_LIMITS, projects=1)
        ) as retiring:
            second, _ = await self.connect()
            second_ready = asyncio.create_task(self.ready(second))
            self.addAsyncCleanup(_settle, second_ready)
            await self.waits_for_retirement(retiring, second_ready)
            third, _ = await self.connect()
            third_ready = asyncio.create_task(self.ready(third))
            self.addAsyncCleanup(_settle, third_ready)
            await self.waits_for_retirement(retiring, third_ready)
            self.assertIs(retiring.service.sources[self.project], retiring.source)
            self.assertFalse(retiring.source.released)
            self.assertEqual(len(_ManualInotify.instances), 1)
            rejected, _ = await self.connect(sha256(b"project-b").hexdigest())
            self.assertTrue((await rejected.read()).startswith(b"HTTP/1.1 503 "))
        second_notice, third_notice = await asyncio.gather(second_ready, third_ready)
        self.assertEqual(second_notice, third_notice)
        self.assertIsNot(retiring.service.sources[self.project], retiring.source)
        self.assertEqual(len(retiring.service.sources), 1)
        self.assertEqual(len(_ManualInotify.instances), 2)
        self.assertTrue(retiring.source.released)

    async def test_retirement_admission_deadline_does_not_cancel_owned_cleanup(
        self,
    ) -> None:
        async with self.retiring_project() as retiring:
            reader, _ = await self.connect()
            response = asyncio.create_task(reader.read())
            self.addAsyncCleanup(_settle, response)
            await self.waits_for_retirement(retiring, response)
            cleanup = retiring.source.closing
            assert cleanup is not None
            (await retiring.deadlines.active(11)).expire()
            self.assertTrue((await response).startswith(b"HTTP/1.1 503 "))
            self.assertFalse(cleanup.cancelled())
            self.assertFalse(cleanup.done())
            self.assertIs(retiring.service.sources[self.project], retiring.source)
            self.assertFalse(retiring.source.released)
        self.assertTrue(retiring.source.released)
        self.assertEqual(retiring.service.sources, {})
        self.assertEqual(len(_ManualInotify.instances), 1)

    async def test_cancelled_retirement_admission_leaves_cleanup_owned(self) -> None:
        async with self.retiring_project() as retiring:
            previous = set(retiring.service.clients)
            reader, _ = await self.connect()
            response = asyncio.create_task(reader.read())
            self.addAsyncCleanup(_settle, response)
            await self.waits_for_retirement(retiring, response)
            pending = retiring.service.clients - previous
            self.assertEqual(len(pending), 1)
            request = pending.pop()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertEqual(await response, b"")
            cleanup = retiring.source.closing
            assert cleanup is not None
            self.assertFalse(cleanup.cancelled())
            self.assertFalse(cleanup.done())
            self.assertIs(retiring.service.sources[self.project], retiring.source)
        self.assertEqual(retiring.service.sources, {})
        self.assertEqual(len(_ManualInotify.instances), 1)

    async def _reopen_after_deletion(self, *, complete: bool) -> None:
        async with self.retiring_project() as retiring:
            reader, _ = await self.connect()
            response = asyncio.create_task(reader.read())
            self.addAsyncCleanup(_settle, response)
            await self.waits_for_retirement(retiring, response)
            deleting = self.registry.begin_delete(self.project)
            assert deleting is not None and deleting.deletion_id is not None
            if complete:
                self.registry.finish_delete(
                    self.project,
                    deleting.incarnation,
                    deleting.deletion_id,
                    deleting.sessions,
                )
        expected = b"HTTP/1.1 404 " if complete else b"HTTP/1.1 409 "
        self.assertTrue((await response).startswith(expected))
        self.assertTrue(retiring.source.released)
        record = self.registry.begin_delete(self.project)
        assert record is not None
        self.assertEqual(record.phase, "deleted" if complete else "deleting")

    async def test_deleting_project_is_rechecked_after_retirement(self) -> None:
        await self._reopen_after_deletion(complete=False)

    async def test_deleted_project_is_not_created_after_retirement(self) -> None:
        await self._reopen_after_deletion(complete=True)

    async def test_shared_project_survives_one_client_disconnect(self) -> None:
        service = await self.start()
        first, first_writer = await self.connect()
        first_ready = await self.ready(first)
        first_task = next(iter(service.clients))
        second, second_writer = await self.connect()
        self.assertEqual(await self.ready(second), first_ready)
        self.assertEqual(len(service.sources), 1)
        self.assertEqual(len(_ManualInotify.instances), 1)
        source = service.sources[self.project]
        await self.close_writer(first_writer)
        await first_task
        self.assertFalse(source.stopping.is_set())
        backend = _ManualInotify.instances[-1]
        backend.emit(
            (
                watch._Event(
                    backend.descriptor_for("files"), watch._MODIFY, b"private-name"
                ),
            )
        )
        self.assertEqual(json.loads(await second.readline()), {"type": "changed"})
        final_task = next(iter(service.clients))
        await self.close_writer(second_writer)
        await final_task
        self.assertTrue(source.completed.done())
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(service.sources, {})
        self.assertEqual(service.budget.used, 0)

    async def test_missing_and_deleting_have_explicit_http_status(self) -> None:
        await self.start()
        missing, _ = await self.connect(sha256(b"missing").hexdigest())
        self.assertTrue((await missing.read()).startswith(b"HTTP/1.1 404 "))
        self.registry.begin_delete(self.project)
        deleting, _ = await self.connect()
        self.assertTrue((await deleting.read()).startswith(b"HTTP/1.1 409 "))

    async def test_project_capacity_does_not_close_an_active_source(self) -> None:
        service = await self.start(replace(watch._DEFAULT_LIMITS, projects=1))
        first, _ = await self.connect()
        await self.ready(first)
        rejected, _ = await self.connect(sha256(b"another").hexdigest())
        self.assertTrue((await rejected.read()).startswith(b"HTTP/1.1 503 "))
        self.assertFalse(service.sources[self.project].stopping.is_set())

    async def test_connection_capacity_returns_503(self) -> None:
        await self.start(replace(watch._DEFAULT_LIMITS, clients=1))
        first, _ = await self.connect()
        await self.ready(first)
        rejected, writer = await asyncio.open_connection(*self.address)
        self.addAsyncCleanup(self.close_writer, writer)
        self.assertTrue((await rejected.read()).startswith(b"HTTP/1.1 503 "))

    async def test_cancellation_joins_active_workers_and_closes_streams(self) -> None:
        service = await self.start()
        reader, _ = await self.connect()
        await self.ready(reader)
        source = service.sources[self.project]
        await self.stop()
        self.assertEqual(await reader.read(), b"")
        self.assertTrue(source.completed.done())
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(source.stop_reader.fileno(), -1)

    async def test_thread_start_failure_keeps_another_project_live(self) -> None:
        service = await self.start()
        first, _ = await self.connect()
        await self.ready(first)
        first_source = service.sources[self.project]
        first_backend = _ManualInotify.instances[-1]
        original = watch._Source.start
        failed: list[watch._Source] = []

        def start(source: watch._Source) -> None:
            self.assertIs(service.sources[source.project], source)
            self.assertTrue(source.listeners)
            failed.append(source)
            with patch.object(
                source.thread, "start", side_effect=RuntimeError("no thread")
            ):
                original(source)

        with patch.object(watch._Source, "start", start):
            rejected, _ = await self.connect(sha256(b"project-b").hexdigest())
            self.assertTrue((await rejected.read()).startswith(b"HTTP/1.1 503 "))
        await failed[0].aclose()
        self.assertEqual(failed[0].stop_reader.fileno(), -1)
        self.assertFalse(first_source.stopping.is_set())
        first_backend.emit(
            (
                watch._Event(
                    first_backend.descriptor_for("files"), watch._MODIFY, b"item"
                ),
            )
        )
        self.assertEqual(json.loads(await first.readline()), {"type": "changed"})

    async def test_header_deadline_rejects_without_creating_a_source(self) -> None:
        deadlines = _Deadlines()
        limits = replace(
            watch._DEFAULT_LIMITS, request_timeout=11, write_timeout=22, heartbeat=33
        )
        with patch.object(watch, "asyncio", deadlines.module()):
            service = await self.start(limits)
            reader, writer = await asyncio.open_connection(*self.address)
            self.addAsyncCleanup(self.close_writer, writer)
            deadline = await deadlines.active(11)
            request = next(iter(service.clients))
            deadline.expire()
            self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 503 "))
            await request
            self.assertEqual(service.sources, {})
            await self.stop()

    async def test_ready_deadline_cancels_scan_and_releases_project_slot(self) -> None:
        deadlines = _Deadlines()
        limits = replace(
            watch._DEFAULT_LIMITS, request_timeout=11, write_timeout=22, heartbeat=33
        )
        arming = asyncio.Event()
        loop = asyncio.get_running_loop()

        def arm(tree: watch._Tree) -> watch._Effects:
            loop.call_soon_threadsafe(arming.set)
            tree.stopping.wait()
            raise watch._Stopped()

        with (
            patch.object(watch, "asyncio", deadlines.module()),
            patch.object(watch._Tree, "arm", arm),
        ):
            service = await self.start(limits)
            reader, _ = await self.connect()
            await arming.wait()
            source = service.sources[self.project]
            request = next(iter(service.clients))
            (await deadlines.active(11)).expire()
            self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 503 "))
            await request
            self.assertFalse(source.thread.is_alive())
            self.assertEqual(source.stop_reader.fileno(), -1)
            self.assertEqual(service.sources, {})
            await self.stop()

    async def test_blocked_write_deadline_closes_stream_and_joins_source(self) -> None:
        deadlines = _Deadlines()
        limits = replace(
            watch._DEFAULT_LIMITS, request_timeout=11, write_timeout=22, heartbeat=33
        )
        loop = asyncio.get_running_loop()
        original = loop.sock_sendall
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        async def send(endpoint: socket.socket, payload: bytes) -> None:
            if payload == b'{"type":"changed"}\n':
                try:
                    blocked.set()
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                await original(endpoint, payload)

        with (
            patch.object(watch, "asyncio", deadlines.module()),
            patch.object(loop, "sock_sendall", send),
        ):
            service = await self.start(limits)
            reader, _ = await self.connect()
            await self.ready(reader)
            request = next(iter(service.clients))
            source = service.sources[self.project]
            backend = _ManualInotify.instances[-1]
            backend.emit(
                (watch._Event(backend.descriptor_for("files"), watch._MODIFY, b"item"),)
            )
            await blocked.wait()
            (await deadlines.active(22)).expire()
            self.assertEqual(await reader.read(), b"")
            await request
            self.assertTrue(cancelled.is_set())
            self.assertFalse(source.thread.is_alive())
            self.assertEqual(source.stop_reader.fileno(), -1)
            self.assertEqual(service.sources, {})
            await self.stop()

    async def test_heartbeat_deadline_emits_only_a_liveness_notice(self) -> None:
        deadlines = _Deadlines()
        limits = replace(
            watch._DEFAULT_LIMITS, request_timeout=11, write_timeout=22, heartbeat=33
        )
        with patch.object(watch, "asyncio", deadlines.module()):
            service = await self.start(limits)
            reader, _ = await self.connect()
            await self.ready(reader)
            (await deadlines.active(33)).expire()
            self.assertEqual(json.loads(await reader.readline()), {"type": "heartbeat"})
            self.assertFalse(service.sources[self.project].stopping.is_set())
            await self.stop()

    async def test_bootstrap_retirement_keeps_slot_when_new_threads_are_unavailable(
        self,
    ) -> None:
        service = await self.start()
        reader, writer = await self.connect()
        await self.ready(reader)
        source = service.sources[self.project]
        request = next(iter(service.clients))
        original_join = source.thread.join
        native_exit = asyncio.get_running_loop().run_in_executor(None, original_join)
        joining = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            self.assertEqual(delay, watch._JOIN_INTERVAL)
            joining.set()
            await release.wait()

        with patch.object(
            watch,
            "asyncio",
            SimpleNamespace(**{**vars(watch.asyncio), "sleep": sleep}),
        ):
            try:
                with patch.object(
                    threading.Thread,
                    "start",
                    side_effect=RuntimeError("no cleanup thread"),
                ) as starting:
                    with _bootstrap_lock():
                        await self.close_writer(writer)
                        checking = asyncio.create_task(joining.wait())
                        try:
                            done, _ = await asyncio.wait(
                                (checking, request), return_when=asyncio.FIRST_COMPLETED
                            )
                            if request in done:
                                request.result()
                                self.fail(
                                    "request released a thread inside interpreter retirement"
                                )
                        finally:
                            checking.cancel()
                            await asyncio.gather(checking, return_exceptions=True)
                        self.assertTrue(source.completed.done())
                        self.assertTrue(source.thread.is_alive())
                        self.assertIs(service.sources[self.project], source)
                        self.assertFalse(source.released)
                        self.assertGreaterEqual(source.stop_reader.fileno(), 0)
                        self.assertGreaterEqual(source.stop_writer.fileno(), 0)
                    await native_exit
                    release.set()
                    await request
                    starting.assert_not_called()
            finally:
                source._stop()
                await native_exit
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        await request
        self.assertTrue(source.released)
        self.assertFalse(source.thread.is_alive())
        self.assertEqual(service.sources, {})
        self.assertEqual(source.stop_reader.fileno(), -1)
        self.assertEqual(source.stop_writer.fileno(), -1)
        self.assertEqual(service.budget.used, 0)


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_observer_failure_does_not_stop_parent_operation(self) -> None:
        failed = asyncio.Event()
        parent_started = asyncio.Event()
        finish = asyncio.Event()

        async def unavailable() -> None:
            failed.set()
            raise RuntimeError("private failure detail")

        async def parent() -> None:
            parent_started.set()
            await finish.wait()

        with (
            patch.object(supervise, "_watch_files", unavailable),
            patch.object(supervise, "print") as printed,
        ):
            task = asyncio.create_task(supervise._with_observation(parent()))
            try:
                await failed.wait()
                await parent_started.wait()
                self.assertFalse(task.done())
                finish.set()
                await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        printed.assert_called_once_with(
            "Workspace file observation is unavailable", file=sys.stderr, flush=True
        )

    async def test_parent_cancellation_waits_for_collector_cleanup(self) -> None:
        observing = asyncio.Event()
        released = asyncio.Event()

        async def observe() -> None:
            try:
                observing.set()
                await asyncio.Event().wait()
            finally:
                released.set()

        async def parent() -> None:
            await asyncio.Event().wait()

        with patch.object(supervise, "_watch_files", observe):
            task = asyncio.create_task(supervise._with_observation(parent()))
            await observing.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(released.is_set())


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux inotify integration")
class LinuxInotifyIntegrationTest(unittest.TestCase):
    """Verify real kernel events; the owning image suite manages Docker resources."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="tinkerfin-watch-linux-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "state"
        self.registry = ProjectRegistry(self.root, uid=os.getuid(), gid=os.getgid())
        self.project = sha256(b"project-a").hexdigest()
        prepared = self.registry.prepare(self.project)
        self.record = self.registry.reserve(
            self.project, prepared.incarnation, SessionOwner(str(uuid4()), str(uuid4()))
        )
        self.files = (
            self.registry.project_path(self.project, self.record.incarnation) / "files"
        )
        self.observation = self.enterContext(
            ProjectRegistry.open_existing(self.root, self.project)
        )
        self.budget = watch._Budget(128)
        self.addCleanup(self.assert_released)

    def test_active_reservations_and_release_do_not_emit_file_changes(self) -> None:
        tree = self.tree()
        owner = SessionOwner(str(uuid4()), str(uuid4()))
        self.registry.reserve(self.project, self.record.incarnation, owner)
        self.assertEqual(self.effects(tree), watch._Effects())
        self.observation.validate()
        self.registry.release(self.project, self.record.incarnation, owner)
        self.assertEqual(self.effects(tree), watch._Effects())
        self.observation.validate()
        self.assertEqual(list(self.files.iterdir()), [])

    def assert_released(self) -> None:
        self.assertEqual(self.budget.used, 0)

    def tree(self) -> watch._Tree:
        tree = watch._Tree(
            self.observation, self.budget, watch._DEFAULT_LIMITS, threading.Event()
        )
        self.addCleanup(tree.close)
        tree.arm()
        return tree

    def effects(self, tree: watch._Tree) -> watch._Effects:
        events = tree.inotify.read()
        self.assertTrue(events)
        return watch._effects(events, tree.inotify.watches, self.project)

    def test_file_write_truncate_rename_unlink_and_directory_creation(self) -> None:
        nested = self.files / "nested"
        nested.mkdir()
        tree = self.tree()
        target = nested / "file"
        target.write_bytes(b"original")
        self.assertTrue(self.effects(tree).changed)
        with target.open("ab") as destination:
            destination.write(b"append")
        self.assertTrue(self.effects(tree).changed)
        os.truncate(target, 1)
        self.assertTrue(self.effects(tree).changed)
        renamed = nested / "renamed"
        target.rename(renamed)
        self.assertTrue(self.effects(tree).changed)
        renamed.unlink()
        self.assertTrue(self.effects(tree).changed)
        (nested / "child").mkdir()
        self.assertEqual(self.effects(tree).resync, "topology")

    def test_moved_directories_rebuild_only_inside_the_fixed_root(self) -> None:
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"outside")
        tree = self.tree()
        moved = self.files / "incoming"
        outside.rename(moved)
        self.assertEqual(self.effects(tree).resync, "topology")
        tree.close()
        tree = self.tree()
        (moved / "file").write_bytes(b"inside")
        self.assertTrue(self.effects(tree).changed)
        moved.rename(outside)
        self.assertEqual(self.effects(tree).resync, "topology")
        tree.close()
        tree = self.tree()
        (outside / "file").write_bytes(b"outside again")
        self.assertEqual(tree.inotify.read(), ())
        (self.files / "link").symlink_to(outside, target_is_directory=True)
        self.assertTrue(self.effects(tree).changed)
        tree.close()
        tree = self.tree()
        (outside / "file").write_bytes(b"never observed")
        self.assertEqual(tree.inotify.read(), ())

    def test_files_root_replacement_is_terminal_without_following_sibling(self) -> None:
        tree = self.tree()
        outside = Path(self.directory.name) / "sibling"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"sibling")
        self.files.rename(self.files.with_name("pinned"))
        self.files.symlink_to(outside, target_is_directory=True)
        self.assertTrue(self.effects(tree).closed)
        with self.assertRaises(RegistryError):
            self.observation.validate()
        (outside / "sentinel").write_bytes(b"sibling retained")
        self.assertEqual(tree.inotify.read(), ())
        self.assertEqual((outside / "sentinel").read_bytes(), b"sibling retained")

    def test_atomic_records_and_records_directory_replacement_are_observed(
        self,
    ) -> None:
        tree = self.tree()
        self.registry.begin_delete(self.project)
        events = tree.inotify.read()
        self.assertTrue(any(event.name == self.project.encode() for event in events))
        with self.assertRaises(RegistryError) as rejected:
            self.observation.validate()
        self.assertEqual(rejected.exception.reason, "busy")
        records = self.root / "records"
        records.rename(self.root / "retired-records")
        records.mkdir(mode=0o700)
        self.assertTrue(self.effects(tree).closed)
        with self.assertRaises(RegistryError):
            self.observation.validate()


if __name__ == "__main__":
    unittest.main()
