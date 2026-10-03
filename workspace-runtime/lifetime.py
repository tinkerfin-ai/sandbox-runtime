"""Bind workspace admission to one complete container run, not one process.

Docker remounts the isolation tmpfs when the container starts. An exclusive marker
there prevents restarting the supervisor within that run and forgetting revoked
access. Readiness is published only after previous Run ownership is recovered.
Changing the execd namespace alone never permits recovery or a second claim.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

DIRECTORY = Path("/var/lib/execd/isolation/workspace-control")
EGRESS_SOCKET = DIRECTORY / "egress.sock"
CONTROL_SOCKET = DIRECTORY / "control.sock"


@dataclass(frozen=True, slots=True)
class _Mount:
    root: Path
    directory: Path
    filesystem: str
    writable: bool


def _mounts() -> tuple[_Mount, ...]:
    def decoded(value: str) -> Path:
        for escaped, character in (
            (r"\040", " "),
            (r"\011", "\t"),
            (r"\012", "\n"),
            (r"\134", "\\"),
        ):
            value = value.replace(escaped, character)
        return Path(value)

    mounts: list[_Mount] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount, separator, filesystem = line.partition(" - ")
        fields = mount.split()
        filesystem_fields = filesystem.split()
        if not separator or len(fields) < 6 or not filesystem_fields:
            raise RuntimeError("Workspace mount ownership is unavailable")
        mounts.append(
            _Mount(
                decoded(fields[3]),
                decoded(fields[4]),
                filesystem_fields[0],
                "rw" in fields[5].split(","),
            )
        )
    return tuple(mounts)


def require_private_storage(root: Path) -> None:
    """Reject shared project storage before claiming any old process has ended.

    Recovery is supported only on Docker's private, writable overlay root. A
    bind mount, including one on the same device, must not cover an ancestor or
    descendant of the registry. Otherwise its sessions could belong to another
    container that is still running.
    """
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError("Workspace storage must use its canonical container path")
    mounts = _mounts()
    roots = [mount for mount in mounts if mount.directory == Path("/")]
    if (
        len(roots) != 1
        or roots[0].root != Path("/")
        or roots[0].filesystem != "overlay"
        or not roots[0].writable
    ):
        raise RuntimeError(
            "Workspace recovery requires a private writable overlay root"
        )
    for mount in mounts:
        if mount.directory != Path("/") and (
            root.is_relative_to(mount.directory) or mount.directory.is_relative_to(root)
        ):
            raise RuntimeError("External mounts must not overlap workspace storage")


def _private_directory(directory: Path) -> None:
    identity = directory.lstat()
    if (
        not stat.S_ISDIR(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) != 0o700
    ):
        raise RuntimeError("Workspace admission requires a trusted private directory")


def _require_tmpfs(directory: Path) -> None:
    root = directory.parent
    identity = root.lstat()
    if (
        not stat.S_ISDIR(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or root.resolve(strict=True) != root
    ):
        raise RuntimeError("Workspace isolation requires a trusted tmpfs mount")
    mounts = _mounts()
    parents = [mount for mount in mounts if mount.directory == root]
    if (
        len(parents) != 1
        or parents[0].filesystem != "tmpfs"
        or parents[0].root != Path("/")
        or not parents[0].writable
        or any(mount.directory.is_relative_to(directory) for mount in mounts)
    ):
        raise RuntimeError("Workspace isolation requires a private container tmpfs")


def claim(directory: Path = DIRECTORY) -> None:
    """Claim fresh container state; refuse a second supervisor even after failure."""
    _require_tmpfs(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    _private_directory(directory)
    descriptor = os.open(
        directory / "lifetime",
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    os.close(descriptor)


def publish_namespace(namespace: str, directory: Path = DIRECTORY) -> None:
    """Admit registry calls only after recovery and listener setup have succeeded."""
    if str(UUID(namespace)) != namespace:
        raise ValueError("Workspace namespace must be a canonical UUID")
    _private_directory(directory)
    descriptor = os.open(
        directory / "ready",
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(namespace.encode("ascii"))
            target.flush()
    except BaseException:
        (directory / "ready").unlink(missing_ok=True)
        raise


def current_namespace(directory: Path = DIRECTORY) -> str:
    """Read the fixed execd identity without accepting unready or untrusted state."""
    _private_directory(directory)
    descriptor = os.open(
        directory / "ready",
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
    )
    try:
        identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.geteuid()
            or stat.S_IMODE(identity.st_mode) != 0o600
            or identity.st_nlink != 1
        ):
            raise RuntimeError("Workspace readiness is not exclusively owned")
        namespace = os.read(descriptor, 37).decode("ascii")
        if len(namespace) != 36 or str(UUID(namespace)) != namespace:
            raise ValueError("Workspace readiness has an invalid namespace")
        return namespace
    finally:
        os.close(descriptor)
