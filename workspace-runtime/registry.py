"""Keep project admission and session ownership inside one trusted sandbox.

Only the trusted parent invokes this module. Workloads receive selected directory
mounts, never the registry or its ancestors. A reservation is durable before an
execd session can be requested. Deletion seals admission, then its caller cancels
every recorded session and confirms its namespace and file operations have ended.
Only that caller may finish deletion. An execd instance mismatch is not proof of
cleanup and must leave the project sealed with its files intact.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, cast
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifetime import EGRESS_SOCKET as _EGRESS_SOCKET
from lifetime import current_namespace

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_STAGED_RECORD = re.compile(r"record-[a-z0-9_]+\.pending\Z")
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_RETIRED_SESSIONS = 65536
_SYSTEM_MOUNTS = (
    "/usr",
    "/opt/sandbox-runtime",
    "/etc/ssl",
    "/etc/fonts",
    "/etc/ld.so.cache",
)


class RegistryError(Exception):
    """Reject an operation without discarding uncertain session ownership."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["busy", "stale", "unavailable"] = "unavailable",
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SessionOwner:
    """Identify a request before it can create an execd session."""

    session_id: str
    session_namespace: str

    def __post_init__(self) -> None:
        _uuid(self.session_id)
        _uuid(self.session_namespace)

    def payload(self) -> dict[str, str]:
        """Return the exact native ownership fields."""
        return {
            "session_id": self.session_id,
            "session_namespace": self.session_namespace,
        }


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """Persist only container-scoped admission and cleanup ownership."""

    incarnation: str
    phase: Literal["active", "deleting", "deleted"]
    sessions: tuple[SessionOwner, ...]
    deletion_id: str | None = None

    def payload(self) -> dict[str, object]:
        """Return the single current on-disk representation."""
        return {
            "incarnation": self.incarnation,
            "phase": self.phase,
            "sessions": [session.payload() for session in self.sessions],
            "deletion_id": self.deletion_id,
        }


