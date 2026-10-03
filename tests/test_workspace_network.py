"""Run revocation closes DNS, buffered bodies and sockets without affecting peers."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import egress
import network
import relay
import supervise
from test_workspace_egress import HEADER, PUBLIC, ProxyHarness, proxy_running


class NetworkOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_drains_full_queue_and_dns_after_relay_shutdown(self) -> None:
        resolving = asyncio.Event()
        cancelled = asyncio.Event()
        queue_blocked = asyncio.Event()
        limits = egress.Limits(chunk=64)
        original_put = asyncio.Queue.put

        async def put(queue: asyncio.Queue[bytes], value: bytes) -> None:
            if resolving.is_set() and queue.full():
                queue_blocked.set()
            await original_put(queue, value)

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            if hostname == "127.0.0.1":
                return (ipaddress.ip_address(hostname),)
            resolving.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return (PUBLIC,)

        with mock.patch.object(asyncio.Queue, "put", new=put):
            async with proxy_running(lookup, limits=limits) as harness:
                peer = ProxyHarness(harness.proxy, harness.path)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(4)
                    forwarder = relay.Relay(
                        listener,
                        str(harness.path),
                        egress_token=harness.token,
                        limits=limits,
                    )
                    serving = asyncio.create_task(forwarder.serve())
                    reader, writer = await asyncio.open_connection(
                        *listener.getsockname()
                    )
                    try:
                        writer.write(
                            b"POST http://public.example/request HTTP/1.1\r\n"
                            b"Host: public.example\r\nContent-Length: 4096\r\n\r\n"
                        )
                        await writer.drain()
                        await resolving.wait()
                        writer.write(b"x" * (3 * limits.chunk))
                        await writer.drain()
                        await queue_blocked.wait()
                        await forwarder.aclose()
                        self.assertEqual(await reader.read(), b"")
                        await harness.proxy.revoke_run_egress(
                            harness.session_id, harness.namespace
                        )
                        self.assertTrue(cancelled.is_set())
                        self.assertFalse(harness.proxy.server.active)
                        peer_reader, peer_writer = await peer.connect()
                        peer_writer.write(
                            b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n"
                        )
                        await peer_writer.drain()
                        self.assertTrue(
                            (await peer_reader.read()).startswith(b"HTTP/1.1 403")
                        )
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        for peer_writer in peer.writers:
                            peer_writer.close()
                            await peer_writer.wait_closed()
                        await forwarder.aclose()
                        await asyncio.gather(serving, return_exceptions=True)

    async def test_cancelled_revoke_keeps_drain_owned_and_late_grants_fenced(
        self,
    ) -> None:
        resolving = asyncio.Event()
        draining = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            resolving.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release_cleanup.wait()
            return (PUBLIC,)

        async with proxy_running(lookup) as harness:
            reader, writer = await harness.connect()
            writer.write(HEADER)
            await writer.drain()
            await resolving.wait()
            revoking = asyncio.create_task(
                harness.proxy.revoke_run_egress(harness.session_id, harness.namespace)
            )
            try:
                await draining.wait()
                revoking.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await revoking
                with self.assertRaises(egress.Rejected):
                    harness.proxy.grant_run_egress(
                        harness.session_id, harness.namespace
                    )
            finally:
                release_cleanup.set()
                await asyncio.gather(revoking, return_exceptions=True)
            await harness.proxy.revoke_run_egress(harness.session_id, harness.namespace)
            self.assertEqual(await reader.read(), b"")
            self.assertFalse(harness.proxy.server.active)

    async def test_revoke_unknown_owner_prevents_late_grant(self) -> None:
        async with proxy_running() as harness:
            session_id, namespace = str(uuid4()), str(uuid4())
            await harness.proxy.revoke_run_egress(session_id, namespace)
            await harness.proxy.revoke_run_egress(session_id, namespace)
            with self.assertRaises(egress.Rejected):
                harness.proxy.grant_run_egress(session_id, namespace)
            other = harness.proxy.grant_run_egress(session_id, str(uuid4()))
            self.assertEqual(len(other), 64)

    async def test_partial_handshake_cannot_be_admitted_after_revoke(self) -> None:
        lookup = mock.AsyncMock(return_value=(PUBLIC,))
        async with proxy_running(lookup) as harness:
            reader, writer = await asyncio.open_unix_connection(str(harness.path))
            try:
                writer.write(harness.token[:32].encode())
                await writer.drain()
                await harness.proxy.revoke_run_egress(
                    harness.session_id, harness.namespace
                )
                writer.write(harness.token[32:].encode() + b"\n")
                await writer.drain()
                self.assertEqual(await reader.read(), b"")
                lookup.assert_not_awaited()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_unauthorized_transport_never_resolves_a_destination(self) -> None:
        lookup = mock.AsyncMock(return_value=(PUBLIC,))
        async with proxy_running(lookup) as harness:
            reader, writer = await asyncio.open_unix_connection(str(harness.path))
            try:
                writer.write(b"a" * 64 + b"\n")
                await writer.drain()
                self.assertEqual(await reader.read(), b"")
                lookup.assert_not_awaited()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_control_protocol_grant_revoke_and_invalid_requests(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            control_path = root / "control.sock"
            with (
                egress.unix_listener(root / "egress.sock", 4) as listener,
                egress.unix_listener(control_path, 4, mode=0o600) as control,
            ):
                proxy = egress.EgressProxy(listener, egress.DnsResolver("127.0.0.1"))
                service = supervise.WorkspaceNetwork(proxy, control)
                serving = asyncio.create_task(service.serve())
                owner = {"session_id": str(uuid4()), "session_namespace": str(uuid4())}
                try:
                    self.assertEqual(
                        json.loads(
                            await network.request({"operation": "status"}, control_path)
                        ),
                        {"ready": True},
                    )
                    first = json.loads(
                        await network.request(
                            {"operation": "grant", **owner}, control_path
                        )
                    )
                    self.assertEqual(len(first["token"]), 64)
                    self.assertEqual(
                        json.loads(
                            await network.request(
                                {"operation": "grant", **owner}, control_path
                            )
                        ),
                        first,
                    )
                    self.assertEqual(
                        json.loads(
                            await network.request(
                                {"operation": "revoke", **owner}, control_path
                            )
                        ),
                        {},
                    )
                    self.assertEqual(
                        json.loads(
                            await network.request(
                                {"operation": "grant", **owner}, control_path
                            )
                        ),
                        {"error": "denied"},
                    )
                    self.assertEqual(
                        json.loads(
                            await network.request(
                                {"operation": "grant", **owner, "extra": "bad"},
                                control_path,
                            )
                        ),
                        {"error": "denied"},
                    )
                finally:
                    serving.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await serving


if __name__ == "__main__":
    unittest.main()
