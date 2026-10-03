"""Observe existing project files through a private parent-container HTTP stream.

Linux inotify reports ordinary file writes, truncation and directory operations,
including writes made through bind mounts. It does not cover mmap writes or new
mounts. Events are coalescible root-level hints, never a file-operation journal.
Only the authenticated execd proxy may expose this loopback listener to a host.
Workloads have private networking and cannot reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import json
import os
import re
import select
import socket
import struct
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from registry import ProjectObservation, ProjectRegistry, RegistryError

_MODIFY = 0x00000002
_ATTRIB = 0x00000004
_CLOSE_WRITE = 0x00000008
_MOVED_FROM = 0x00000040
_MOVED_TO = 0x00000080
_CREATE = 0x00000100
_DELETE = 0x00000200
_DELETE_SELF = 0x00000400
_MOVE_SELF = 0x00000800
_UNMOUNT = 0x00002000
_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_ONLYDIR = 0x01000000
_ISDIR = 0x40000000
_SELF = _DELETE_SELF | _MOVE_SELF | _UNMOUNT | _IGNORED
_TOPOLOGY = _MOVED_FROM | _MOVED_TO | _CREATE | _DELETE
_MUTATIONS = _MODIFY | _ATTRIB | _CLOSE_WRITE | _TOPOLOGY
_MASK = _MUTATIONS | _SELF | _ONLYDIR
_EVENT = struct.Struct("=iIII")
_REQUEST = re.compile(rb"GET /projects/([a-f0-9]{64})/changes HTTP/1\.[01]\Z")
_REGISTRY = Path("/var/lib/tinkerfin-workspaces")
_JOIN_INTERVAL = 0.05

Reason = Literal["overflow", "topology", "source_closed"]
Kind = Literal["files", "directory", "records", "ancestor"]


@dataclass(frozen=True, slots=True)
class Limits:
    """Bound total ownership, recursive work and stream buffering.

    Directory limits include the trusted ancestor watches. Scanning owns at most
    2 * depth + 8 directory descriptors per project and examines at most entries
    entries per rebuild. Each project owns one dedicated worker, one inotify FD
    and a stop socket pair; subscribers each retain at most three notices.
    """

    clients: int = 64
    projects: int = 32
    directories: int = 4096
    total_directories: int = 32768
    entries: int = 65536
    depth: int = 64
    headers: int = 8192
    request_timeout: float = 15
    write_timeout: float = 15
    heartbeat: float = 15
    initial_batches: int = 16


_DEFAULT_LIMITS = Limits()


class Unavailable(Exception):
    """Expose only a safe HTTP status when observation cannot be established."""

    def __init__(self, status: int = 503) -> None:
        self.status = status
        super().__init__(status)


class _Stopped(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Event:
    descriptor: int
    mask: int
    name: bytes


def _events(content: bytes) -> tuple[_Event, ...]:
    offset = 0
    parsed: list[_Event] = []
    while offset < len(content):
        if len(content) - offset < _EVENT.size:
            raise ValueError("incomplete inotify event")
        descriptor, mask, _, length = _EVENT.unpack_from(content, offset)
        offset += _EVENT.size
        if length > 256 or offset + length > len(content):
            raise ValueError("invalid inotify event length")
        name = content[offset : offset + length]
        if name:
            end = name.find(b"\0")
            if end < 0 or any(name[end:]) or b"/" in name[:end]:
                raise ValueError("invalid inotify entry name")
            name = name[:end]
        parsed.append(_Event(descriptor, mask, name))
        offset += length
    return tuple(parsed)


class _Budget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            if self.used >= self.maximum:
                raise Unavailable()
            self.used += 1

    def release(self, count: int) -> None:
        with self.lock:
            self.used -= count


class _Inotify:
    """Own kernel watches without resolving mutable project path strings."""

    def __init__(self, budget: _Budget, maximum: int) -> None:
        if not sys.platform.startswith("linux"):
            raise Unavailable()
        self.library = ctypes.CDLL(None, use_errno=True)
        self.library.inotify_init1.argtypes = [ctypes.c_int]
        self.library.inotify_init1.restype = ctypes.c_int
        self.library.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.library.inotify_add_watch.restype = ctypes.c_int
        descriptor = self.library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "inotify unavailable")
        self.descriptor: int = descriptor
        self.budget = budget
        self.maximum = maximum
        self.watches: dict[int, Kind] = {}

    def add(self, directory: int, kind: Kind) -> None:
        if len(self.watches) >= self.maximum:
            raise Unavailable()
        self.budget.acquire()
        descriptor = self.library.inotify_add_watch(
            self.descriptor, f"/proc/self/fd/{directory}".encode(), _MASK
        )
        if descriptor < 0 or descriptor in self.watches:
            self.budget.release(1)
            raise OSError(
                ctypes.get_errno() or errno.EINVAL, "inotify watch unavailable"
            )
        self.watches[descriptor] = kind

    def read(self) -> tuple[_Event, ...]:
        try:
            return _events(os.read(self.descriptor, 65536))
        except BlockingIOError:
            return ()

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
            self.budget.release(len(self.watches))
            self.watches.clear()


@dataclass(frozen=True, slots=True)
class _Effects:
    changed: bool = False
    resync: Reason | None = None
    closed: bool = False


def _effects(
    events: tuple[_Event, ...],
    watches: dict[int, Kind],
    project: str,
) -> _Effects:
    changed = False
    reason: Reason | None = None
    closed = False
    for event in events:
        if event.mask & _OVERFLOW or event.descriptor not in watches:
            reason = "overflow"
            continue
        kind = watches[event.descriptor]
        if event.mask & _SELF:
            if kind != "directory":
                closed = True
            elif reason is None:
                reason = "topology"
        if kind == "records":
            if event.name == project.encode() and event.mask & (
                _CREATE | _DELETE | _MOVED_FROM
            ):
                closed = True
            continue
        if kind == "ancestor":
            continue
        if event.mask & _ISDIR and event.mask & _TOPOLOGY and reason is None:
            reason = "topology"
        if event.mask & _MUTATIONS:
            changed = True
    return _Effects(changed, reason, closed)


class _Tree:
    """Arm each directory before listing it and retain only the original root."""

    def __init__(
        self,
        observed: ProjectObservation,
        budget: _Budget,
        limits: Limits,
        stopping: threading.Event,
    ) -> None:
        self.observed = observed
        self.limits = limits
        self.stopping = stopping
        self.inotify = _Inotify(budget, limits.directories)
        self.entries = 0
        self.visited: set[tuple[int, int]] = set()

    def arm(self) -> _Effects:
        descriptors = {
            entry[2]
            for entry in self.observed.entries
            if entry[2] != self.observed.files
        }
        for descriptor in descriptors:
            self.inotify.add(
                descriptor,
                "records" if descriptor == self.observed.records else "ancestor",
            )
        self._scan(self.observed.files, 0)
        self.observed.validate()
        changed = False
        for _ in range(self.limits.initial_batches):
            events = self.inotify.read()
            effects = _effects(events, self.inotify.watches, self.observed.project)
            if effects.resync is not None or effects.closed:
                raise Unavailable()
            changed |= effects.changed
            if not events:
                self.observed.validate()
                return _Effects(changed)
        raise Unavailable()

    def _scan(self, directory: int, depth: int) -> None:
        if self.stopping.is_set():
            raise _Stopped()
        if depth > self.limits.depth:
            raise Unavailable()
        identity = os.fstat(directory)
        key = identity.st_dev, identity.st_ino
        if key in self.visited:
            raise Unavailable()
        self.visited.add(key)
        self.inotify.add(directory, "files" if depth == 0 else "directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                if self.stopping.is_set():
                    raise _Stopped()
                self.entries += 1
                if self.entries > self.limits.entries:
                    raise Unavailable()
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child = os.open(
                    entry.name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    self._scan(child, depth + 1)
                finally:
                    os.close(child)

    def close(self) -> None:
        self.inotify.close()


class _Notices:
    """Merge hints into at most resync, changed and closed, in that order."""

    def __init__(self) -> None:
        self.pending: deque[dict[str, str]] = deque()
        self.available = asyncio.Event()
        self.closed = False

    def put(self, kind: str, reason: Reason | None = None) -> None:
        if self.closed:
            return
        if kind == "closed":
            self.closed = True
        elif kind == "resync":
            self.pending.clear()
        elif any(notice["type"] == kind for notice in self.pending):
            return
        notice = {"type": kind}
        if reason is not None:
            notice["reason"] = reason
        self.pending.append(notice)
        self.available.set()

    async def get(self) -> dict[str, str]:
        while not self.pending:
            await self.available.wait()
        notice = self.pending.popleft()
        if not self.pending:
            self.available.clear()
        return notice


@dataclass(frozen=True, slots=True)
class _Opened:
    status: int
    incarnation: str | None = None


class _Source:
    """Own one dedicated worker and coalesce its thread-to-loop handoff.

    The worker performs all registry and directory I/O. Cancellation signals its
    socket pair and is checked during traversal. A single filesystem syscall
    cannot be interrupted; the parent uses only local filesystem roots. Shutdown
    awaits the worker before closing the stop sockets or releasing its capacity.
    The caller registers ownership before start. One cleanup task retains that
    slot through completion and nonblocking joins, including cancellation. When
    the interpreter is still retiring a thread, a short asynchronous wait avoids
    spinning without allocating another thread to finish resource cleanup.
    """

    def __init__(
        self,
        root: Path,
        project: str,
        budget: _Budget,
        limits: Limits,
    ) -> None:
        self.root = root
        self.project = project
        self.budget = budget
        self.limits = limits
        self.loop = asyncio.get_running_loop()
        self.opened: asyncio.Future[_Opened] = self.loop.create_future()
        self.completed: asyncio.Future[None] = self.loop.create_future()
        self.closing: asyncio.Task[None] | None = None
        self.listeners: set[_Notices] = set()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.pending: dict[str, _Opened | Reason | None] = {}
        self.scheduled = False
        self.closed = False
        self.released = False
        self.start_requested = False
        self.thread = threading.Thread(target=self._thread_main, name="workspace-watch")
        self.stop_reader, self.stop_writer = socket.socketpair()
        self.stop_writer.setblocking(False)

    def start(self) -> None:
        """Start only after the caller owns this source and its project slot."""
        if self.start_requested or self.stopping.is_set():
            raise Unavailable()
        self.start_requested = True
        try:
            self.thread.start()
        except RuntimeError as error:
            self._stop()
            if self.thread.ident is None:
                self._complete()
            self._post("ready", _Opened(503))
            self._post("closed")
            raise Unavailable() from error

    def _thread_main(self) -> None:
        try:
            with contextlib.suppress(Unavailable):
                self._run()
        finally:
            self.loop.call_soon_threadsafe(self._complete)

    def _complete(self) -> None:
        if not self.completed.done():
            self.completed.set_result(None)

    def _post(self, kind: str, value: _Opened | Reason | None = None) -> None:
        with self.lock:
            self.pending[kind] = value
            if not self.scheduled:
                self.scheduled = True
                self.loop.call_soon_threadsafe(self._flush)

    def _flush(self) -> None:
        with self.lock:
            pending = self.pending
            self.pending = {}
            self.scheduled = False
        opened = pending.get("ready")
        if isinstance(opened, _Opened) and not self.opened.done():
            self.opened.set_result(opened)
        reason = pending.get("resync")
        if isinstance(reason, str):
            for listener in self.listeners:
                listener.put("resync", reason)
        if "changed" in pending:
            for listener in self.listeners:
                listener.put("changed")
        if "closed" in pending:
            self.closed = True
            for listener in self.listeners:
                listener.put("closed")

    def _run(self) -> None:
        ready = False
        rejected = 503
        try:
            with ProjectRegistry.open_existing(self.root, self.project) as observed:
                rebuild: Reason | None = None
                while not self.stopping.is_set():
                    tree = _Tree(observed, self.budget, self.limits, self.stopping)
                    try:
                        initial = tree.arm()
                        if not ready:
                            self._post("ready", _Opened(200, observed.incarnation))
                            ready = True
                        if rebuild is not None:
                            self._post("resync", rebuild)
                        if initial.changed:
                            self._post("changed")
                        while not self.stopping.is_set():
                            readable, _, _ = select.select(
                                [tree.inotify.descriptor, self.stop_reader], [], []
                            )
                            if self.stop_reader in readable:
                                raise _Stopped()
                            effects = _effects(
                                tree.inotify.read(), tree.inotify.watches, self.project
                            )
                            observed.validate()
                            if effects.closed:
                                raise Unavailable()
                            if effects.resync is not None:
                                rebuild = effects.resync
                                break
                            if effects.changed:
                                self._post("changed")
                    finally:
                        tree.close()
        except _Stopped:
            pass
        except FileNotFoundError:
            rejected = 404
        except RegistryError as error:
            rejected = {"busy": 409, "stale": 404}.get(error.reason, 503)
        except Exception as error:
            raise Unavailable() from error
        finally:
            if not ready:
                self._post("ready", _Opened(rejected))
            elif not self.stopping.is_set():
                self._post("resync", "source_closed")
            self._post("closed")

    def _stop(self) -> None:
        if not self.stopping.is_set():
            self.stopping.set()
            with contextlib.suppress(OSError):
                self.stop_writer.send(b"x")

    async def _close(self) -> None:
        if not self.start_requested:
            self._complete()
        await self.completed
        if self.thread.ident is not None:
            while True:
                self.thread.join(timeout=0)
                if not self.thread.is_alive():
                    break
                await asyncio.sleep(_JOIN_INTERVAL)
        self.stop_reader.close()
        self.stop_writer.close()
        self.released = True

    async def aclose(self) -> None:
        self._stop()
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self.closing)
                break
            except asyncio.CancelledError:
                cancelled = True
                if self.closing.cancelled():
                    raise
        if cancelled:
            raise asyncio.CancelledError


class WorkspaceWatch:
    """Own bounded HTTP clients and shared project sources with dedicated threads.

    The listener is loopback-only. Callers expose it solely through the existing
    authenticated execd proxy. Normal shutdown or cancellation joins all project
    workers; collector failure never changes file-operation results.
    """

    def __init__(
        self,
        root: Path = _REGISTRY,
        *,
        limits: Limits = _DEFAULT_LIMITS,
        listener: socket.socket | None = None,
    ) -> None:
        self.root = root
        self.limits = limits
        self.source = str(uuid4())
        self.listener = listener
        self.budget = _Budget(limits.total_directories)
        self.sources: dict[str, _Source] = {}
        self.clients: set[asyncio.Task[None]] = set()

    async def serve(self) -> None:
        try:
            if self.listener is None:
                self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("127.0.0.1", 44773))
                self.listener.listen(self.limits.clients)
            self.listener.setblocking(False)
            async with asyncio.TaskGroup() as ownership:
                try:
                    while True:
                        endpoint, _ = await asyncio.get_running_loop().sock_accept(
                            self.listener
                        )
                        endpoint.setblocking(False)
                        if len(self.clients) >= self.limits.clients:
                            try:
                                await self._error(endpoint, 503)
                            finally:
                                endpoint.close()
                            continue
                        task = ownership.create_task(self._request(endpoint))
                        self.clients.add(task)
                        task.add_done_callback(self.clients.discard)
                finally:
                    self.listener.close()
                    for task in self.clients:
                        task.cancel()
        finally:
            if self.listener is not None:
                self.listener.close()
            await asyncio.gather(*(source.aclose() for source in self.sources.values()))
            self.sources.clear()

    async def _error(self, endpoint: socket.socket, status: int) -> None:
        labels = {
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
            503: "Service Unavailable",
        }
        response = (
            f"HTTP/1.1 {status} {labels[status]}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode()
        with contextlib.suppress(OSError, TimeoutError):
            async with asyncio.timeout(self.limits.write_timeout):
                await asyncio.get_running_loop().sock_sendall(endpoint, response)

    async def _request(self, endpoint: socket.socket) -> None:
        source: _Source | None = None
        notices: _Notices | None = None
        started = False
        try:
            loop = asyncio.get_running_loop()
            content = bytearray()
            async with asyncio.timeout(self.limits.request_timeout):
                while b"\r\n\r\n" not in content:
                    chunk = await loop.sock_recv(
                        endpoint, self.limits.headers + 1 - len(content)
                    )
                    if not chunk:
                        return
                    content.extend(chunk)
                    if len(content) > self.limits.headers:
                        raise Unavailable(400)
            project = _project_request(bytes(content))
            source = self.sources.get(project)
            if source is not None and (source.closed or source.stopping.is_set()):
                raise Unavailable()
            created = source is None
            if source is None:
                if len(self.sources) >= self.limits.projects:
                    raise Unavailable()
                source = _Source(self.root, project, self.budget, self.limits)
                self.sources[project] = source
            notices = _Notices()
            source.listeners.add(notices)
            if created:
                source.start()
            async with asyncio.timeout(self.limits.request_timeout):
                opened = await asyncio.shield(source.opened)
            if opened.status != 200 or opened.incarnation is None or source.closed:
                raise Unavailable(opened.status if opened.status != 200 else 503)
            async with asyncio.timeout(self.limits.write_timeout):
                await loop.sock_sendall(
                    endpoint,
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                    b"Cache-Control: no-store\r\nConnection: close\r\n\r\n",
                )
            started = True
            await self._send(
                endpoint,
                {
                    "type": "ready",
                    "source": self.source,
                    "incarnation": opened.incarnation,
                },
            )
            sender = asyncio.create_task(self._stream(endpoint, notices))
            disconnected = asyncio.create_task(loop.sock_recv(endpoint, 1))
            try:
                done, _ = await asyncio.wait(
                    (sender, disconnected), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            finally:
                sender.cancel()
                disconnected.cancel()
                await asyncio.gather(sender, disconnected, return_exceptions=True)
        except Unavailable as error:
            if not started:
                await self._error(endpoint, error.status)
        except TimeoutError:
            if not started:
                await self._error(endpoint, 503)
        except OSError:
            pass
        finally:
            endpoint.close()
            if source is not None and notices is not None:
                source.listeners.discard(notices)
                if not source.listeners:
                    try:
                        await source.aclose()
                    finally:
                        if (
                            source.released
                            and self.sources.get(source.project) is source
                        ):
                            del self.sources[source.project]

    async def _send(self, endpoint: socket.socket, notice: dict[str, str]) -> None:
        async with asyncio.timeout(self.limits.write_timeout):
            await asyncio.get_running_loop().sock_sendall(
                endpoint, json.dumps(notice, separators=(",", ":")).encode() + b"\n"
            )

    async def _stream(self, endpoint: socket.socket, notices: _Notices) -> None:
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time() + self.limits.heartbeat
        while True:
            remaining = heartbeat_at - loop.time()
            if notices.closed:
                notice = await notices.get()
            elif remaining <= 0:
                notice = {"type": "heartbeat"}
            else:
                try:
                    async with asyncio.timeout(remaining):
                        notice = await notices.get()
                except TimeoutError:
                    notice = {"type": "heartbeat"}
            await self._send(endpoint, notice)
            if notice["type"] == "closed":
                return
            if notice["type"] == "heartbeat":
                heartbeat_at = loop.time() + self.limits.heartbeat


def _project_request(content: bytes) -> str:
    lines = content.split(b"\r\n")
    if len(lines) < 3 or lines[-2:] != [b"", b""]:
        raise Unavailable(400)
    matched = _REQUEST.fullmatch(lines[0])
    if matched is None:
        raise Unavailable(404)
    seen: set[bytes] = set()
    for line in lines[1:-2]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if (
            not separator
            or not name
            or name in seen
            or any(character <= 32 or character >= 127 for character in name)
        ):
            raise Unavailable(400)
        seen.add(name)
        if name == b"transfer-encoding" or (
            name == b"content-length" and value.strip() != b"0"
        ):
            raise Unavailable(400)
    return matched.group(1).decode("ascii")
