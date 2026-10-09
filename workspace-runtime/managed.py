"""Publish managed project directories only while all workload writers are absent.

The registry's project lock protects admission, not workload writes. Maintenance
therefore seals new reservations and requires an empty session set before any
file comparison or publication. Files remain writable after admission resumes.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

if TYPE_CHECKING:
    from registry import ProjectRegistry

MAX_FILES = 8192
MAX_ENTRIES = 16384
MAX_BYTES = 512 * 1024 * 1024


class ManagedDirectoryError(Exception):
    """Reject a directory operation without releasing uncertain ownership."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["busy", "stale", "unavailable", "changed"] = "unavailable",
    ) -> None:
        super().__init__(message)
        self.reason = reason


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("invalid relative file path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("file path must remain inside its directory")
    return value


def directory(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("directory must be a virtual absolute path")
    return "/" + relative(value[1:])


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def manifest(value: object) -> dict[str, str]:
    items = mapping(value)
    if len(items) > MAX_FILES:
        raise ValueError("directory exceeds file capacity")
    result: dict[str, str] = {}
    for path, digest in items.items():
        relative(path)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid content digest")
        result[path] = digest
    return result


def write_record(path: Path, value: object) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="managed-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_record(path: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ManagedDirectoryError("directory ownership record is not trusted")
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ManagedDirectoryError("directory ownership record is too large")
    return mapping(json.loads(data))


def tree(root: Path) -> dict[str, str]:
    """Hash regular files without following links or traversing special entries."""
    if not root.exists() and not root.is_symlink():
        return {}
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ManagedDirectoryError("managed target is not a directory")
    result: dict[str, str] = {}
    total = 0
    nodes = 0
    pending = [root]
    while pending:
        parent = pending.pop()
        for child in parent.iterdir():
            nodes += 1
            if nodes > MAX_ENTRIES:
                raise ManagedDirectoryError("directory exceeds node capacity")
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_BYTES or len(result) >= MAX_FILES:
                    raise ManagedDirectoryError(
                        "directory exceeds safe inspection capacity"
                    )
                digest = hashlib.sha256()
                descriptor = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                with os.fdopen(descriptor, "rb") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                result[child.relative_to(root).as_posix()] = digest.hexdigest()
            else:
                raise ManagedDirectoryError(
                    "managed directory contains a link or special file"
                )
    return result


def exchange(left: Path, right: Path) -> None:
    """Swap complete directory entries atomically on the Linux runtime."""
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameat2", None)
    if operation is None:
        raise ManagedDirectoryError("atomic directory exchange is unavailable")
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), "atomic directory publication failed")


