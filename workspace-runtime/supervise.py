"""Own workspace admission and egress for one complete container run.

The control socket is never mounted into a workload. Its private parent directory
and mode restrict admission and revocation to trusted parent commands. A tmpfs
marker prevents restarting this service with an empty cancellation ledger until
the whole container has stopped. Startup retains exact old Run identities as
termination evidence; an execd namespace change alone never supplies that proof.
Egress failure terminates the entrypoint, which bootstrap couples to execd.
File observation fails independently and never terminates the parent command or
changes file-operation results.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import cast
from uuid import UUID

import lifetime
from egress import (
    DnsResolver,
    EgressProxy,
    Limits,
    Rejected,
    _ConnectionServer,
    _first_completed,
    _nameserver,
    serve_until_signal,
    unix_listener,
)
from registry import ProjectRegistry, SessionOwner
from watch import WorkspaceWatch


class WorkspaceNetwork:
    """Retain Run revocations independently of individual control connections."""

    def __init__(
        self,
        proxy: EgressProxy,
        control: socket.socket,
        *,
        session_namespace: str,
        stopped_sessions: tuple[SessionOwner, ...],
    ) -> None:
        self.proxy = proxy
        self.control = _ConnectionServer(control, 64)
        self.session_namespace = session_namespace
        self.stopped_sessions = frozenset(
            (owner.session_id, owner.session_namespace) for owner in stopped_sessions
        )

    async def serve(self) -> None:
        """Fail the service as a whole if either listener fails."""
        try:
            await _first_completed(
                self.proxy.serve(), self.control.serve(self._request)
            )
        finally:
            await self.control.aclose()
            await self.proxy.aclose()

    async def _request(self, endpoint: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(30):
                content = bytearray()
                while not content.endswith(b"\n"):
                    value = await loop.sock_recv(endpoint, 4097 - len(content))
                    if not value:
                        return
                    content.extend(value)
                    if len(content) > 4096:
                        raise ValueError("Invalid request")
                raw: object = json.loads(content)
                if not isinstance(raw, dict):
                    raise TypeError("Invalid request")
                payload = cast(dict[str, object], raw)
                operation = payload.get("operation")
                response: dict[str, str | bool]
                if operation == "status" and set(payload) == {"operation"}:
                    response = {"ready": True}
                else:
                    if set(payload) != {"operation", "session_id", "session_namespace"}:
                        raise ValueError("Invalid request")
                    session_id = payload["session_id"]
                    namespace = payload["session_namespace"]
                    if not isinstance(session_id, str) or not isinstance(
                        namespace, str
                    ):
                        raise ValueError("Invalid request")
                    if (
                        str(UUID(session_id)) != session_id
                        or str(UUID(namespace)) != namespace
                    ):
                        raise ValueError("Invalid Run identity")
                    if operation == "grant":
                        if namespace != self.session_namespace:
                            raise Rejected(403)
                        response = {
                            "token": self.proxy.grant_run_egress(session_id, namespace)
                        }
                    elif operation == "revoke":
                        await self.proxy.revoke_run_egress(session_id, namespace)
                        response = {
                            "stopped": (session_id, namespace) in self.stopped_sessions
                        }
                    else:
                        raise ValueError("Invalid request")
                await loop.sock_sendall(
                    endpoint,
                    json.dumps(response, separators=(",", ":")).encode() + b"\n",
                )
        except (TypeError, ValueError, Rejected):
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(30):
                    await loop.sock_sendall(endpoint, b'{"error":"denied"}\n')
        except (OSError, TimeoutError):
            return


async def _command(arguments: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(*arguments)
    try:
        status = await process.wait()
        if status:
            raise RuntimeError("Workspace parent command failed")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _execd_namespace(token: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", 44772, limit=8192)
    try:
        writer.write(
            (
                "GET /v1/isolated/capabilities HTTP/1.0\r\n"
                "Host: 127.0.0.1\r\nConnection: close\r\n"
                f"X-EXECD-ACCESS-TOKEN: {token}\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        response = bytearray()
        while True:
            content = await reader.read(8193 - len(response))
            if not content:
                break
            response.extend(content)
            if len(response) > 8192:
                raise RuntimeError("Execd capabilities exceed their byte limit")
        header, separator, body = response.partition(b"\r\n\r\n")
        status = header.partition(b"\r\n")[0].split()
        if (
            not separator
            or len(status) < 2
            or status[0] not in (b"HTTP/1.0", b"HTTP/1.1")
            or status[1] != b"200"
        ):
            raise RuntimeError("Execd capabilities are unavailable")
        raw: object = json.loads(body)
        if not isinstance(raw, dict):
            raise TypeError("Invalid execd capabilities")
        payload = cast(dict[str, object], raw)
        namespace = payload.get("session_namespace")
        if payload.get("available") is not True:
            raise RuntimeError("Execd isolation is unavailable")
        if not isinstance(namespace, str) or str(UUID(namespace)) != namespace:
            raise ValueError("Execd isolation requires a canonical session namespace")
        return namespace
    finally:
        writer.close()
        await writer.wait_closed()


async def _wait_execd_namespace() -> str:
    """Bound startup readiness without retrying authentication or protocol errors."""
    token = os.environ.get("EXECD_ACCESS_TOKEN", "")
    if len(token) != 64 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise ValueError("Workspace isolation requires authenticated execd access")
    async with asyncio.timeout(30):
        while True:
            try:
                return await _execd_namespace(token)
            except ConnectionRefusedError:
                await asyncio.sleep(0.05)


async def _watch_files() -> None:
    await WorkspaceWatch().serve()


def _observation_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        print("Workspace file observation is unavailable", file=sys.stderr, flush=True)


async def _with_observation(operation: Coroutine[None, None, None]) -> None:
    """Keep file observation independent and join its resources on parent exit."""
    collector = asyncio.create_task(_watch_files())
    collector.add_done_callback(_observation_done)
    try:
        await operation
    finally:
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)


def main() -> None:
    """Prepare protected listeners before entering the asynchronous service loop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=lifetime.DIRECTORY)
    parser.add_argument("--deny-host", action="append", default=[])
    parser.add_argument("--deny-network", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    directory: Path = arguments.directory
    registry_root = Path("/var/lib/tinkerfin-workspaces")
    lifetime.require_private_storage(registry_root)
    lifetime.claim(directory)
    namespace = asyncio.run(_wait_execd_namespace())
    registry = ProjectRegistry(
        registry_root,
        uid=1000,
        gid=1000,
        session_namespace=namespace,
    )
    stopped = registry.retire_sessions()
    limits = Limits()
    with (
        unix_listener(directory / "egress.sock", limits.clients) as listener,
        unix_listener(directory / "control.sock", 64, mode=0o600) as control,
    ):
        service = WorkspaceNetwork(
            EgressProxy(
                listener,
                DnsResolver(_nameserver()),
                deny_hosts=arguments.deny_host,
                deny_networks=arguments.deny_network,
                limits=limits,
            ),
            control,
            session_namespace=namespace,
            stopped_sessions=stopped,
        )
        lifetime.publish_namespace(namespace, directory)

        async def serve_ready() -> None:
            print(json.dumps({"ready": True}), flush=True)
            command = arguments.command
            if command and command[0] == "--":
                command = command[1:]
            if command:
                await _with_observation(
                    _first_completed(service.serve(), _command(command))
                )
            else:
                await _with_observation(service.serve())

        try:
            asyncio.run(serve_until_signal(serve_ready()))
        finally:
            (directory / "ready").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
