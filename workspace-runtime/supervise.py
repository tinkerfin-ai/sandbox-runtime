"""Own workspace egress for the lifetime of one trusted parent container.

The control socket is never mounted into a workload. Its private parent directory
and mode restrict admission and revocation to trusted parent commands. A lifetime
marker prevents restarting this service with an empty cancellation ledger. Egress
failure terminates the entrypoint, which OpenSandbox bootstrap couples to execd.
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
import stat
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import cast

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
from watch import WorkspaceWatch


class WorkspaceNetwork:
    """Retain Run revocations independently of individual control connections."""

    def __init__(self, proxy: EgressProxy, control: socket.socket) -> None:
        self.proxy = proxy
        self.control = _ConnectionServer(control, 64)

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
                    if operation == "grant":
                        response = {
                            "token": self.proxy.grant_run_egress(session_id, namespace)
                        }
                    elif operation == "revoke":
                        await self.proxy.revoke_run_egress(session_id, namespace)
                        response = {}
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
    parser.add_argument(
        "--directory", type=Path, default=Path("/run/tinkerfin-workspace-egress")
    )
    parser.add_argument("--deny-host", action="append", default=[])
    parser.add_argument("--deny-network", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    directory: Path = arguments.directory
    directory.mkdir(mode=0o700, exist_ok=True)
    identity = directory.lstat()
    if (
        not stat.S_ISDIR(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) != 0o700
    ):
        raise RuntimeError("Workspace network requires a trusted private directory")
    descriptor = os.open(
        directory / "lifetime",
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )
    os.close(descriptor)
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
        )

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

        asyncio.run(serve_until_signal(serve_ready()))


if __name__ == "__main__":
    main()