class ManagedDirectories:
    """Own one maintenance operation and its private staging on a project."""

    def __init__(self, registry: ProjectRegistry) -> None:
        self.registry = registry
        for name in (
            "maintenance",
            "managed",
            "directory-staging",
            "maintenance-locks",
            "maintenance-cancelled",
        ):
            registry._trusted_directory(registry.root / name)

    @contextmanager
    def operation(self, project: str) -> Iterator[None]:
        lock = self.registry.root / "maintenance-locks" / project
        with self.registry._locked(project):
            descriptor = os.open(
                lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise ManagedDirectoryError("maintenance lock is not trusted")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ManagedDirectoryError(
                    "maintenance operation is still running", reason="busy"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def cancelled(self, operation_id: str) -> Path:
        if str(UUID(operation_id)) != operation_id:
            raise ValueError("maintenance identity must be a canonical UUID")
        return self.registry.root / "maintenance-cancelled" / operation_id

    def target(
        self, record: dict[str, object], path: str, *, create_parents: bool = False
    ) -> Path:
        root = Path(cast(str, record["root"]))
        if path == "/":
            return root
        parts = directory(path)[1:].split("/")
        target = root
        for index, part in enumerate(parts):
            target /= part
            if target.is_symlink():
                raise ManagedDirectoryError("maintenance never follows symbolic links")
            if target.exists():
                if index < len(parts) - 1 and not stat.S_ISDIR(target.lstat().st_mode):
                    raise ManagedDirectoryError(
                        "maintenance path crosses a non-directory"
                    )
            elif index < len(parts) - 1:
                if not create_parents:
                    raise FileNotFoundError(path)
                target.mkdir(mode=0o755)
                os.chown(target, self.registry.uid, self.registry.gid)
        return target

    def marker(self, project: str) -> Path:
        return self.registry.root / "maintenance" / project

    def baseline(self, project: str, path: str) -> Path:
        root = self.registry.root / "managed" / project
        self.registry._trusted_directory(root)
        return root / hashlib.sha256(path.encode()).hexdigest()

    def require_idle(self, project: str) -> None:
        if self.marker(project).exists():
            raise ManagedDirectoryError(
                "workspace maintenance is in progress", reason="busy"
            )

    def begin(
        self,
        project: str,
        operation_id: str,
        path: str | None,
        desired: dict[str, str] | None,
    ) -> dict[str, object]:
        if str(UUID(operation_id)) != operation_id:
            raise ValueError("maintenance identity must be a canonical UUID")
        registry = self.registry
        with registry._locked(project):
            if self.cancelled(operation_id).exists():
                raise ManagedDirectoryError(
                    "maintenance request was cancelled", reason="stale"
                )
            current = read_record(self.marker(project))
            if current is not None:
                if current.get("operation_id") == operation_id:
                    return current
                raise ManagedDirectoryError(
                    "workspace maintenance is in progress", reason="busy"
                )
            record = registry._read(project)
            if record is None or record.phase != "active":
                raise ManagedDirectoryError(
                    "workspace is not initialized", reason="stale"
                )
            baseline = (
                None if path is None else read_record(self.baseline(project, path))
            )
            if (
                desired is not None
                and baseline is not None
                and baseline.get("incarnation") == record.incarnation
                and baseline.get("files") == desired
            ):
                return {"status": "unchanged"}
            if record.sessions:
                raise ManagedDirectoryError("workspace is in use", reason="busy")
            files = registry.project_path(project, record.incarnation) / "files"
            if not files.is_dir():
                if desired is None:
                    raise ManagedDirectoryError(
                        "workspace files are not initialized", reason="stale"
                    )
                registry._data_directories(project, record)
            stage = registry.root / "directory-staging" / operation_id
            result: dict[str, object] = {
                "status": "prepared",
                "operation_id": operation_id,
                "incarnation": record.incarnation,
                "stage": str(stage),
                "root": str(files),
                "path": path,
                "files": desired,
            }
            # Publish ownership before creating files, so interruption at any
            # later point leaves an exact identity that end can safely settle.
            write_record(self.marker(project), result)
            registry._trusted_directory(stage)
            (stage / "upload").mkdir(mode=0o700, exist_ok=True)
            return result

    def owned(self, project: str, operation_id: str) -> dict[str, object]:
        record = read_record(self.marker(project))
        if record is None or record.get("operation_id") != operation_id:
            raise ManagedDirectoryError(
                "maintenance identity is no longer current", reason="stale"
            )
        current = self.registry._read(project)
        if (
            current is None
            or current.incarnation != record["incarnation"]
            or current.phase != "active"
            or current.sessions
        ):
            raise ManagedDirectoryError(
                "workspace maintenance lost exclusive ownership", reason="stale"
            )
        return record

    def commit(self, project: str, operation_id: str) -> dict[str, object]:
        with self.operation(project):
            return self._commit(project, operation_id)

    def _commit(self, project: str, operation_id: str) -> dict[str, object]:
        with self.registry._locked(project):
            record = self.owned(project, operation_id)
        if record["status"] == "published":
            return {"status": "published"}
        if record["status"] != "prepared":
            raise ManagedDirectoryError("directory publication needs recovery")
        path = directory(record["path"])
        desired = manifest(record["files"])
        stage = Path(cast(str, record["stage"]))
        uploaded = tree(stage / "upload")
        if uploaded != desired:
            raise ManagedDirectoryError(
                "uploaded directory does not match its declaration"
            )
        target = self.target(record, path, create_parents=True)
        current = tree(target)
        saved = read_record(self.baseline(project, path))
        previous = (
            {}
            if saved is None or saved.get("incarnation") != record["incarnation"]
            else manifest(saved["files"])
        )
        conflicts = [
            name
            for name in previous
            if previous[name] != desired.get(name)
            and current.get(name) != previous[name]
        ]
        conflicts.extend(
            name
            for name in desired
            if name not in previous
            and name in current
            and current[name] != desired[name]
        )
        if conflicts:
            raise ManagedDirectoryError(
                "managed files have local modifications", reason="changed"
            )
        candidate = stage / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)
        if target.exists():
            shutil.copytree(target, candidate)
        else:
            candidate.mkdir()
        obsolete_parents: set[Path] = set()
        for name in previous.keys() - desired.keys():
            (candidate / name).unlink(missing_ok=True)
            obsolete_parents.update(
                parent for parent in Path(name).parents if parent != Path(".")
            )
        # Only prune ancestors of removed managed files. Unrelated empty
        # directories and directories containing user files remain untouched.
        for parent in sorted(
            obsolete_parents, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                (candidate / parent).rmdir()
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno != errno.ENOTEMPTY:
                    raise
        for name, digest in desired.items():
            if previous.get(name) == digest:
                continue
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / "upload" / name, destination)
        for parent, dirs, file_names in os.walk(candidate):
            os.chown(parent, self.registry.uid, self.registry.gid)
            for name in file_names:
                os.chown(Path(parent) / name, self.registry.uid, self.registry.gid)
        existed = target.exists()
        record.update(
            status="publishing",
            existed=existed,
            candidate_inode=candidate.stat().st_ino,
        )
        write_record(self.marker(project), record)
        if existed:
            exchange(candidate, target)
        else:
            os.rename(candidate, target)
        write_record(
            self.baseline(project, path),
            {"incarnation": record["incarnation"], "files": desired},
        )
        record["status"] = "published"
        write_record(self.marker(project), record)
        return {"status": "published"}

    def end(self, project: str, operation_id: str) -> None:
        with self.operation(project):
            self._end(project, operation_id)

    def _end(self, project: str, operation_id: str) -> None:
        with self.registry._locked(project):
            write_record(self.cancelled(operation_id), {"project": project})
            record = read_record(self.marker(project))
            if record is None or record.get("operation_id") != operation_id:
                return
            record = self.owned(project, operation_id)
        if record["status"] == "publishing":
            # A lost publication response must not expose a partial transaction.
            path = directory(record["path"])
            target = self.target(record, path)
            candidate = Path(cast(str, record["stage"])) / "candidate"
            if target.exists() and target.stat().st_ino == record["candidate_inode"]:
                write_record(
                    self.baseline(project, path),
                    {
                        "incarnation": record["incarnation"],
                        "files": manifest(record["files"]),
                    },
                )
            elif (
                not candidate.exists()
                or candidate.stat().st_ino != record["candidate_inode"]
            ):
                raise ManagedDirectoryError(
                    "directory publication outcome is uncertain"
                )
            record["status"] = "settled"
            write_record(self.marker(project), record)
        stage = Path(cast(str, record["stage"]))
        if stage.exists():
            shutil.rmtree(stage)
        with self.registry._locked(project):
            self.owned(project, operation_id)
            self.marker(project).unlink()

    def dispatch(self, project: str, operation: str, arguments: object) -> object:
        args = mapping(arguments)
        operation_id = args.get("operation_id")
        if not isinstance(operation_id, str):
            raise TypeError("maintenance identity is required")
        if operation == "managed_begin":
            prepared = self.begin(
                project,
                operation_id,
                directory(args.get("path")),
                manifest(args.get("files")),
            )
            # Clients need ownership and staging, not another copy of the manifest.
            return {
                key: value
                for key, value in prepared.items()
                if key not in {"files", "path"}
            }
        if operation == "maintenance_begin":
            return self.begin(project, operation_id, None, None)
        if operation == "managed_commit":
            return self.commit(project, operation_id)
        if operation == "maintenance_end":
            self.end(project, operation_id)
            return None
        raise ValueError("unknown maintenance operation")
