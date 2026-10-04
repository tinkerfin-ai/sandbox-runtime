"""Invoke the trusted parent's Run-scoped egress control socket."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifetime import CONTROL_SOCKET


async def request(payload: dict[str, str], path: Path = CONTROL_SOCKET) -> bytes:
    """Exchange control messages without treating a starting listener as failure.

    The socket closes on completion, failure, deadline, or cancellation. Closing
    the caller never revokes server-side Run ownership.

    Args:
        payload: Control action and any required Run ownership fields.
        path: Unix control socket in the trusted parent's filesystem.

    Returns:
        Response bytes, or explicit not-ready JSON only for a status request
        whose socket is missing or not yet listening.

    Raises:
        OSError: Another connection failure, or any send or receive failure occurs.
        TimeoutError: The existing request deadline expires.
        ValueError: The request or response exceeds its byte limit.
        asyncio.CancelledError: The caller cancels the exchange.
    """
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > 4096:
        raise ValueError("Egress control request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.setblocking(False)
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(30):
            try:
                await loop.sock_connect(endpoint, str(path))
            except OSError as error:
                if payload == {"operation": "status"} and error.errno in (
                    errno.ENOENT,
                    errno.ECONNREFUSED,
                ):
                    return b'{"ready":false}\n'
                raise
            await loop.sock_sendall(endpoint, encoded)
            response = bytearray()
            while True:
                value = await loop.sock_recv(endpoint, 4097 - len(response))
                if not value:
                    return bytes(response)
                response.extend(value)
                if len(response) > 4096:
                    raise ValueError("Egress control response is too large")


def main() -> None:
    """Report one safe JSON result; bearer grants are returned only to the parent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "grant", "revoke"))
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("session_namespace", nargs="?")
    arguments = parser.parse_args()
    payload = {"operation": arguments.operation}
    if arguments.operation != "status":
        if arguments.session_id is None or arguments.session_namespace is None:
            parser.error("Run control requires both ownership fields")
        payload.update(
            session_id=arguments.session_id,
            session_namespace=arguments.session_namespace,
        )
    elif arguments.session_id is not None or arguments.session_namespace is not None:
        parser.error("status does not accept Run identities")
    try:
        response = asyncio.run(request(payload))
        parsed: object = json.loads(response)
        if not isinstance(parsed, dict):
            raise TypeError("Invalid egress control result")
        print(response.decode("utf-8"), end="")
        if "error" in parsed:
            raise SystemExit(1)
    except (OSError, TimeoutError, TypeError, ValueError):
        print('{"error":"unavailable"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
