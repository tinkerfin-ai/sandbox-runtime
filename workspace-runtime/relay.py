"""Run-local loopback transport for the parent's Unix HTTP egress proxy."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket

from egress import (
    Limits,
    _ConnectionServer,
    _first_completed,
    _Input,
    _tunnel,
    serve_until_signal,
)


class Relay:
    """Relay only an owned IPv4 loopback listener to one fixed proxy socket.

    The workspace supervisor starts this process inside the run's private network
    namespace, binds the trusted socket into its root, and terminates it with the
    run. The relay does not interpret or authorize destinations; the parent proxy
    applies that policy. Closing either side closes the entire connection.

    Args:
        listener: Bound 127.0.0.1 listener whose ownership transfers here.
        proxy_socket: The one trusted Unix proxy path exposed inside the run.
        egress_token: This Run's private proxy grant, never forwarded to origins.
        limits: Concurrency, buffer, connection, and idle bounds.
    """

    def __init__(
        self,
        listener: socket.socket,
        proxy_socket: str,
        *,
        egress_token: str,
        limits: Limits = Limits(),
    ) -> None:
        if (
            listener.family != socket.AF_INET
            or listener.getsockname()[0] != "127.0.0.1"
        ):
            raise ValueError("The relay requires an IPv4 loopback listener")
        if re.fullmatch(r"[0-9a-f]{64}", egress_token) is None:
            raise ValueError("Invalid Run egress grant")
        self.proxy_socket = proxy_socket
        self._prelude = egress_token.encode("ascii") + b"\n"
        self.limits = limits
        self.server = _ConnectionServer(listener, limits.clients)

    async def serve(self) -> None:
        """Serve until cancelled, then close and await all owned connections."""
        await self.server.serve(self._handle)

    async def aclose(self) -> None:
        """Stop the listener and every connection; repeated calls are harmless."""
        await self.server.aclose()

    async def _handle(self, client: socket.socket) -> None:
        incoming = _Input(client, self.limits.chunk)
        receiver = asyncio.create_task(incoming.receive())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.setblocking(False)

            async def forward() -> None:
                async with asyncio.timeout(self.limits.connect_timeout):
                    await asyncio.get_running_loop().sock_connect(
                        upstream, self.proxy_socket
                    )
                    await asyncio.get_running_loop().sock_sendall(
                        upstream, self._prelude
                    )
                await _tunnel(incoming, client, upstream, self.limits)

            try:
                await _first_completed(forward(), incoming.closed.wait())
            except (OSError, TimeoutError):
                return
            finally:
                upstream.close()
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)


def main() -> None:
    """Listen only inside the current network namespace on 127.0.0.1."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--egress-token", required=True)
    arguments = parser.parse_args()
    limits = Limits()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", arguments.port))
        listener.listen(limits.clients)
        relay = Relay(
            listener,
            arguments.socket,
            egress_token=arguments.egress_token,
            limits=limits,
        )

        async def serve_ready() -> None:
            print(
                json.dumps({"ready": True, "port": int(listener.getsockname()[1])}),
                flush=True,
            )
            await relay.serve()

        asyncio.run(serve_until_signal(serve_ready()))


if __name__ == "__main__":
    main()
