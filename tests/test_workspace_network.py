"""Run revocation closes DNS, buffered bodies and sockets without affecting peers."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import ipaddress
import json
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Literal
from unittest import mock
from uuid import uuid4

import egress
import network
import relay
import supervise
from test_workspace_egress import HEADER, PUBLIC, Deadlines, ProxyHarness, proxy_running


class NetworkStatusTest(unittest.TestCase):
    def run_main(
        self,
        operation: str = "status",
        *,
        phase: Literal["connect", "send", "receive"] = "connect",
        failure: BaseException | None = None,
        response: bytes = b'{"ready":true}\n',
    ) -> tuple[int, str]:
        connect = mock.AsyncMock(side_effect=failure if phase == "connect" else None)
        send = mock.AsyncMock(side_effect=failure if phase == "send" else None)
        receive = mock.AsyncMock(
            side_effect=failure
            if phase == "receive" and failure is not None
            else [response, b""]
        )
        arguments = ["network.py", operation]
        if operation != "status":
            arguments.extend((str(uuid4()), str(uuid4())))
        output = io.StringIO()
        exit_code = 0
        with (
            mock.patch.object(asyncio.SelectorEventLoop, "sock_connect", connect),
            mock.patch.object(asyncio.SelectorEventLoop, "sock_sendall", send),
            mock.patch.object(asyncio.SelectorEventLoop, "sock_recv", receive),
            mock.patch.object(network.sys, "argv", arguments),
            contextlib.redirect_stdout(output),
        ):
            try:
                network.main()
            except SystemExit as result:
                assert isinstance(result.code, int)
                exit_code = result.code
        connect.assert_awaited_once()
        called = connect.await_args
        assert called is not None
        endpoint = called.args[0]
        assert isinstance(endpoint, socket.socket)
        self.assertEqual(endpoint.fileno(), -1)
        if failure is not None and phase == "connect":
            send.assert_not_awaited()
            receive.assert_not_awaited()
        return exit_code, output.getvalue()

    def test_status_reports_explicit_not_ready_for_starting_listener(self) -> None:
        for number in (errno.ENOENT, errno.ECONNREFUSED):
            with self.subTest(errno=number):
                self.assertEqual(
                    self.run_main(failure=OSError(number, "private failure")),
                    (0, '{"ready":false}\n'),
                )

    def test_status_keeps_permission_timeout_and_other_connect_errors_fatal(
        self,
    ) -> None:
        for failure in (
            PermissionError(errno.EACCES, "private failure"),
            OSError(errno.EIO, "private failure"),
            TimeoutError("private failure"),
        ):
            with self.subTest(error=type(failure).__name__):
                self.assertEqual(
                    self.run_main(failure=failure),
                    (1, '{"error":"unavailable"}\n'),
                )

    def test_later_phases_cannot_turn_socket_errors_into_not_ready(self) -> None:
        for phase in ("send", "receive"):
            for number in (errno.ENOENT, errno.ECONNREFUSED):
                with self.subTest(phase=phase, errno=number):
                    self.assertEqual(
                        self.run_main(
                            phase=phase, failure=OSError(number, "private failure")
                        ),
                        (1, '{"error":"unavailable"}\n'),
                    )

    def test_status_keeps_valid_success_and_rejects_invalid_responses(self) -> None:
        for response, expected in (
            (b'{"ready":true}\n', (0, '{"ready":true}\n')),
            (b"invalid JSON", (1, '{"error":"unavailable"}\n')),
            (b"[]", (1, '{"error":"unavailable"}\n')),
            (b'{"error":"denied"}\n', (1, '{"error":"denied"}\n')),
        ):
            with self.subTest(response=response):
                self.assertEqual(self.run_main(response=response), expected)

    def test_grant_and_revoke_keep_connection_failures_and_success_results(
        self,
    ) -> None:
        for operation, response in (
            ("grant", b'{"token":"synthetic"}\n'),
            ("revoke", b'{"stopped":true}\n'),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(
                    self.run_main(operation, response=response), (0, response.decode())
                )
                for number in (errno.ENOENT, errno.ECONNREFUSED):
                    with self.subTest(errno=number):
                        self.assertEqual(
                            self.run_main(
                                operation, failure=OSError(number, "private failure")
                            ),
                            (1, '{"error":"unavailable"}\n'),
                        )


class NetworkRequestSettlementTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_status_payload_does_not_receive_readiness(self) -> None:
        failure = FileNotFoundError(errno.ENOENT, "private failure")
        connect = mock.AsyncMock(side_effect=failure)
        with (
            mock.patch.object(asyncio.get_running_loop(), "sock_connect", connect),
            self.assertRaises(FileNotFoundError) as caught,
        ):
            await network.request({"operation": "status", "extra": "invalid"})
        self.assertIs(caught.exception, failure)
        called = connect.await_args
        assert called is not None
        endpoint = called.args[0]
        assert isinstance(endpoint, socket.socket)
        self.assertEqual(endpoint.fileno(), -1)

    async def test_cancellation_and_deadline_close_each_pending_socket_phase(
        self,
    ) -> None:
        for phase in ("connect", "send", "receive"):
            for stop in ("cancel", "deadline"):
                with self.subTest(phase=phase, stop=stop):
                    entered = asyncio.Event()
                    release = asyncio.Event()
                    deadlines = Deadlines()

                    async def blocked(
                        *_arguments: object,
                        entered_phase: asyncio.Event = entered,
                        released_phase: asyncio.Event = release,
                    ) -> bytes:
                        entered_phase.set()
                        await released_phase.wait()
                        return b""

                    connect = mock.AsyncMock(
                        side_effect=blocked if phase == "connect" else None
                    )
                    send = mock.AsyncMock(
                        side_effect=blocked if phase == "send" else None
                    )
                    receive = mock.AsyncMock(
                        side_effect=blocked if phase == "receive" else [b""]
                    )
                    loop = asyncio.get_running_loop()
                    with (
                        mock.patch.object(loop, "sock_connect", connect),
                        mock.patch.object(loop, "sock_sendall", send),
                        mock.patch.object(loop, "sock_recv", receive),
                        mock.patch.object(
                            network.asyncio, "timeout", deadlines.timeout
                        ),
                    ):
                        requesting = asyncio.create_task(
                            network.request({"operation": "status"})
                        )
                        try:
                            await entered.wait()
                            if stop == "cancel":
                                requesting.cancel()
                            else:
                                (await deadlines.active(30)).expire()
                            with self.assertRaises(
                                asyncio.CancelledError
                                if stop == "cancel"
                                else TimeoutError
                            ):
                                await requesting
                        finally:
                            release.set()
                            requesting.cancel()
                            await asyncio.gather(requesting, return_exceptions=True)
                    called = connect.await_args
                    assert called is not None
                    endpoint = called.args[0]
                    assert isinstance(endpoint, socket.socket)
                    self.assertEqual(endpoint.fileno(), -1)


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
                owner = {"session_id": str(uuid4()), "session_namespace": str(uuid4())}
                stopped = supervise.SessionOwner(str(uuid4()), str(uuid4()))
                service = supervise.WorkspaceNetwork(
                    proxy,
                    control,
                    session_namespace=owner["session_namespace"],
                    stopped_sessions=(stopped,),
                )
                serving = asyncio.create_task(service.serve())
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
                        {"stopped": False},
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
                    for prior in (
                        stopped,
                        supervise.SessionOwner(str(uuid4()), stopped.session_namespace),
                    ):
                        self.assertEqual(
                            json.loads(
                                await network.request(
                                    {"operation": "grant", **prior.payload()},
                                    control_path,
                                )
                            ),
                            {"error": "denied"},
                        )
                        self.assertEqual(
                            json.loads(
                                await network.request(
                                    {"operation": "revoke", **prior.payload()},
                                    control_path,
                                )
                            ),
                            {"stopped": prior == stopped},
                        )
                finally:
                    serving.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await serving


if __name__ == "__main__":
    unittest.main()