def _uuid(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("identity must be a canonical UUID")
    return value


def _mapping(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RegistryError("invalid project ownership record")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise RegistryError("invalid project ownership identity")
    return value


def _parse_record(value: object) -> ProjectRecord:
    record = _mapping(value, {"incarnation", "phase", "sessions", "deletion_id"})
    incarnation = _uuid(_text(record["incarnation"]))
    phase = record["phase"]
    if phase not in ("active", "deleting", "deleted"):
        raise RegistryError("invalid project admission state")
    owners = record["sessions"]
    if not isinstance(owners, list):
        raise RegistryError("invalid project sessions")
    sessions: list[SessionOwner] = []
    for owner in cast(list[object], owners):
        fields = _mapping(owner, {"session_id", "session_namespace"})
        sessions.append(
            SessionOwner(
                _text(fields["session_id"]), _text(fields["session_namespace"])
            )
        )
    if len({session.session_id for session in sessions}) != len(sessions):
        raise RegistryError("duplicate project session identity")
    deletion_id = record["deletion_id"]
    if deletion_id is not None:
        deletion_id = _uuid(_text(deletion_id))
    if (phase == "active") != (deletion_id is None):
        raise RegistryError("invalid project deletion identity")
    if phase == "deleted" and sessions:
        raise RegistryError("deleted project still owns sessions")
    return ProjectRecord(
        incarnation,
        cast(Literal["active", "deleting", "deleted"], phase),
        tuple(sessions),
        deletion_id,
    )


def _read_record(descriptor: int, *, allow_unlinked: bool = False) -> ProjectRecord:
    """Read an owned record, optionally retaining a replaced observation snapshot.

    Args:
        descriptor: Open record descriptor whose ownership transfers here.
        allow_unlinked: Accept an immutable snapshot detached by atomic replacement.
            Observers recheck current records after arming and on registry events;
            locked admission operations continue to require a linked record.

    Returns:
        The validated record from this fixed descriptor.
    """
    minimum_links = 0 if allow_unlinked else 1
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not minimum_links <= info.st_nlink <= 1
        ):
            raise RegistryError("workspace record is not exclusively owned")
        content = source.read(_MAX_RECORD_BYTES + 1)
    if len(content) > _MAX_RECORD_BYTES:
        raise RegistryError("workspace ownership record exceeds its capacity")
    return _parse_record(json.loads(content))


class ProjectObservation:
    """Borrow fixed directory descriptors while observing an existing project.

    The open_existing context owns every descriptor. Consumers must stop using
    them before leaving that context. Validation reads only trusted metadata and
    verifies each original directory entry; it never creates admission or follows
    a replacement root. No registry lock is retained while observing files.
    """

    def __init__(
        self,
        project: str,
        incarnation: str,
        records: int,
        files: int,
        entries: tuple[tuple[int, str, int], ...],
    ) -> None:
        self.project = project
        self.incarnation = incarnation
        self.records = records
        self.files = files
        self.entries = entries

    def validate(self) -> None:
        """Reject deletion, another incarnation, or any replaced directory."""
        for parent, name, descriptor in self.entries:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            original = os.fstat(descriptor)
            if not stat.S_ISDIR(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (original.st_dev, original.st_ino):
                raise RegistryError(
                    "workspace observation root changed", reason="stale"
                )
        record = _read_record(
            os.open(
                self.project,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.records,
            ),
            allow_unlinked=True,
        )
        if record.phase == "deleting":
            raise RegistryError("workspace deletion is in progress", reason="busy")
        if record.phase != "active" or record.incarnation != self.incarnation:
            raise RegistryError(
                "workspace observation no longer exists", reason="stale"
            )


class ProjectRegistry:
    """Serialize short local state transitions without holding locks over execd I/O.

    The root and lock files must be owned by the invoking trusted parent. Lock
    inodes are never removed or replaced, including after project deletion. File
    trees are removed only after admission is sealed and native cleanup has been
    confirmed. A native DELETE or the supervisor's exact restart receipt supplies
    that proof; an uncertain result must never permit finish_delete.
    The current session namespace comes from trusted execd admission and fences
    delayed requests; it never proves that an older session has stopped.
    """

    def __init__(
        self, root: Path, *, uid: int, gid: int, session_namespace: str
    ) -> None:
        self.root = root
        self.uid = uid
        self.gid = gid
        self.session_namespace = _uuid(session_namespace)
        if uid < 0 or gid < 0:
            raise ValueError("workspace user and group must not be negative")
        self._trusted_directory(root)
        for directory in ("locks", "records", "projects", "cancelled", "staging"):
            self._trusted_directory(root / directory)

    @staticmethod
    def _trusted_directory(path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RegistryError(
                "workspace registry requires trusted private directories"
            )

    @staticmethod
    @contextmanager
    def _existing_directory(
        path: str | Path, *, parent: int | None = None, uid: int | None = None
    ) -> Iterator[int]:
        """Pin trusted storage, including directories not yet handed to the user.

        Startup may have stopped between mkdir and chown. The trusted parent is
        therefore a valid owner until that handoff; unrelated owners never are.
        """
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            expected_uid = os.geteuid() if uid is None else uid
            if info.st_uid not in {expected_uid, os.geteuid()} or (
                uid is None and stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise RegistryError("workspace cleanup directory is not trusted")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self, project: str) -> Iterator[None]:
        if _DIGEST.fullmatch(project) is None:
            raise ValueError("project must be a lowercase SHA-256 identity")
        descriptor = os.open(
            self.root / "locks" / project,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RegistryError("workspace lock is not exclusively owned")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _read(self, project: str) -> ProjectRecord | None:
        try:
            descriptor = os.open(
                self.root / "records" / project,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            )
        except FileNotFoundError:
            return None
        return _read_record(descriptor)

    @staticmethod
    @contextmanager
    def open_existing(root: Path, project: str) -> Iterator[ProjectObservation]:
        """Open an active file root without creating or reserving any resource.

        Args:
            root: Trusted registry location in the parent container.
            project: Lowercase SHA-256 project identity.

        Yields:
            Borrowed descriptors valid only inside this context. All traversal
            starts from these descriptors, including after a directory rename.

        Raises:
            FileNotFoundError: Registry, active record, or project files are absent.
            RegistryError: Deletion, a replaced root, or invalid ownership prevents
                observation. A deleting record has reason busy; deleted is stale.
            OSError: A directory cannot be opened without following a symlink.
        """
        if _DIGEST.fullmatch(project) is None:
            raise ValueError("project must be a lowercase SHA-256 identity")
        if not root.name:
            raise ValueError("workspace registry must be a named directory")
        with ExitStack() as ownership:

            def directory(name: str | Path, parent: int | None = None) -> int:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent,
                )
                ownership.callback(os.close, descriptor)
                return descriptor

            parent = directory(root.parent)
            registry = directory(root.name, parent)
            records = directory("records", registry)
            for descriptor in (registry, records):
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise RegistryError("workspace registry directory is not trusted")
            record = _read_record(
                os.open(
                    project,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=records,
                ),
                allow_unlinked=True,
            )
            if record.phase == "deleting":
                raise RegistryError("workspace deletion is in progress", reason="busy")
            if record.phase != "active":
                raise RegistryError(
                    "workspace observation no longer exists", reason="stale"
                )
            projects = directory("projects", registry)
            project_root = directory(project, projects)
            incarnation = directory(record.incarnation, project_root)
            for descriptor in (projects, project_root, incarnation):
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise RegistryError("workspace project directory is not trusted")
            files = directory("files", incarnation)
            observed = ProjectObservation(
                project,
                record.incarnation,
                records,
                files,
                (
                    (parent, root.name, registry),
                    (registry, "records", records),
                    (registry, "projects", projects),
                    (projects, project, project_root),
                    (project_root, record.incarnation, incarnation),
                    (incarnation, "files", files),
                ),
            )
            observed.validate()
            yield observed

    def _cancellation_path(self, project: str, owner: SessionOwner) -> Path:
        return (
            self.root
            / "cancelled"
            / f"{project}.{owner.session_namespace}.{owner.session_id}"
        )

    def _is_cancelled(self, project: str, owner: SessionOwner) -> bool:
        try:
            info = self._cancellation_path(project, owner).lstat()
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise RegistryError("workspace cancellation is not exclusively owned")
        return True

    def _cancel(self, project: str, owner: SessionOwner) -> None:
        if self._is_cancelled(project, owner):
            return
        descriptor = os.open(
            self._cancellation_path(project, owner),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            self.root / "cancelled", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _write(self, project: str, record: ProjectRecord) -> None:
        content = json.dumps(record.payload(), separators=(",", ":")).encode()
        if len(content) > _MAX_RECORD_BYTES:
            raise RegistryError("workspace ownership record exceeds its capacity")
        directory = self.root / "records"
        descriptor, temporary = tempfile.mkstemp(
            prefix="record-", suffix=".pending", dir=self.root / "staging"
        )
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, directory / project)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def project_path(self, project: str, incarnation: str) -> Path:
        """Return a trusted incarnation path, never a caller-supplied file path."""
        if _DIGEST.fullmatch(project) is None:
            raise ValueError("project must be a lowercase SHA-256 identity")
        _uuid(incarnation)
        return self.root / "projects" / project / incarnation

    def _directories(
        self, project: str, record: ProjectRecord, owner: SessionOwner
    ) -> None:
        project_root = self.root / "projects" / project
        self._trusted_directory(project_root)
        incarnation = self.project_path(project, record.incarnation)
        self._trusted_directory(incarnation)
        for name in ("files", "home", "cache", "dependencies"):
            directory = incarnation / name
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {
                    self.uid,
                    os.geteuid(),
                }:
                    raise RegistryError("workspace data root changed ownership")
                if info.st_uid != self.uid:
                    with self._existing_directory(directory) as descriptor:
                        if os.listdir(descriptor):
                            raise RegistryError(
                                "unfinished workspace data root is not empty"
                            )
                        os.fchown(descriptor, self.uid, self.gid)
            else:
                os.chown(directory, self.uid, self.gid)
        runs = incarnation / "runs"
        self._trusted_directory(runs)
        self._trusted_directory(runs / owner.session_namespace)
        self._trusted_directory(runs / owner.session_namespace / owner.session_id)

    def prepare(self, project: str) -> ProjectRecord:
        """Select an incarnation without creating project data or native resources.

        A subsequent reservation must carry this identity. Deletion therefore
        fences a delayed reservation even when its caller died before it could
        send an explicit cancellation. A lost prepare response has no workload
        or data-directory ownership to recover.
        """
        with self._locked(project):
            record = self._read(project)
            if record is not None and record.phase == "deleting":
                raise RegistryError("workspace deletion is in progress", reason="busy")
            if record is None or record.phase == "deleted":
                record = ProjectRecord(str(uuid4()), "active", ())
                self._write(project, record)
            return record

    def reserve(
        self, project: str, incarnation: str, owner: SessionOwner
    ) -> ProjectRecord:
        """Record one request only within its previously acknowledged incarnation."""
        if owner.session_namespace != self.session_namespace:
            raise RegistryError("workspace session namespace is stale", reason="stale")
        _uuid(incarnation)
        with self._locked(project):
            if self._is_cancelled(project, owner):
                raise RegistryError(
                    "workspace reservation has been cancelled", reason="stale"
                )
            record = self._read(project)
            if (
                record is None
                or record.incarnation != incarnation
                or record.phase != "active"
            ):
                raise RegistryError(
                    "workspace incarnation no longer admits reservations",
                    reason="stale",
                )
            for existing in record.sessions:
                if existing.session_id == owner.session_id:
                    if existing != owner:
                        raise RegistryError(
                            "session identity belongs to another execd instance"
                        )
                    self._directories(project, record, owner)
                    return record
            record = ProjectRecord(
                record.incarnation, "active", (*record.sessions, owner)
            )
            self._write(project, record)
            self._directories(project, record, owner)
            return record

    def cancel(self, project: str, owner: SessionOwner) -> ProjectRecord | None:
        """Fence a late reservation and locate it after a lost reserve response.

        This operation does not claim that the native session has stopped. Its
        cancellation record survives deletion and prevents replay from recreating
        an incarnation. The trusted caller must separately confirm execd deletion
        before releasing a found reservation or deleting any project data.
        """
        with self._locked(project):
            self._cancel(project, owner)
            record = self._read(project)
            return record if record is not None and owner in record.sessions else None

    def session_request(
        self, project: str, incarnation: str, owner: SessionOwner
    ) -> dict[str, object]:
        """Prepare a minimal mount view while project deletion admission is locked.

        This performs only a fixed amount of local filesystem setup, never an
        execd or network call. Each source ancestor is parent-owned and excluded
        from the workload view. After this method returns, native cancellation
        of the reserved identity fences a delayed session POST.
        """
        if owner.session_namespace != self.session_namespace:
            raise RegistryError("workspace session namespace is stale", reason="stale")
        _uuid(incarnation)
        with self._locked(project):
            record = self._read(project)
            if (
                record is None
                or record.incarnation != incarnation
                or record.phase != "active"
                or owner not in record.sessions
                or self._is_cancelled(project, owner)
            ):
                raise RegistryError(
                    "workspace reservation is not active", reason="stale"
                )
            root = self.project_path(project, incarnation)
            run = root / "runs" / owner.session_namespace / owner.session_id
            self._trusted_directory(run)
            view = run / "view"
            view.mkdir(mode=0o755, exist_ok=True)
            mounts: list[dict[str, object]] = [
                {"source": str(view), "dest": "/", "readonly": True}
            ]
            for name in (
                "proc",
                "tmp",
                "run",
                "dev",
                "etc",
                "home/workspace",
                "workspace",
                "cache",
                "dependencies",
            ):
                (view / name).mkdir(mode=0o755, parents=True, exist_ok=True)
            for source_name in _SYSTEM_MOUNTS:
                source = Path(source_name)
                if not source.exists():
                    raise RegistryError("workspace runtime toolchain is incomplete")
                destination = view / source_name.lstrip("/")
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                if source.is_dir():
                    destination.mkdir(mode=0o755, exist_ok=True)
                else:
                    destination.touch(exist_ok=True)
                mounts.append(
                    {"source": source_name, "dest": source_name, "readonly": True}
                )
            for alias in ("bin", "sbin", "lib", "lib64"):
                destination = view / alias
                if Path(f"/{alias}").exists() and not destination.is_symlink():
                    destination.symlink_to(f"usr/{alias}")
            (view / "var").mkdir(mode=0o755, exist_ok=True)
            if not (view / "var" / "tmp").is_symlink():
                (view / "var" / "tmp").symlink_to("/tmp")
            (view / "etc" / "passwd").write_text(
                f"root:x:0:0:Root:/root:/bin/bash\nworkspace:x:{self.uid}:{self.gid}:Workspace:/home/workspace:/bin/bash\n"
            )
            (view / "etc" / "group").write_text(f"root:x:0:\nworkspace:x:{self.gid}:\n")
            (view / "etc" / "hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
            (view / "etc" / "resolv.conf").write_text("")
            for source_name, destination in (
                ("files", "/workspace"),
                ("home", "/home/workspace"),
                ("cache", "/cache"),
                ("dependencies", "/dependencies"),
            ):
                mounts.append({"source": str(root / source_name), "dest": destination})
            files = root / "files"
            (view / str(files).lstrip("/")).mkdir(
                mode=0o755, parents=True, exist_ok=True
            )
            mounts.append({"source": str(files), "dest": str(files)})
            for source_name, destination in (
                ("tmp", "/tmp"),
                ("run", "/run"),
            ):
                source = run / source_name
                source.mkdir(mode=0o700, exist_ok=True)
                os.chown(source, self.uid, self.gid)
                mounts.append({"source": str(source), "dest": destination})
            if not stat.S_ISSOCK(_EGRESS_SOCKET.stat().st_mode):
                raise RegistryError("workspace egress proxy is unavailable")
            (run / "run" / "egress.sock").touch(exist_ok=True)
            mounts.append(
                {
                    "source": str(_EGRESS_SOCKET),
                    "dest": "/run/egress.sock",
                    "readonly": True,
                }
            )
            return {
                **owner.payload(),
                "workspace": {"path": str(files), "mode": "rw"},
                "profile": "strict",
                "share_net": False,
                "uid_mode": "setpriv",
                "uid": self.uid,
                "gid": self.gid,
                "idle_timeout_seconds": 0,
                "env_passthrough": {"mode": "allow", "keys": []},
                "binds": mounts,
            }

    def _remove_session_data(
        self, project: str, incarnation: str, owner: SessionOwner
    ) -> None:
        with ExitStack() as ownership:

            def optional_directory(
                name: str, parent: int, *, uid: int | None = None
            ) -> int | None:
                try:
                    return ownership.enter_context(
                        self._existing_directory(name, parent=parent, uid=uid)
                    )
                except FileNotFoundError:
                    return None

            registry = ownership.enter_context(self._existing_directory(self.root))
            projects = ownership.enter_context(
                self._existing_directory("projects", parent=registry)
            )
            project_root = optional_directory(project, projects)
            if project_root is None:
                return
            root = optional_directory(incarnation, project_root)
            if root is None:
                return
            trees: list[tuple[int, str]] = []
            runs = optional_directory("runs", root)
            if runs is not None:
                namespace = optional_directory(owner.session_namespace, runs)
                if namespace is not None:
                    run = optional_directory(owner.session_id, namespace)
                    if run is not None:
                        trees.append((namespace, owner.session_id))
            dependencies = optional_directory("dependencies", root, uid=self.uid)
            temporary_name = f".python-{owner.session_id}"
            temporary_link = False
            if dependencies is not None:
                try:
                    info = os.stat(
                        temporary_name, dir_fd=dependencies, follow_symlinks=False
                    )
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISLNK(info.st_mode):
                        temporary_link = True
                    else:
                        ownership.enter_context(
                            self._existing_directory(
                                temporary_name, parent=dependencies, uid=self.uid
                            )
                        )
                        trees.append((dependencies, temporary_name))
            for parent, name in trees:
                self._remove_tree(name, parent=parent)
            if temporary_link and dependencies is not None:
                os.unlink(temporary_name, dir_fd=dependencies)

    @staticmethod
    def _discard_staged_records(directory: int) -> None:
        """Remove only unfinished record writes after all previous writers ended."""
        for name in os.listdir(directory):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                _STAGED_RECORD.fullmatch(name) is None
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise RegistryError("unfinished workspace record is not trusted")
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)

    def retire_sessions(self) -> tuple[SessionOwner, ...]:
        """Confirm recorded sessions ended at a proven complete container restart.

        Only the startup supervisor may call this before publishing admission,
        after its exclusive container lifetime claim proves all former processes
        ended. A different execd namespace alone is not such proof. Records remain
        unchanged until their existing callers finish release or deletion, so a
        failed startup never discards ownership needed to retry cleanup.

        Returns:
            The exact, deduplicated stopped identities for this supervisor to own.

        Raises:
            RegistryError: A current session, invalid record or exceeded capacity
                makes recovery uncertain. Admission must remain closed on failure.
            OSError: A directory is untrusted or cleanup cannot be completed.
            ValueError: A stored ownership identity or JSON record is invalid.
        """
        records: list[tuple[str, ProjectRecord]] = []
        retired: dict[SessionOwner, None] = {}
        with ExitStack() as ownership:
            registry = ownership.enter_context(self._existing_directory(self.root))
            directories = {
                name: ownership.enter_context(
                    self._existing_directory(name, parent=registry)
                )
                for name in ("locks", "records", "projects", "cancelled", "staging")
            }
            self._discard_staged_records(directories["staging"])
            for project in sorted(os.listdir(directories["records"])):
                if _DIGEST.fullmatch(project) is None:
                    raise RegistryError("invalid workspace record entry")
                with self._locked(project):
                    record = self._read(project)
                    if record is None:
                        raise RegistryError("workspace ownership record disappeared")
                for owner in record.sessions:
                    if owner.session_namespace == self.session_namespace:
                        raise RegistryError("current workspace session is not stopped")
                    retired[owner] = None
                    if len(retired) > _MAX_RETIRED_SESSIONS:
                        raise RegistryError("workspace recovery exceeds its capacity")
                if record.sessions:
                    records.append((project, record))
            for project, record in records:
                with self._locked(project):
                    if self._read(project) != record:
                        raise RegistryError(
                            "workspace ownership changed during recovery"
                        )
                    for owner in record.sessions:
                        self._cancel(project, owner)
                for owner in record.sessions:
                    self._remove_session_data(project, record.incarnation, owner)
        return tuple(retired)

    def release(self, project: str, incarnation: str, owner: SessionOwner) -> None:
        """Forget a reservation only after its native session has completely ended."""
        _uuid(incarnation)
        with self._locked(project):
            self._cancel(project, owner)
            record = self._read(project)
            if record is None or record.incarnation != incarnation:
                return
            if owner not in record.sessions or record.phase != "active":
                return
        self._remove_session_data(project, incarnation, owner)
        with self._locked(project):
            record = self._read(project)
            if (
                record is None
                or record.incarnation != incarnation
                or record.phase != "active"
            ):
                return
            self._write(
                project,
                ProjectRecord(
                    incarnation,
                    "active",
                    tuple(item for item in record.sessions if item != owner),
                ),
            )

    def begin_delete(self, project: str) -> ProjectRecord | None:
        """Seal admission and return all native requests that must be cancelled."""
        with self._locked(project):
            record = self._read(project)
            if record is None or record.phase in ("deleting", "deleted"):
                return record
            deleting = ProjectRecord(
                record.incarnation, "deleting", record.sessions, str(uuid4())
            )
            self._write(project, deleting)
            return deleting

    def finish_delete(
        self,
        project: str,
        incarnation: str,
        deletion_id: str,
        confirmed: tuple[SessionOwner, ...],
    ) -> None:
        """Remove only the sealed incarnation after all its session barriers succeed."""
        _uuid(incarnation)
        _uuid(deletion_id)
        with self._locked(project):
            record = self._read(project)
            if (
                record is None
                or record.incarnation != incarnation
                or record.deletion_id != deletion_id
            ):
                raise RegistryError(
                    "workspace deletion no longer owns this incarnation"
                )
            if record.phase == "deleted":
                return
            if record.phase != "deleting" or set(record.sessions) != set(confirmed):
                raise RegistryError("workspace sessions are not confirmed stopped")
            for owner in record.sessions:
                self._cancel(project, owner)
        self._remove_tree(self.project_path(project, incarnation))
        with self._locked(project):
            current = self._read(project)
            if (
                current is None
                or current.incarnation != incarnation
                or current.deletion_id != deletion_id
            ):
                raise RegistryError(
                    "workspace deletion no longer owns this incarnation"
                )
            self._write(project, ProjectRecord(incarnation, "deleted", (), deletion_id))

    @staticmethod
    def _remove_tree(path: str | Path, *, parent: int | None = None) -> None:
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RegistryError("directory removal requires descriptor-safe rmtree")

        def ignore_removed_entry(
            operation: Callable[..., object],
            entry: str,
            failure: tuple[type[BaseException], BaseException, TracebackType | None],
        ) -> None:
            del operation, entry
            if not isinstance(failure[1], FileNotFoundError):
                raise failure[1]

        shutil.rmtree(path, onerror=ignore_removed_entry, dir_fd=parent)


def main() -> None:
    """Process one trusted parent's bounded request, never arbitrary filesystem paths."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path("/var/lib/tinkerfin-workspaces")
    )
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--gid", type=int, default=1000)
    arguments = parser.parse_args()
    content = sys.stdin.buffer.read(_MAX_RECORD_BYTES + 1)
    if len(content) > _MAX_RECORD_BYTES:
        raise RegistryError("workspace request exceeds its capacity")
    request = _mapping(json.loads(content), {"operation", "project", "arguments"})
    operation = _text(request["operation"])
    project = _text(request["project"])
    try:
        session_namespace = current_namespace()
    except RuntimeError:
        raise RegistryError("workspace admission is unavailable") from None
    registry = ProjectRegistry(
        arguments.root,
        uid=arguments.uid,
        gid=arguments.gid,
        session_namespace=session_namespace,
    )
    payload = request["arguments"]
    result: ProjectRecord | None = None
    if operation == "session_request":
        fields = _mapping(payload, {"incarnation", "session_id", "session_namespace"})
        owner = SessionOwner(
            _text(fields["session_id"]), _text(fields["session_namespace"])
        )
        print(
            json.dumps(
                registry.session_request(project, _text(fields["incarnation"]), owner),
                separators=(",", ":"),
            )
        )
        return
    if operation == "prepare":
        _mapping(payload, set())
        result = registry.prepare(project)
    elif operation == "reserve":
        fields = _mapping(payload, {"incarnation", "session_id", "session_namespace"})
        result = registry.reserve(
            project,
            _text(fields["incarnation"]),
            SessionOwner(
                _text(fields["session_id"]), _text(fields["session_namespace"])
            ),
        )
    elif operation == "cancel":
        fields = _mapping(payload, {"session_id", "session_namespace"})
        result = registry.cancel(
            project,
            SessionOwner(
                _text(fields["session_id"]), _text(fields["session_namespace"])
            ),
        )
    elif operation == "release":
        fields = _mapping(payload, {"incarnation", "session_id", "session_namespace"})
        registry.release(
            project,
            _text(fields["incarnation"]),
            SessionOwner(
                _text(fields["session_id"]), _text(fields["session_namespace"])
            ),
        )
    elif operation == "begin_delete":
        _mapping(payload, set())
        result = registry.begin_delete(project)
    elif operation == "finish_delete":
        fields = _mapping(payload, {"incarnation", "deletion_id", "confirmed"})
        sessions = fields["confirmed"]
        if not isinstance(sessions, list):
            raise RegistryError("invalid cleanup confirmations")
        confirmed: list[SessionOwner] = []
        for session in cast(list[object], sessions):
            owner = _mapping(session, {"session_id", "session_namespace"})
            confirmed.append(
                SessionOwner(
                    _text(owner["session_id"]), _text(owner["session_namespace"])
                )
            )
        registry.finish_delete(
            project,
            _text(fields["incarnation"]),
            _text(fields["deletion_id"]),
            tuple(confirmed),
        )
    else:
        raise RegistryError("unknown workspace operation")
    print(
        json.dumps(
            result.payload() if result is not None else None, separators=(",", ":")
        )
    )


if __name__ == "__main__":
    try:
        main()
    except RegistryError as error:
        print(json.dumps({"error": {"reason": error.reason, "message": str(error)}}))
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print(
            json.dumps(
                {
                    "error": {
                        "reason": "unavailable",
                        "message": "workspace registry could not complete the request",
                    }
                }
            )
        )
        raise SystemExit(1) from None
