"""Deterministic protocol and ownership checks using only test-owned sockets."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import signal
import socket
import struct
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import TracebackType
from unittest import mock
from uuid import uuid4

import egress
import network
import relay

PUBLIC = ipaddress.ip_address("93.184.216.34")
OTHER_PUBLIC = ipaddress.ip_address("1.1.1.1")
HEADER = b"GET http://public.example/file HTTP/1.1\r\nHost: public.example\r\n\r\n"
Lookup = Callable[[str], Awaitable[tuple[egress.IPAddress, ...]]]
Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def public_lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
    try:
        return (ipaddress.ip_address(hostname),)
    except ValueError:
        return (PUBLIC,)


class ProxyHarness:
    def __init__(self, proxy: egress.EgressProxy, path: Path) -> None:
        self.proxy = proxy
        self.path = path
        self.writers: list[asyncio.StreamWriter] = []
        self.session_id = str(uuid4())
        self.namespace = str(uuid4())
        self.token = proxy.grant_run_egress(self.session_id, self.namespace)

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        self.writers.append(writer)
        writer.write(self.token.encode() + b"\n")
        await writer.drain()
        return reader, writer

    async def exchange(self, request: bytes) -> bytes:
        reader, writer = await self.connect()
        writer.write(request)
        await writer.drain()
        return await reader.read()


@contextlib.asynccontextmanager
async def proxy_running(
    lookup: Lookup = public_lookup,
    *,
    denied_hosts: tuple[str, ...] = (),
    denied_networks: tuple[str, ...] = (),
    limits: egress.Limits = egress.Limits(),
) -> AsyncIterator[ProxyHarness]:
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "proxy.sock"
        with egress.unix_listener(path, limits.clients) as listener:
            resolver = egress.DnsResolver("127.0.0.1")
            proxy = egress.EgressProxy(
                listener,
                resolver,
                deny_hosts=denied_hosts,
                deny_networks=denied_networks,
                limits=limits,
            )
            harness = ProxyHarness(proxy, path)
            with mock.patch.object(resolver, "resolve", new=lookup):
                serving = asyncio.create_task(proxy.serve())
                try:
                    yield harness
                finally:
                    for writer in harness.writers:
                        writer.close()
                    await proxy.aclose()
                    await asyncio.gather(serving, return_exceptions=True)
                    for writer in harness.writers:
                        with contextlib.suppress(OSError):
                            await writer.wait_closed()


@contextlib.asynccontextmanager
async def origin_running(handler: Handler) -> AsyncIterator[tuple[str, int]]:
    writers: set[asyncio.StreamWriter] = set()
    active: set[asyncio.Task[None]] = set()
    async with asyncio.TaskGroup() as requests:

        async def owned(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writers.add(writer)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                writers.discard(writer)

        def accepted(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = requests.create_task(owned(reader, writer))
            active.add(task)
            task.add_done_callback(active.discard)

        server = await asyncio.start_server(accepted, "127.0.0.1", 0)
        try:
            address = server.sockets[0].getsockname()
            yield (str(address[0]), int(address[1]))
        finally:
            server.close()
            await server.wait_closed()
            for writer in writers:
                writer.close()
            for task in active:
                task.cancel()


@contextlib.contextmanager
def route_public(address: tuple[str, int]):
    loop = asyncio.get_running_loop()
    original = loop.sock_connect
    destinations: list[tuple[str, int]] = []

    async def connect(
        endpoint: socket.socket, destination: str | tuple[str, int]
    ) -> None:
        if endpoint.family in (socket.AF_INET, socket.AF_INET6):
            if not isinstance(destination, tuple):
                raise AssertionError("Expected a numeric network destination")
            destinations.append(destination)
            await original(endpoint, address)
        else:
            await original(endpoint, destination)

    with mock.patch.object(loop, "sock_connect", new=connect):
        yield destinations


def dns_name(hostname: str) -> bytes:
    return (
        b"".join(bytes([len(label)]) + label.encode() for label in hostname.split("."))
        + b"\0"
    )


def dns_packet(
    query: bytes, records: list[tuple[str, int, bytes]], *, flags: int = 0x8180
) -> bytes:
    payload = bytearray(
        struct.pack(
            "!6H", int.from_bytes(query[:2], "big"), flags, 1, len(records), 0, 0
        )
    )
    payload.extend(query[12:])
    for hostname, kind, value in records:
        payload.extend(dns_name(hostname))
        payload.extend(struct.pack("!HHIH", kind, 1, 60, len(value)))
        payload.extend(value)
    return bytes(payload)


class ParsingTests(unittest.TestCase):
    def test_authority_and_framing_are_rebuilt(self) -> None:
        request = egress.parse_request(
            b"POST http://Public.Example.:80?name=one HTTP/1.1\r\n"
            b"Host: public.example\r\nContent-Length: 0003\r\n"
            b"Connection: keep-alive\r\nProxy-Authorization: secret\r\n"
            b"Proxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n",
            egress.Limits(),
        )
        self.assertEqual(request.hostname, "public.example")
        self.assertEqual(request.length, 3)
        self.assertEqual(
            request.header,
            b"POST /?name=one HTTP/1.1\r\nHost: public.example\r\n"
            b"accept: */*\r\nContent-Length: 3\r\nConnection: close\r\n\r\n",
        )

    def test_ambiguous_or_unsupported_requests_are_rejected(self) -> None:
        requests = [
            HEADER.replace(b"Host:", b"Host: private.example\r\nHost:"),
            HEADER.replace(b"Host: public.example", b"Host: private.example"),
            HEADER.replace(b"Host:", b"Host :"),
            HEADER.replace(b"Host:", b" Host:"),
            HEADER.replace(b"Host:", b"Host:\t"),
            HEADER.replace(b"public.example/file", b"user@public.example/file"),
            HEADER.replace(b"public.example/file", b"public.example:443/file"),
            HEADER.replace(b"public.example/file", b"public.example:080/file"),
            HEADER.replace(b"public.example/file", b"public.example\\@127.0.0.1/file"),
            HEADER.replace(b"public.example/file", b"public.example%2f@127.0.0.1/file"),
            HEADER.replace(b"/file", b"/file#fragment"),
            HEADER.replace(b"/file", b"/file\tHTTP/1.1"),
            HEADER.replace(b"http://", b"https://"),
            HEADER.replace(b"http://public.example/file", b"/file"),
            HEADER.replace(b"HTTP/1.1", b"HTTP/1.0"),
            HEADER.replace(b"\r\n", b"\n"),
            HEADER.replace(b"GET ", b"GET\t"),
            HEADER.replace(b"GET ", b"GET  "),
            HEADER.replace(b"\r\n\r\n", b"\r\nContent-Length: +1\r\n\r\n"),
            HEADER.replace(b"\r\n\r\n", b"\r\nContent-Length: 1, 1\r\n\r\n"),
            HEADER.replace(
                b"\r\n\r\n", b"\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n"
            ),
            HEADER.replace(
                b"\r\n\r\n",
                b"\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\n",
            ),
            HEADER.replace(
                b"\r\n\r\n", b"\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"
            ),
            HEADER.replace(b"\r\n\r\n", b"\r\nConnection: host\r\n\r\n"),
            HEADER.replace(b"\r\n\r\n", b"\r\nUpgrade: websocket\r\n\r\n"),
            HEADER.replace(b"\r\n\r\n", b"\r\nExpect: 100-continue\r\n\r\n"),
            b"CONNECT public.example:80 HTTP/1.1\r\nHost: public.example:80\r\n\r\n",
            b"CONNECT public.example HTTP/1.1\r\nHost: public.example\r\n\r\n",
            b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\nContent-Length: 1\r\n\r\n",
            b"CONNECT [::ffff:127.0.0.1%lo]:443 HTTP/1.1\r\nHost: [::ffff:127.0.0.1%lo]:443\r\n\r\n",
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(egress.Rejected):
                egress.parse_request(request, egress.Limits())

    def test_header_and_body_limits(self) -> None:
        with self.assertRaises(egress.Rejected) as failure:
            egress.parse_request(HEADER, egress.Limits(headers=10))
        self.assertEqual(failure.exception.status, 431)
        with self.assertRaises(egress.Rejected) as failure:
            egress.parse_request(
                HEADER.replace(b"\r\n\r\n", b"\r\nContent-Length: 5\r\n\r\n"),
                egress.Limits(request_body=4),
            )
        self.assertEqual(failure.exception.status, 413)

    def test_unix_socket_does_not_replace_existing_resources(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "owned"
            path.write_text("borrowed")
            with self.assertRaises(OSError), egress.unix_listener(path, 1):
                self.fail("Existing files must not be replaced")
            self.assertEqual(path.read_text(), "borrowed")
            path.unlink()
            with egress.unix_listener(path, 1):
                self.assertTrue(path.is_socket())
                path.unlink()
                path.write_text("replacement")
            self.assertEqual(path.read_text(), "replacement")


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_python311_connect_uses_checked_authority_without_host(self) -> None:
        """Use the bytes emitted by CPython v3.11.15 HTTPConnection._tunnel."""

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            self.assertEqual(await reader.readexactly(5), b"hello")
            writer.write(b"world")
            await writer.drain()

        async with origin_running(origin) as address:
            with route_public(address) as destinations:
                async with proxy_running() as harness:
                    reader, writer = await harness.connect()
                    writer.write(b"CONNECT public.example:443 HTTP/1.0\r\n\r\n")
                    await writer.drain()
                    self.assertTrue(
                        (await reader.readuntil(b"\r\n\r\n")).startswith(
                            b"HTTP/1.1 200"
                        )
                    )
                    writer.write(b"hello")
                    await writer.drain()
                    self.assertEqual(await reader.read(), b"world")
                self.assertEqual(destinations, [(str(PUBLIC), 443)])

    async def test_every_resolved_address_must_be_public(self) -> None:
        denied = (
            "0.0.0.0",
            "0.1.2.3",
            "10.1.2.3",
            "127.0.0.1",
            "100.64.1.2",
            "169.254.169.254",
            "172.17.0.1",
            "192.168.1.1",
            "192.0.0.8",
            "192.0.2.1",
            "192.88.99.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "::ffff:93.184.216.34",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
            "64:ff9b:1::a00:1",
            "2001::1",
            "2001:db8::1",
            "2002:7f00:1::",
            "3fff::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
        )
        for address in denied:
            with self.subTest(address=address):

                async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
                    return (PUBLIC, ipaddress.ip_address(address))

                async with proxy_running(lookup) as harness:
                    response = await harness.exchange(HEADER)
                    self.assertTrue(response.startswith(b"HTTP/1.1 403"), response)

    async def test_protected_hosts_networks_and_aliases_fail_closed(self) -> None:
        calls: list[str] = []

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            calls.append(hostname)
            return (PUBLIC,)

        async with proxy_running(lookup, denied_hosts=("public.example",)) as harness:
            self.assertTrue(
                (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 403")
            )
            self.assertEqual(calls, [])
        async with proxy_running(lookup, denied_hosts=("control.example",)) as harness:
            self.assertTrue(
                (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 403")
            )
            self.assertEqual(calls, ["public.example", "control.example"])
        async with proxy_running(denied_networks=("93.184.216.0/24",)) as harness:
            self.assertTrue(
                (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 403")
            )

        async def unavailable(hostname: str) -> tuple[egress.IPAddress, ...]:
            if hostname == "control.example":
                raise egress.Rejected(502)
            return (PUBLIC,)

        async with proxy_running(
            unavailable, denied_hosts=("control.example",)
        ) as harness:
            self.assertTrue(
                (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 502")
            )

    async def test_numeric_destination_is_pinned_and_redirect_is_not_followed(
        self,
    ) -> None:
        captured: list[bytes] = []
        response = b"HTTP/1.1 302 Found\r\nlocation: http://127.0.0.1/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            captured.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(response)
            await writer.drain()

        async with origin_running(origin) as address:
            with route_public(address) as destinations:
                async with proxy_running() as harness:
                    self.assertEqual(await harness.exchange(HEADER), response)
                    denied = await harness.exchange(
                        b"GET http://127.0.0.1/private HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                    )
                    self.assertTrue(denied.startswith(b"HTTP/1.1 403"))
            self.assertEqual(destinations, [(str(PUBLIC), 80)])
        self.assertEqual(
            captured,
            [
                b"GET /file HTTP/1.1\r\nHost: public.example\r\nConnection: close\r\n\r\n"
            ],
        )

    async def test_protected_addresses_are_resolved_again_for_every_connection(
        self,
    ) -> None:
        protected = OTHER_PUBLIC
        calls: list[str] = []

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            calls.append(hostname)
            return (protected if hostname == "control.example" else PUBLIC,)

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
            await writer.drain()

        async with origin_running(origin) as address:
            with route_public(address) as destinations:
                async with proxy_running(
                    lookup, denied_hosts=("control.example",)
                ) as harness:
                    self.assertTrue(
                        (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 204")
                    )
                    protected = PUBLIC
                    self.assertTrue(
                        (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 403")
                    )
            self.assertEqual(destinations, [(str(PUBLIC), 80)])
        self.assertEqual(calls, ["public.example", "control.example"] * 2)

    async def test_local_control_plane_names_do_not_disable_public_egress(self) -> None:
        resolver = egress.DnsResolver("127.0.0.1")
        queried: list[str] = []

        async def query(hostname: str, kind: int) -> tuple[egress.IPAddress, ...]:
            queried.append(hostname)
            self.assertEqual(hostname, "public.example")
            return (PUBLIC,) if kind == 1 else ()

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
            await writer.drain()

        async with origin_running(origin) as address:
            with (
                route_public(address),
                mock.patch.object(resolver, "_resolve_kind", new=query),
            ):
                async with proxy_running(
                    resolver.resolve, denied_hosts=("localhost", "127.0.0.1", "::1")
                ) as harness:
                    self.assertTrue(
                        (await harness.exchange(HEADER)).startswith(b"HTTP/1.1 204")
                    )
        self.assertEqual(queried, ["public.example", "public.example"])

    async def test_request_bodies_have_one_canonical_framing(self) -> None:
        cases = (
            (b"Content-Length: 0005", b"hello", b"Content-Length: 5", b"hello"),
            (
                b"Transfer-Encoding: chunked",
                b"03\r\nabc\r\n2\r\nde\r\n0\r\n\r\n",
                b"Transfer-Encoding: chunked",
                b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n",
            ),
        )
        for framing, body, expected_framing, expected_body in cases:
            with self.subTest(framing=framing):
                captured: list[bytes] = []

                async def origin(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                ) -> None:
                    captured.append(await reader.readuntil(b"\r\n\r\n"))
                    captured.append(await reader.readexactly(len(expected_body)))
                    writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
                    await writer.drain()

                async with origin_running(origin) as address:
                    with route_public(address):
                        async with proxy_running() as harness:
                            request = (
                                b"POST http://public.example/file HTTP/1.1\r\nHost: public.example\r\n"
                                + framing
                                + b"\r\n\r\n"
                                + body
                            )
                            self.assertTrue(
                                (await harness.exchange(request)).startswith(
                                    b"HTTP/1.1 204"
                                )
                            )
                self.assertIn(expected_framing + b"\r\n", captured[0])
                self.assertEqual(captured[1], expected_body)

    async def test_chunk_extensions_trailers_and_pipelining_are_not_forwarded(
        self,
    ) -> None:
        cases = (
            (b"Transfer-Encoding: chunked", b"3;ext=value\r\nabc\r\n0\r\n\r\n"),
            (b"Transfer-Encoding: chunked", b"0\r\nHost: private.example\r\n\r\n"),
            (
                b"Content-Length: 0",
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
            ),
        )
        for framing, body in cases:
            with self.subTest(framing=framing, body=body):
                received: list[bytes] = []
                closed = asyncio.Event()

                async def origin(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                ) -> None:
                    received.append(await reader.read())
                    closed.set()

                async with origin_running(origin) as address:
                    with route_public(address) as destinations:
                        async with proxy_running() as harness:
                            request = HEADER[:-2] + framing + b"\r\n\r\n" + body
                            self.assertTrue(
                                (await harness.exchange(request)).startswith(
                                    b"HTTP/1.1 400"
                                )
                            )
                            await closed.wait()
                        self.assertEqual(destinations, [(str(PUBLIC), 80)])
                self.assertNotIn(b"private.example", received[0])
                self.assertNotIn(b"CONNECT", received[0])
                self.assertNotIn(b"ext=value", received[0])

    async def test_connect_is_opaque_and_disconnect_closes_the_origin(self) -> None:
        ended = asyncio.Event()
        opaque = b"\x16\x03\x03opaque-public-443"

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            self.assertEqual(await reader.readexactly(len(opaque)), opaque)
            writer.write(b"answer")
            await writer.drain()
            self.assertEqual(await reader.read(), b"")
            ended.set()

        async with origin_running(origin) as address:
            with route_public(address) as destinations:
                async with proxy_running() as harness:
                    reader, writer = await harness.connect()
                    writer.write(
                        b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n"
                    )
                    await writer.drain()
                    self.assertEqual(
                        await reader.readuntil(b"\r\n\r\n"),
                        b"HTTP/1.1 200 Connection established\r\n\r\n",
                    )
                    writer.write(opaque)
                    await writer.drain()
                    self.assertEqual(await reader.readexactly(6), b"answer")
                    writer.close()
                    await writer.wait_closed()
                    await ended.wait()
                self.assertEqual(destinations, [(str(PUBLIC), 443)])

    async def test_disconnect_cancels_pending_resolution(self) -> None:
        resolving = asyncio.Event()
        cancelled = asyncio.Event()
        pending = asyncio.Event()

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            resolving.set()
            try:
                await pending.wait()
            finally:
                cancelled.set()
            return (PUBLIC,)

        async with proxy_running(lookup) as harness:
            _, writer = await harness.connect()
            writer.write(HEADER)
            await writer.drain()
            await resolving.wait()
            writer.close()
            await writer.wait_closed()
            await cancelled.wait()
            self.assertFalse(pending.is_set())

    async def test_disconnect_cancels_pending_connect_and_closes_its_socket(
        self,
    ) -> None:
        connecting = asyncio.Event()
        cancelled = asyncio.Event()
        pending = asyncio.Event()
        attempted: list[socket.socket] = []
        loop = asyncio.get_running_loop()
        original = loop.sock_connect

        async def connect(
            endpoint: socket.socket, destination: str | tuple[str, int]
        ) -> None:
            if endpoint.family == socket.AF_UNIX:
                await original(endpoint, destination)
                return
            attempted.append(endpoint)
            connecting.set()
            try:
                await pending.wait()
            finally:
                cancelled.set()

        with mock.patch.object(loop, "sock_connect", new=connect):
            async with proxy_running() as harness:
                _, writer = await harness.connect()
                writer.write(HEADER)
                await writer.drain()
                await connecting.wait()
                writer.close()
                await writer.wait_closed()
                await cancelled.wait()
        self.assertEqual(attempted[0].fileno(), -1)

    async def test_server_shutdown_settles_both_sides_and_preserves_cancellation(
        self,
    ) -> None:
        arrived = asyncio.Event()
        ended = asyncio.Event()

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            self.assertEqual(await reader.read(), b"")
            ended.set()

        async with origin_running(origin) as address:
            with route_public(address):
                async with proxy_running() as harness:
                    reader, writer = await harness.connect()
                    writer.write(HEADER)
                    await writer.drain()
                    await arrived.wait()
                    serving = harness.proxy.server.task
                    self.assertIsNotNone(serving)
                    assert serving is not None
                    serving.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await serving
                    await ended.wait()
                    self.assertEqual(await reader.read(), b"")
                    self.assertEqual(harness.proxy.server.active, set())
                    await harness.proxy.aclose()
                    await harness.proxy.aclose()

    async def test_client_limit_rejects_without_creating_another_resolver(self) -> None:
        resolving = asyncio.Event()
        pending = asyncio.Event()
        calls: list[str] = []

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            calls.append(hostname)
            resolving.set()
            await pending.wait()
            return (PUBLIC,)

        async with proxy_running(lookup, limits=egress.Limits(clients=1)) as harness:
            _, first = await harness.connect()
            first.write(HEADER)
            await first.drain()
            await resolving.wait()
            second, second_writer = await asyncio.open_unix_connection(
                str(harness.path)
            )
            harness.writers.append(second_writer)
            self.assertEqual(await second.read(), b"")
            self.assertEqual(calls, ["public.example"])

    async def test_response_framing_closes_even_when_the_origin_requests_keepalive(
        self,
    ) -> None:
        cases = (
            (HEADER, b"Content-Length: 3", b"xyzEXTRA", b"xyz"),
            (HEADER.replace(b"GET ", b"HEAD "), b"Content-Length: 3", b"", b""),
            (
                HEADER,
                b"Transfer-Encoding: chunked",
                b"03\r\nxyz\r\n0\r\n\r\nEXTRA",
                b"3\r\nxyz\r\n0\r\n\r\n",
            ),
        )
        for request, framing, body, expected in cases:
            with self.subTest(request=request, framing=framing):
                closed = asyncio.Event()

                async def origin(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                ) -> None:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(
                        b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\n"
                    )
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nSet-Cookie: one=1\r\nSet-Cookie: two=2\r\n"
                        + framing
                        + b"\r\n\r\n"
                        + body
                    )
                    await writer.drain()
                    self.assertEqual(await reader.read(), b"")
                    closed.set()

                async with origin_running(origin) as address:
                    with route_public(address):
                        async with proxy_running() as harness:
                            response = await harness.exchange(request)
                            header, actual = response.split(b"\r\n\r\n", 1)
                            self.assertTrue(header.startswith(b"HTTP/1.1 200 OK"))
                            self.assertIn(b"Connection: close", header)
                            self.assertNotIn(b"keep-alive", header)
                            self.assertIn(
                                b"set-cookie: one=1\r\nset-cookie: two=2", header
                            )
                            self.assertEqual(actual, expected)
                            await closed.wait()

    async def test_origin_ambiguous_headers_are_rejected_before_forwarding(
        self,
    ) -> None:
        cases = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
            b"HTTP/1.1 200 OK\r\n Bad: folded\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nContent-Length: 1\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: +1\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nConnection: content-length\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nHeader: " + b"a" * 16384 + b"\r\n\r\n",
        )
        for response in cases:
            with self.subTest(response=response[:100]):

                async def origin(
                    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                ) -> None:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(response)
                    await writer.drain()

                async with origin_running(origin) as address:
                    with route_public(address):
                        async with proxy_running() as harness:
                            self.assertTrue(
                                (await harness.exchange(HEADER)).startswith(
                                    b"HTTP/1.1 502"
                                )
                            )

    async def test_response_write_backpressure_is_cancelled_on_client_disconnect(
        self,
    ) -> None:
        blocked = asyncio.Event()
        cancelled = asyncio.Event()
        ended = asyncio.Event()
        original = egress._send

        async def send(endpoint: socket.socket, value: bytes, timeout: float) -> None:
            if value == b"payload":
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                await original(endpoint, value, timeout)

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\npayload")
            await writer.drain()
            self.assertEqual(await reader.read(), b"")
            ended.set()

        async with origin_running(origin) as address:
            with route_public(address), mock.patch.object(egress, "_send", new=send):
                async with proxy_running() as harness:
                    _, writer = await harness.connect()
                    writer.write(HEADER)
                    await writer.drain()
                    await blocked.wait()
                    writer.close()
                    await writer.wait_closed()
                    await cancelled.wait()
                    await ended.wait()


class BufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_delimiters_can_cross_every_input_boundary(self) -> None:
        for split in range(1, len(HEADER)):
            with self.subTest(split=split), socket.socket(socket.AF_UNIX) as endpoint:
                incoming = egress._Input(endpoint, 64)
                incoming.queue.put_nowait(HEADER[:split])
                incoming.queue.put_nowait(HEADER[split:] + b"body")
                self.assertEqual(await incoming.until(b"\r\n\r\n", len(HEADER)), HEADER)
                self.assertEqual(await incoming.read(4), b"body")

    async def test_input_backpressure_has_a_fixed_number_of_owned_chunks(self) -> None:
        third_receive = asyncio.Event()
        receives: list[int] = []
        loop = asyncio.get_running_loop()

        async def receive(endpoint: socket.socket, size: int) -> bytes:
            receives.append(size)
            if len(receives) == 3:
                third_receive.set()
            return b"a" * size

        with socket.socket(socket.AF_UNIX) as endpoint:
            incoming = egress._Input(endpoint, 16)
            with mock.patch.object(loop, "sock_recv", new=receive):
                receiving = asyncio.create_task(incoming.receive())
                try:
                    await third_receive.wait()
                    self.assertEqual(receives, [16, 16, 16])
                    self.assertEqual(incoming.queue.qsize(), 2)
                finally:
                    receiving.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await receiving


class DnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_literals_and_localhost_do_not_issue_dns_queries(self) -> None:
        resolver = egress.DnsResolver("127.0.0.1")
        with mock.patch.object(
            resolver,
            "_resolve_kind",
            side_effect=AssertionError("Unexpected DNS query"),
        ):
            self.assertEqual(await resolver.resolve(str(PUBLIC)), (PUBLIC,))
            self.assertEqual(
                await resolver.resolve("::1"), (ipaddress.ip_address("::1"),)
            )
            for hostname in ("localhost", "LOCALHOST.", "service.localhost"):
                self.assertEqual(
                    await resolver.resolve(hostname),
                    (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")),
                )

    async def test_native_dns_uses_both_families_and_follows_bounded_cnames(
        self,
    ) -> None:
        observed: list[tuple[str, int]] = []
        loop = asyncio.get_running_loop()
        original = loop.sock_connect
        expected = (PUBLIC, ipaddress.ip_address("2606:4700:4700::1111"))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as nameserver:
            nameserver.bind(("127.0.0.1", 0))
            nameserver.setblocking(False)

            async def answer() -> None:
                for _ in range(4):
                    query, client = await loop.sock_recvfrom(nameserver, 4096)
                    hostname, offset = egress._dns_name(query, 12)
                    kind = int.from_bytes(query[offset : offset + 2], "big")
                    observed.append((hostname, kind))
                    if hostname == "public.example":
                        records = [(hostname, 5, dns_name("cdn.example"))]
                    else:
                        records = [
                            (hostname, kind, expected[0 if kind == 1 else 1].packed)
                        ]
                    await loop.sock_sendto(
                        nameserver, dns_packet(query, records), client
                    )

            async def connect(
                endpoint: socket.socket, destination: tuple[str, int]
            ) -> None:
                self.assertEqual(destination, ("127.0.0.1", 53))
                await original(endpoint, nameserver.getsockname())

            responder = asyncio.create_task(answer())
            try:
                with mock.patch.object(loop, "sock_connect", new=connect):
                    self.assertEqual(
                        await egress.DnsResolver("127.0.0.1").resolve("public.example"),
                        expected,
                    )
                await responder
            finally:
                responder.cancel()
                await asyncio.gather(responder, return_exceptions=True)
        self.assertEqual(
            observed,
            [
                ("public.example", 1),
                ("cdn.example", 1),
                ("public.example", 28),
                ("cdn.example", 28),
            ],
        )

    async def test_malformed_dns_packets_and_alias_cycles_fail_closed(self) -> None:
        hostname = "public.example"
        query = (
            struct.pack("!6H", 7, 0x100, 1, 0, 0, 0)
            + dns_name(hostname)
            + struct.pack("!HH", 1, 1)
        )
        good = dns_packet(query, [(hostname, 1, PUBLIC.packed)])
        malformed = (
            good[:10],
            b"\x00\x08" + good[2:],
            dns_packet(query, [], flags=0x8380),
            dns_packet(query, [], flags=0x8182),
            dns_packet(query, [(hostname, 1, b"\x01\x02\x03")]),
            dns_packet(query, [(hostname, 5, dns_name(hostname))]),
            dns_packet(
                query,
                [
                    (hostname, 5, dns_name("alias.example")),
                    ("alias.example", 5, dns_name(hostname)),
                ],
            ),
            dns_packet(
                query,
                [
                    (hostname, 5, dns_name("first.example")),
                    (hostname, 5, dns_name("second.example")),
                ],
            ),
            dns_packet(query, [(hostname, 1, PUBLIC.packed)], flags=0x8183),
            good + b"extra",
            good[:12] + b"\xc0\x0c" + good[14:],
        )
        self.assertEqual(egress._dns_answers(good, 7, hostname, 1), ((PUBLIC,), None))
        for packet in malformed:
            with self.subTest(packet=packet), self.assertRaises(egress.Rejected):
                egress._dns_answers(packet, 7, hostname, 1)

    async def test_dns_cancellation_closes_its_only_pending_socket(self) -> None:
        waiting = asyncio.Event()
        held: list[socket.socket] = []
        loop = asyncio.get_running_loop()

        async def connect(
            endpoint: socket.socket, destination: tuple[str, int]
        ) -> None:
            held.append(endpoint)

        async def send(endpoint: socket.socket, value: bytes) -> None:
            return

        async def receive(endpoint: socket.socket, size: int) -> bytes:
            waiting.set()
            await asyncio.Event().wait()
            return b""

        with (
            mock.patch.object(loop, "sock_connect", new=connect),
            mock.patch.object(loop, "sock_sendall", new=send),
            mock.patch.object(loop, "sock_recv", new=receive),
        ):
            resolving = asyncio.create_task(
                egress.DnsResolver("127.0.0.1").resolve("public.example")
            )
            await waiting.wait()
            resolving.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await resolving
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].fileno(), -1)


class ControlledDeadline:
    def __init__(self, owner: Deadlines, delay: float | None) -> None:
        self.owner = owner
        self.delay = delay
        self.task: asyncio.Task[None] | None = None
        self.active = False
        self.expired = False

    async def __aenter__(self) -> ControlledDeadline:
        self.task = asyncio.current_task()
        self.active = True
        self.owner.changed.set()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False
        if self.expired and isinstance(exception, asyncio.CancelledError):
            assert self.task is not None
            self.task.uncancel()
            raise TimeoutError from exception

    def expire(self) -> None:
        self.expired = True
        assert self.task is not None
        self.task.cancel()

    def reschedule(self, when: float | None) -> None:
        return


class Deadlines:
    def __init__(self) -> None:
        self.created: list[ControlledDeadline] = []
        self.changed = asyncio.Event()

    def timeout(self, delay: float | None) -> ControlledDeadline:
        deadline = ControlledDeadline(self, delay)
        self.created.append(deadline)
        return deadline

    async def active(self, delay: float) -> ControlledDeadline:
        while True:
            for deadline in self.created:
                if deadline.active and deadline.delay == delay:
                    return deadline
            self.changed.clear()
            await self.changed.wait()


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_deadline_cancels_without_trying_another_address(
        self,
    ) -> None:
        deadlines = Deadlines()
        limits = egress.Limits(header_timeout=11, connect_timeout=22, idle_timeout=33)
        waiting = asyncio.Event()
        held: list[socket.socket] = []
        destinations: list[tuple[str, int]] = []
        loop = asyncio.get_running_loop()
        original = loop.sock_connect

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            return (PUBLIC, OTHER_PUBLIC)

        async def connect(
            endpoint: socket.socket, destination: str | tuple[str, int]
        ) -> None:
            if isinstance(destination, str):
                await original(endpoint, destination)
                return
            held.append(endpoint)
            destinations.append(destination)
            waiting.set()
            await asyncio.Event().wait()

        with (
            mock.patch.object(loop, "sock_connect", new=connect),
            mock.patch.object(egress.asyncio, "timeout", new=deadlines.timeout),
        ):
            async with proxy_running(lookup, limits=limits) as harness:
                reader, writer = await harness.connect()
                writer.write(HEADER)
                await writer.drain()
                await waiting.wait()
                (await deadlines.active(22)).expire()
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 504"))
        self.assertEqual(destinations, [(str(PUBLIC), 80)])
        self.assertEqual(held[0].fileno(), -1)

    async def test_header_deadline_expires_by_explicit_signal(self) -> None:
        deadlines = Deadlines()
        limits = egress.Limits(header_timeout=11, connect_timeout=22, idle_timeout=33)
        reading_http = asyncio.Event()
        original = egress._ProxyConnection._process

        async def process(connection: egress._ProxyConnection) -> None:
            reading_http.set()
            await original(connection)

        with (
            mock.patch.object(egress.asyncio, "timeout", new=deadlines.timeout),
            mock.patch.object(egress._ProxyConnection, "_process", new=process),
        ):
            async with proxy_running(limits=limits) as harness:
                reader, _ = await harness.connect()
                await reading_http.wait()
                (await deadlines.active(11)).expire()
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 504"))

    async def test_resolution_deadline_cancels_owned_resolution(self) -> None:
        deadlines = Deadlines()
        limits = egress.Limits(header_timeout=11, connect_timeout=22, idle_timeout=33)
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return (PUBLIC,)

        with mock.patch.object(egress.asyncio, "timeout", new=deadlines.timeout):
            async with proxy_running(lookup, limits=limits) as harness:
                reader, writer = await harness.connect()
                writer.write(HEADER)
                await writer.drain()
                await waiting.wait()
                (await deadlines.active(22)).expire()
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 504"))
                await cancelled.wait()

    async def test_upstream_header_deadline_closes_the_origin(self) -> None:
        deadlines = Deadlines()
        limits = egress.Limits(header_timeout=11, connect_timeout=22, idle_timeout=33)
        arrived = asyncio.Event()
        ended = asyncio.Event()

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            self.assertEqual(await reader.read(), b"")
            ended.set()

        async with origin_running(origin) as address:
            with (
                route_public(address),
                mock.patch.object(egress.asyncio, "timeout", new=deadlines.timeout),
            ):
                async with proxy_running(limits=limits) as harness:
                    reader, writer = await harness.connect()
                    writer.write(HEADER)
                    await writer.drain()
                    await arrived.wait()
                    (await deadlines.active(11)).expire()
                    self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 504"))
                    await ended.wait()

    async def test_body_idle_deadline_closes_without_emitting_a_second_response(
        self,
    ) -> None:
        deadlines = Deadlines()
        limits = egress.Limits(header_timeout=11, connect_timeout=22, idle_timeout=33)
        ended = asyncio.Event()

        async def origin(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
            await writer.drain()
            self.assertEqual(await reader.read(), b"")
            ended.set()

        async with origin_running(origin) as address:
            with (
                route_public(address),
                mock.patch.object(egress.asyncio, "timeout", new=deadlines.timeout),
            ):
                async with proxy_running(limits=limits) as harness:
                    reader, writer = await harness.connect()
                    writer.write(HEADER)
                    await writer.drain()
                    header = await reader.readuntil(b"\r\n\r\n")
                    self.assertTrue(header.startswith(b"HTTP/1.1 200"))
                    (await deadlines.active(33)).expire()
                    self.assertEqual(await reader.read(), b"")
                    await ended.wait()


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_cli_reports_readiness_and_removes_only_its_socket(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "egress.sock"
            namespace = str(uuid4())
            startup = (
                "import sys;sys.path.insert(0,"
                + repr(str(Path(egress.__file__).resolve().parent))
                + ")\nfrom pathlib import Path\n"
                "from unittest.mock import patch, AsyncMock\n"
                "import lifetime, supervise\nfrom registry import ProjectRegistry\n"
                "with patch.object(lifetime, '_require_tmpfs'), "
                "patch.object(lifetime, 'require_private_storage'), "
                "patch.object(supervise, '_wait_execd_namespace', "
                f"AsyncMock(return_value={namespace!r})), "
                "patch.object(supervise, 'ProjectRegistry', "
                f"side_effect=lambda root, **kwargs: ProjectRegistry(Path({directory!r}) / 'registry', **kwargs)):\n"
                "    supervise.main()\n"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                startup,
                "--directory",
                str(path.parent),
                "--deny-host",
                "localhost",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert process.stdout is not None
                notification = json.loads(await process.stdout.readline())
                self.assertEqual(notification, {"ready": True})
                self.assertEqual(path.stat().st_mode & 0o777, 0o666)
                control = path.parent / "control.sock"
                self.assertEqual(control.stat().st_mode & 0o777, 0o600)
                granted = json.loads(
                    await network.request(
                        {
                            "operation": "grant",
                            "session_id": str(uuid4()),
                            "session_namespace": namespace,
                        },
                        control,
                    )
                )
                reader, writer = await asyncio.open_unix_connection(str(path))
                try:
                    writer.write(
                        granted["token"].encode()
                        + b"\n"
                        + b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n"
                    )
                    await writer.drain()
                    self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 403"))
                finally:
                    writer.close()
                    await writer.wait_closed()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = await process.communicate()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, b"")
                self.assertEqual(stderr, b"")
                self.assertFalse(path.exists())
                self.assertTrue((path.parent / "lifetime").exists())
                self.assertFalse((path.parent / "ready").exists())
                repeated = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    startup,
                    "--directory",
                    str(path.parent),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await repeated.communicate()
                    self.assertNotEqual(repeated.returncode, 0)
                    self.assertEqual(stdout, b"")
                    self.assertIn(b"FileExistsError", stderr)
                finally:
                    if repeated.returncode is None:
                        repeated.kill()
                        await repeated.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()

    async def test_cli_reports_readiness_and_settles_on_sigterm(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import runpy,sys;sys.path.insert(0,"
            + repr(str(Path(relay.__file__).resolve().parent))
            + ");runpy.run_module('relay',run_name='__main__')",
            "--socket",
            "/unused-test-proxy.sock",
            "--port",
            "0",
            "--egress-token",
            "a" * 64,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            notification = json.loads(await process.stdout.readline())
            self.assertIs(notification["ready"], True)
            self.assertGreater(notification["port"], 0)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = await process.communicate()
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    async def test_relay_binds_only_loopback_and_preserves_proxy_policy(self) -> None:
        async with proxy_running() as harness:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(4)
                forwarder = relay.Relay(
                    listener, str(harness.path), egress_token=harness.token
                )
                serving = asyncio.create_task(forwarder.serve())
                reader, writer = await asyncio.open_connection(*listener.getsockname())
                try:
                    writer.write(
                        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n"
                    )
                    await writer.drain()
                    self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 403"))
                finally:
                    writer.close()
                    await writer.wait_closed()
                    await forwarder.aclose()
                    await asyncio.gather(serving, return_exceptions=True)
                self.assertEqual(forwarder.server.active, set())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("0.0.0.0", 0))
            with self.assertRaises(ValueError):
                relay.Relay(listener, "/trusted/proxy.sock", egress_token="a" * 64)

    async def test_relay_shutdown_closes_parent_proxy_connection(self) -> None:
        resolving = asyncio.Event()
        cancelled = asyncio.Event()

        async def lookup(hostname: str) -> tuple[egress.IPAddress, ...]:
            resolving.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return (PUBLIC,)

        async with proxy_running(lookup) as harness:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(4)
                forwarder = relay.Relay(
                    listener, str(harness.path), egress_token=harness.token
                )
                serving = asyncio.create_task(forwarder.serve())
                reader, writer = await asyncio.open_connection(*listener.getsockname())
                try:
                    writer.write(HEADER)
                    await writer.drain()
                    await resolving.wait()
                    await forwarder.aclose()
                    self.assertEqual(await reader.read(), b"")
                    await cancelled.wait()
                    self.assertEqual(forwarder.server.active, set())
                finally:
                    writer.close()
                    await writer.wait_closed()
                    await forwarder.aclose()
                    await asyncio.gather(serving, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
