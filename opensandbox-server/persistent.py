"""Keep retained Docker sandboxes restartable without overriding explicit pause.

The upstream synchronous lifecycle routes run in Starlette's bounded thread pool.
Docker calls retain the service's configured API timeout. An accepted request may
outlive a disconnected HTTP caller; its durable pause intent remains authoritative.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from docker import DockerClient
from docker.models.containers import Container
from fastapi import HTTPException
from opensandbox_server.services.docker.networking import EGRESS_SIDECAR_LABEL

_RETAINED_LABEL = "opensandbox.io/manual-cleanup"


class PersistentSandboxLifecycle:
    """Borrow the Docker client and serialize retained-instance pause transitions.

    Intent files describe explicit pause only. Missing intent never authorizes a
    start: Docker's unless-stopped policy owns automatic recovery and manual-stop
    detection. Empty lock files retain their inode so concurrent workers cannot
    acquire different locks for the same sandbox after an intent is removed.
    """

    def __init__(self, client: DockerClient, metadata_root: Path) -> None:
        self._client = client
        self._root = metadata_root / "_lifecycle"

    @staticmethod
    def retained(container: Container) -> bool:
        """Return whether the provider requires explicit instance destruction."""
        assert container.attrs is not None
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        return labels.get(_RETAINED_LABEL) == "true"

    def _path(self, sandbox_id: str) -> Path:
        return self._root / f"{sha256(sandbox_id.encode()).hexdigest()}.json"

    def is_paused(self, sandbox_id: str) -> bool:
        """Read durable pause intent, refusing corrupt or unreadable evidence."""
        try:
            payload = json.loads(self._path(sandbox_id).read_text())
        except FileNotFoundError:
            return False
        if payload != {"sandbox_id": sandbox_id, "desired_state": "paused"}:
            raise RuntimeError("Sandbox pause intent is invalid")
        return True

    @contextmanager
    def _locked(self, sandbox_id: str, *, reconcile: bool = False) -> Iterator[bool]:
        self._root.mkdir(parents=True, exist_ok=True)
        with self._path(sandbox_id).with_suffix(".lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                if reconcile:
                    # The active route owns this instance's transition. Startup
                    # must not compete with it or fail unrelated service startup.
                    yield False
                    return
                raise HTTPException(
                    409,
                    detail={
                        "code": "SANDBOX_LIFECYCLE_BUSY",
                        "message": "Sandbox lifecycle operation is already in progress.",
                    },
                ) from error
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _sync_directory(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _save_pause(self, sandbox_id: str) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode="w", dir=self._root, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"sandbox_id": sandbox_id, "desired_state": "paused"}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path(sandbox_id))
            self._sync_directory()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _companions(self, sandbox_id: str) -> list[Container]:
        # Docker SDK ContainerCollection.model fixes the result type to Container.
        containers = cast(
            list[Container],
            self._client.containers.list(
                all=True, filters={"label": f"{EGRESS_SIDECAR_LABEL}={sandbox_id}"}
            ),
        )
        for container in containers:
            assert container.attrs is not None
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            if labels.get(EGRESS_SIDECAR_LABEL) != sandbox_id:
                raise RuntimeError("Sandbox companion ownership is invalid")
        return containers

    def pause(self, sandbox_id: str, container: Container) -> None:
        """Disable automatic startup before durably accepting an explicit pause."""
        with self._locked(sandbox_id):
            container.reload()
            assert container.attrs is not None
            if not container.attrs.get("State", {}).get("Running"):
                raise HTTPException(409, detail={"code": "SANDBOX_NOT_RUNNING"})
            # Updating policy before committing intent leaves a conservative stopped
            # resource if the process dies at this cross-system boundary.
            for member in [container, *self._companions(sandbox_id)]:
                member.update(restart_policy={"Name": "no"})
            self._save_pause(sandbox_id)
            if not container.attrs.get("State", {}).get("Paused"):
                container.pause()

    def resume(self, sandbox_id: str, container: Container) -> None:
        """Resume only a paused instance; an exited instance requires saved intent."""
        with self._locked(sandbox_id):
            container.reload()
            assert container.attrs is not None
            state = container.attrs.get("State", {})
            if not state.get("Paused") and not self.is_paused(sandbox_id):
                raise HTTPException(409, detail={"code": "SANDBOX_NOT_PAUSED"})
            self._save_pause(sandbox_id)
            companions = self._companions(sandbox_id)
            for companion in companions:
                companion.reload()
                assert companion.attrs is not None
                if not companion.attrs.get("State", {}).get("Running"):
                    companion.start()
            if state.get("Paused"):
                container.unpause()
            elif not state.get("Running"):
                container.start()
            for member in [*companions, container]:
                member.update(restart_policy={"Name": "unless-stopped"})
            self._path(sandbox_id).unlink()
            self._sync_directory()

    def restore_pause(self, sandbox_id: str, container: Container) -> None:
        """Finish recorded pause work at startup without starting any container."""
        if not self.retained(container) or not self.is_paused(sandbox_id):
            return
        with self._locked(sandbox_id, reconcile=True) as acquired:
            if not acquired or not self.is_paused(sandbox_id):
                return
            container.reload()
            assert container.attrs is not None
            for member in [container, *self._companions(sandbox_id)]:
                member.update(restart_policy={"Name": "no"})
            state = container.attrs.get("State", {})
            if state.get("Running") and not state.get("Paused"):
                container.pause()
