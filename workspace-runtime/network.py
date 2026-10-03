"""Invoke the trusted parent's Run-scoped egress control socket."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifetime import CONTROL_SOCKET


async def request(payload: dict[str, str], path: Path = CONTROL_SOCKET) -> bytes:
    """Exchange one bounded request; closing the caller never revokes server ownership."""
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > 4096:
        raise ValueError("Egress control request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.setblocking(False)
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(30):
            await loop.sock_connect(endpoint, str(path))
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
