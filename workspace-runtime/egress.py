"""Bounded HTTP egress for isolated runs, served through a trusted Unix socket.

Only the directly connected destination is constrained. CONNECT carries opaque
bytes to public port 443 and does not inspect TLS or remote application behavior.
The trusted supervisor must supply every control-plane hostname or public network
that workloads must not reach. Tests in test_workspace_egress.py define the
accepted HTTP/1.1 subset and its fail-closed framing rules (RFC 9112, sections 3-7).
HTTP/1.0 CONNECT accepts the request emitted by CPython v3.11.15's
http.client.HTTPConnection._tunnel, whose default headers omit Host.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import secrets
import signal
import socket
import struct
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_EXCLUDED = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.88.99.0/24",
        "198.18.0.0/15",
        "2001::/23",
        "2002::/16",
        "3fff::/20",
    )
)


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-service bounds; timeouts are seconds and sizes are bytes.

    Each input queue contains at most two chunks. Parsers and transfers hold
    bounded headers or chunks, with at most one upstream socket per client. The
    operating system also bounds socket buffers. Request bodies stream and are
    limited to one GiB; downloads have no total size limit. The owner must stop
    the service when its run ends.
    """

    clients: int = 64
    headers: int = 16 * 1024
    header_fields: int = 100
    chunk: int = 64 * 1024
    request_body: int = 1024 * 1024 * 1024
    header_timeout: float = 15
    connect_timeout: float = 15
    idle_timeout: float = 60


class Rejected(Exception):
    """A request cannot be safely forwarded; its status is safe to disclose."""

    def __init__(self, status: int = 400) -> None:
        super().__init__(status)
        self.status = status


def _host(value: str) -> str:
    if not value or any(character in value for character in "%@/\\?#"):
        raise Rejected()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        normalized = value.lower().removesuffix(".")
        if (
            len(normalized) > 253
            or ":" in normalized
            or not all(_LABEL.fullmatch(label) for label in normalized.split("."))
            or all(character in "0123456789." for character in normalized)
        ):
            raise Rejected() from None
        return normalized


def _authority(value: str, port: int, *, require_port: bool = False) -> str:
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            raise Rejected()
        hostname = _host(value[1:closing])
        try:
            if ipaddress.ip_address(hostname).version != 6:
                raise Rejected()
        except ValueError:
            raise Rejected() from None
        suffix = value[closing + 1 :]
    else:
        hostname, separator, raw_port = value.partition(":")
        hostname = _host(hostname)
        suffix = separator + raw_port
    if suffix and suffix != f":{port}":
        raise Rejected(403)
    if require_port and not suffix:
        raise Rejected()
    return hostname


def _public(address: IPAddress, denied: Sequence[IPNetwork]) -> bool:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or any(address in network for network in _EXCLUDED)
    ):
        return False
    if isinstance(
        address, ipaddress.IPv6Address
    ) and address not in ipaddress.ip_network("2000::/3"):
        return False
    return not any(address in network for network in denied)


def _dns_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    visited: set[int] = set()
    end = 0
    for _ in range(128):
        if offset >= len(packet) or offset in visited:
            raise Rejected(502)
        visited.add(offset)
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise Rejected(502)
            if not end:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | packet[offset + 1]
        elif length & 0xC0:
            raise Rejected(502)
        elif not length:
            return ".".join(labels), end or offset + 1
        else:
            offset += 1
            if offset + length > len(packet):
                raise Rejected(502)
            try:
                label = packet[offset : offset + length].decode("ascii").lower()
            except UnicodeDecodeError:
                raise Rejected(502) from None
            if not _LABEL.fullmatch(label):
                raise Rejected(502)
            labels.append(label)
            if sum(map(len, labels)) + len(labels) > 254:
                raise Rejected(502)
            offset += length
    raise Rejected(502)


def _dns_answers(
    packet: bytes, identifier: int, hostname: str, kind: int
) -> tuple[tuple[IPAddress, ...], str | None]:
    if len(packet) < 12:
        raise Rejected(502)
    received, flags, questions, answers, authorities, additional = struct.unpack_from(
        "!6H", packet
    )
    if (
        received != identifier
        or flags & 0xF800 != 0x8000
        or flags & 0x0200
        or flags & 0x000F not in (0, 3)
        or questions != 1
        or answers + authorities + additional > 256
    ):
        raise Rejected(502)
    question, offset = _dns_name(packet, 12)
    if (
        question != hostname
        or offset + 4 > len(packet)
        or struct.unpack_from("!HH", packet, offset) != (kind, 1)
    ):
        raise Rejected(502)
    offset += 4
    aliases: dict[str, str] = {}
    addresses: list[tuple[str, IPAddress]] = []
    for index in range(answers + authorities + additional):
        owner, offset = _dns_name(packet, offset)
        if offset + 10 > len(packet):
            raise Rejected(502)
        record_kind, record_class, _, length = struct.unpack_from(
            "!HHIH", packet, offset
        )
        offset += 10
        end = offset + length
        if end > len(packet):
            raise Rejected(502)
        if index < answers and record_class == 1:
            if record_kind in (1, 28):
                if length != (4 if record_kind == 1 else 16):
                    raise Rejected(502)
                addresses.append((owner, ipaddress.ip_address(packet[offset:end])))
            elif record_kind == 5:
                alias, alias_end = _dns_name(packet, offset)
                if alias_end != end or (owner in aliases and aliases[owner] != alias):
                    raise Rejected(502)
                aliases[owner] = alias
        offset = end
    if offset != len(packet):
        raise Rejected(502)
    canonical = hostname
    chain = {hostname}
    while canonical in aliases:
        canonical = aliases[canonical]
        if canonical in chain or len(chain) >= 16:
            raise Rejected(502)
        chain.add(canonical)
    resolved = tuple(address for owner, address in addresses if owner in chain)
    if flags & 0x000F == 3 and (resolved or aliases):
        raise Rejected(502)
    return resolved, canonical if canonical != hostname and not resolved else None


class DnsResolver:
    """Resolve through one trusted numeric DNS server without worker threads.

    A and AAAA queries use connected UDP sockets and bounded DNS packets. There
    are no retries, search suffixes, alternate resolvers, or caches. Truncated or
    malformed replies fail closed. CNAME following is bounded to sixteen names.
    Numeric addresses and the RFC 6761 localhost namespace require no DNS query.
    The caller supplies the overall resolution deadline and owns cancellation.
    """

    def __init__(self, nameserver: str) -> None:
        self.nameserver = ipaddress.ip_address(nameserver)
        if (
            isinstance(self.nameserver, ipaddress.IPv6Address)
            and self.nameserver.scope_id is not None
        ):
            raise ValueError("Scoped DNS addresses are not supported")

    async def resolve(self, hostname: str) -> tuple[IPAddress, ...]:
        """Return every resolved address, or reject an empty/invalid answer."""
        hostname = _host(hostname)
        try:
            return (ipaddress.ip_address(hostname),)
        except ValueError:
            pass
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"))
        ipv4 = await self._resolve_kind(hostname, 1)
        ipv6 = await self._resolve_kind(hostname, 28)
        addresses = tuple(dict.fromkeys((*ipv4, *ipv6)))
        if not addresses:
            raise Rejected(502)
        return addresses

    async def _resolve_kind(self, hostname: str, kind: int) -> tuple[IPAddress, ...]:
        visited: set[str] = set()
        while len(visited) < 16 and hostname not in visited:
            visited.add(hostname)
            identifier = secrets.randbits(16)
            question = b"".join(
                bytes([len(label)]) + label.encode("ascii")
                for label in hostname.split(".")
            )
            query = struct.pack("!6H", identifier, 0x0100, 1, 0, 0, 0)
            query += question + b"\x00" + struct.pack("!HH", kind, 1)
            family = socket.AF_INET if self.nameserver.version == 4 else socket.AF_INET6
            with socket.socket(family, socket.SOCK_DGRAM) as endpoint:
                endpoint.setblocking(False)
                loop = asyncio.get_running_loop()
                await loop.sock_connect(endpoint, (str(self.nameserver), 53))
                await loop.sock_sendall(endpoint, query)
                packet = await loop.sock_recv(endpoint, 65535)
            addresses, alias = _dns_answers(packet, identifier, hostname, kind)
            if not alias:
                return addresses
            hostname = alias
        raise Rejected(502)


@dataclass(frozen=True, slots=True)
class Request:
    """One validated destination and unambiguous request-body framing."""

    hostname: str
    port: int
    tunnel: bool
    header: bytes
    length: int
    chunked: bool
    head: bool


def parse_request(header: bytes, limits: Limits) -> Request:
    """Validate one proxy request and rebuild its HTTP origin headers."""
    if len(header) > limits.headers or not header.endswith(b"\r\n\r\n"):
        raise Rejected(431)
    lines = header[:-4].split(b"\r\n")
    first = lines[0].split(b" ")
    if len(first) != 3 or not _TOKEN.fullmatch(first[0]):
        raise Rejected()
    method, target, version = first
    tunnel = method == b"CONNECT"
    if version != b"HTTP/1.1" and not (tunnel and version == b"HTTP/1.0"):
        raise Rejected(505)
    if any(character <= 32 or character >= 127 for character in target):
        raise Rejected()
    port = 443 if tunnel else 80
    if tunnel:
        hostname = _authority(target.decode("ascii"), port, require_port=True)
        path = b""
    else:
        match = re.fullmatch(rb"http://([^/?#]+)([^#]*)", target)
        if not match or b"\\" in target or method.upper() != method:
            raise Rejected()
        hostname = _authority(match[1].decode("ascii"), port)
        path = match[2] or b"/"
        if path.startswith(b"?"):
            path = b"/" + path
    if len(lines) - 1 > limits.header_fields:
        raise Rejected(431)
    fields: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or not _TOKEN.fullmatch(name) or name in fields:
            raise Rejected()
        if any(character < 32 or character == 127 for character in value):
            raise Rejected()
        fields[name] = value.strip(b" ")
    if b"host" in fields:
        try:
            host_header = fields[b"host"].decode("ascii")
        except UnicodeDecodeError:
            raise Rejected() from None
        if _authority(host_header, port, require_port=tunnel) != hostname:
            raise Rejected()
    elif version == b"HTTP/1.1":
        raise Rejected()
    if any(name in fields for name in (b"upgrade", b"trailer", b"te", b"expect")):
        raise Rejected()
    connection = fields.get(b"connection", b"close").lower().split(b",")
    if any(token.strip() not in (b"close", b"keep-alive") for token in connection):
        raise Rejected()
    chunked = b"transfer-encoding" in fields
    if chunked and (
        fields[b"transfer-encoding"].lower() != b"chunked"
        or b"content-length" in fields
        or tunnel
    ):
        raise Rejected()
    length_field = fields.get(b"content-length", b"0")
    if not re.fullmatch(rb"[0-9]{1,19}", length_field):
        raise Rejected()
    length = int(length_field)
    if length > limits.request_body:
        raise Rejected(413)
    if tunnel and length:
        raise Rejected()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    forwarded = [method + b" " + path + b" HTTP/1.1", b"Host: " + authority.encode()]
    removed = {
        b"host",
        b"connection",
        b"keep-alive",
        b"proxy-connection",
        b"proxy-authorization",
        b"content-length",
        b"transfer-encoding",
    }
    forwarded.extend(
        name + b": " + value for name, value in fields.items() if name not in removed
    )
    if chunked:
        forwarded.append(b"Transfer-Encoding: chunked")
    elif b"content-length" in fields:
        forwarded.append(f"Content-Length: {length}".encode())
    forwarded.append(b"Connection: close\r\n\r\n")
    return Request(
        hostname,
        port,
        tunnel,
        b"\r\n".join(forwarded),
        length,
        chunked,
        method == b"HEAD",
    )


@dataclass(frozen=True, slots=True)
class Response:
    """A validated origin response with one explicit body boundary."""

    status: int
    header: bytes
    length: int | None
    chunked: bool


def parse_response(header: bytes, limits: Limits) -> Response:
    """Bound origin headers and prevent persistence or ambiguous body framing."""
    if len(header) > limits.headers or not header.endswith(b"\r\n\r\n"):
        raise Rejected(502)
    lines = header[:-4].split(b"\r\n")
    match = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [ -~]*)?", lines[0])
    if not match or len(lines) - 1 > limits.header_fields:
        raise Rejected(502)
    status = int(match[1])
    if status == 101:
        raise Rejected(502)
    fields: list[tuple[bytes, bytes]] = []
    framing: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or not _TOKEN.fullmatch(name):
            raise Rejected(502)
        if any(character < 32 or character == 127 for character in value):
            raise Rejected(502)
        value = value.strip(b" ")
        if name in (b"content-length", b"transfer-encoding", b"connection"):
            if name in framing:
                raise Rejected(502)
            framing[name] = value
        if name in (b"upgrade", b"trailer"):
            raise Rejected(502)
        fields.append((name, value))
    connection = framing.get(b"connection", b"close").lower().split(b",")
    if any(token.strip() not in (b"close", b"keep-alive") for token in connection):
        raise Rejected(502)
    chunked = b"transfer-encoding" in framing
    if chunked and (
        framing[b"transfer-encoding"].lower() != b"chunked"
        or b"content-length" in framing
    ):
        raise Rejected(502)
    length: int | None = None
    if b"content-length" in framing:
        if not re.fullmatch(rb"[0-9]{1,19}", framing[b"content-length"]):
            raise Rejected(502)
        length = int(framing[b"content-length"])
    if (status < 200 or status == 204) and (chunked or length is not None):
        raise Rejected(502)
    forwarded = [lines[0]]
    removed = {
        b"connection",
        b"keep-alive",
        b"proxy-connection",
        b"content-length",
        b"transfer-encoding",
    }
    forwarded.extend(
        name + b": " + value for name, value in fields if name not in removed
    )
    if chunked:
        forwarded.append(b"Transfer-Encoding: chunked")
    elif length is not None:
        forwarded.append(f"Content-Length: {length}".encode())
    forwarded.append(b"Connection: close\r\n\r\n")
    return Response(status, b"\r\n".join(forwarded), length, chunked)


async def _first_completed(*operations: Coroutine[None, None, None | bool]) -> None:
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _InputBuffer:
    """Consume bounded chunks without losing delimiters split across reads."""

    def __init__(self, receive: Callable[[], Awaitable[bytes]]) -> None:
        self.next_chunk = receive
        self.buffer = b""

    async def read(self, size: int) -> bytes:
        if not self.buffer:
            self.buffer = await self.next_chunk()
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    async def until(self, delimiter: bytes, limit: int) -> bytes:
        value = bytearray()
        while True:
            if not self.buffer:
                self.buffer = await self.next_chunk()
                if not self.buffer:
                    raise Rejected(502)
            position = self.buffer.find(delimiter)
            if position >= 0:
                length = position + len(delimiter)
                value.extend(self.buffer[:length])
                self.buffer = self.buffer[length:]
                if len(value) > limit:
                    raise Rejected(431)
                return bytes(value)
            value.extend(self.buffer)
            self.buffer = b""
            if len(value) > limit:
                raise Rejected(431)
            self.buffer = await self.next_chunk()
            if not self.buffer:
                raise Rejected(502)
            overlap = min(len(value), len(delimiter) - 1)
            self.buffer = (
                bytes(value[-overlap:]) + self.buffer if overlap else self.buffer
            )
            if overlap:
                del value[-overlap:]


class _Input(_InputBuffer):
    """Bound client input while observing EOF during DNS and connection setup."""

    def __init__(self, endpoint: socket.socket, chunk: int) -> None:
        self.endpoint = endpoint
        self.chunk = chunk
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(2)
        self.closed = asyncio.Event()
        super().__init__(self.queue.get)

    async def receive(self) -> None:
        try:
            while True:
                value = await asyncio.get_running_loop().sock_recv(
                    self.endpoint, self.chunk
                )
                if not value:
                    return
                await self.queue.put(value)
        except OSError:
            return
        finally:
            self.closed.set()


async def _send(endpoint: socket.socket, value: bytes, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        await asyncio.get_running_loop().sock_sendall(endpoint, value)


async def _body_marker(source: _InputBuffer, limits: Limits) -> None:
    marker = bytearray()
    async with asyncio.timeout(limits.idle_timeout):
        while len(marker) < 2:
            value = await source.read(2 - len(marker))
            if not value:
                raise Rejected()
            marker.extend(value)
    if marker != b"\r\n":
        raise Rejected()


async def _copy_body(
    source: _InputBuffer,
    destination: socket.socket,
    limits: Limits,
    length: int | None,
    chunked: bool,
    maximum: int | None = None,
) -> None:
    async def copy(remaining: int | None) -> None:
        while remaining is None or remaining:
            size = limits.chunk if remaining is None else min(remaining, limits.chunk)
            async with asyncio.timeout(limits.idle_timeout):
                value = await source.read(size)
            if not value:
                if remaining is not None:
                    raise Rejected()
                return
            await _send(destination, value, limits.idle_timeout)
            if remaining is not None:
                remaining -= len(value)

    if not chunked:
        await copy(length)
        return
    total = 0
    while True:
        async with asyncio.timeout(limits.idle_timeout):
            line = await source.until(b"\r\n", 32)
        if not re.fullmatch(rb"[0-9a-fA-F]{1,16}\r\n", line):
            raise Rejected()
        amount = int(line[:-2], 16)
        total += amount
        if maximum is not None and total > maximum:
            raise Rejected(413)
        if not amount:
            await _body_marker(source, limits)
            await _send(destination, b"0\r\n\r\n", limits.idle_timeout)
            return
        await _send(destination, f"{amount:x}\r\n".encode(), limits.idle_timeout)
        await copy(amount)
        await _body_marker(source, limits)
        await _send(destination, b"\r\n", limits.idle_timeout)


async def _tunnel(
    incoming: _Input, client: socket.socket, upstream: socket.socket, limits: Limits
) -> None:
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(limits.idle_timeout) as deadline:

        async def upload() -> None:
            while True:
                value = await incoming.read(limits.chunk)
                await _send(upstream, value, limits.idle_timeout)
                deadline.reschedule(loop.time() + limits.idle_timeout)

        async def download() -> None:
            while value := await loop.sock_recv(upstream, limits.chunk):
                await _send(client, value, limits.idle_timeout)
                deadline.reschedule(loop.time() + limits.idle_timeout)

        await _first_completed(upload(), download())


class _ConnectionServer:
    """Own the listener and all accepted tasks until awaited shutdown finishes."""

    def __init__(self, listener: socket.socket, clients: int) -> None:
        self.listener = listener
        self.listener.setblocking(False)
        self.clients = clients
        self.task: asyncio.Task[None] | None = None
        self.active: set[asyncio.Task[None]] = set()
        self.endpoints: set[socket.socket] = set()
        self.closed = False

    async def serve(self, handle: Callable[[socket.socket], Awaitable[None]]) -> None:
        if self.task is not None or self.closed:
            raise RuntimeError("This listener cannot be served again")
        self.task = asyncio.current_task()
        try:
            async with asyncio.TaskGroup() as connections:
                try:
                    while True:
                        client, _ = await asyncio.get_running_loop().sock_accept(
                            self.listener
                        )
                        if len(self.active) >= self.clients:
                            client.close()
                            continue
                        client.setblocking(False)
                        self.endpoints.add(client)
                        task = connections.create_task(self._handle(client, handle))
                        self.active.add(task)
                        task.add_done_callback(self.active.discard)
                finally:
                    self.listener.close()
                    for client in self.endpoints:
                        client.close()
                    for task in self.active:
                        task.cancel()
        finally:
            self.endpoints.clear()
            self.closed = True

    async def _handle(
        self, client: socket.socket, handle: Callable[[socket.socket], Awaitable[None]]
    ) -> None:
        try:
            await handle(client)
        finally:
            client.close()
            self.endpoints.discard(client)

    async def aclose(self) -> None:
        self.listener.close()
        self.closed = True
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class EgressProxy:
    """Serve one public HTTP destination per owned Unix connection.

    Args:
        listener: Bound Unix stream listener whose ownership transfers here.
        resolver: Trusted numeric DNS resolver; never exposed to the workload.
        deny_hosts: Protected authorities, resolved again for every connection.
        deny_networks: Protected public addresses or networks in CIDR notation.
        limits: Bounded concurrency, buffering, body size, and deadlines.

    Cancellation closes every client and upstream and waits for owned tasks.
    Request bodies accept Content-Length or chunked encoding without extensions
    or trailers. HTTP keepalive, pipelining, upgrades, and redirects are not
    followed; redirects are returned for the client to make a new checked request.
    """

    def __init__(
        self,
        listener: socket.socket,
        resolver: DnsResolver,
        *,
        deny_hosts: Sequence[str] = (),
        deny_networks: Sequence[str] = (),
        limits: Limits = Limits(),
    ) -> None:
        if listener.family != socket.AF_UNIX:
            raise ValueError("Egress requires a Unix listener")
        self.resolver = resolver
        self.deny_hosts = tuple(dict.fromkeys(_host(value) for value in deny_hosts))
        self.deny_networks = tuple(
            ipaddress.ip_network(value) for value in deny_networks
        )
        self.limits = limits
        self.server = _ConnectionServer(listener, limits.clients)
        self._runs: dict[tuple[str, str], _RunAccess] = {}
        self._tokens: dict[str, _RunAccess] = {}

    async def serve(self) -> None:
        """Accept clients until cancelled; shutdown waits for all owned tasks."""
        await self.server.serve(self._handle)

    async def aclose(self) -> None:
        """Close all owned connections and await server termination; repeatable."""
        await self.server.aclose()
        for access in self._runs.values():
            if access.closing is not None:
                await asyncio.shield(access.closing)

    def grant_run_egress(self, session_id: str, session_namespace: str) -> str:
        """Authorize a known Run; a revoked identity can never be authorized again.

        The supervisor retains at most 65,536 identities and 1,024 live grants.
        Full ledgers reject new grants rather than forgetting cancellation. Tokens
        authorize only this proxy and never appear in forwarded HTTP requests.
        """
        owner = _run_owner(session_id, session_namespace)
        access = self._runs.get(owner)
        if access is not None:
            if access.token is None:
                raise Rejected(403)
            return access.token
        if self.server.closed or len(self._runs) >= 65536 or len(self._tokens) >= 1024:
            raise Rejected(503)
        token = secrets.token_hex(32)
        if token in self._tokens:
            raise Rejected(503)
        access = _RunAccess(token)
        self._runs[owner] = access
        self._tokens[token] = access
        return token

    async def revoke_run_egress(self, session_id: str, session_namespace: str) -> None:
        """Fence late grants, then await every connection's complete shutdown.

        A disconnected control caller cannot cancel the retained drain task.
        Authentication and connection registration have no await between them, so
        revocation includes all admitted HTTP, DNS and upstream socket activity.
        """
        owner = _run_owner(session_id, session_namespace)
        access = self._runs.get(owner)
        if access is None:
            if len(self._runs) < 65536:
                self._runs[owner] = _RunAccess(None)
            return
        if access.token is not None:
            self._tokens.pop(access.token)
            access.token = None
        if access.closing is None:
            connections = tuple(access.connections)

            async def drain() -> None:
                for task in connections:
                    task.cancel()
                await asyncio.gather(*connections, return_exceptions=True)

            access.closing = asyncio.create_task(drain(), name="workspace-egress-drain")
        await asyncio.shield(access.closing)

    async def _address(self, request: Request) -> IPAddress:
        if request.hostname in self.deny_hosts:
            raise Rejected(403)
        async with asyncio.timeout(self.limits.connect_timeout):
            addresses = await self.resolver.resolve(request.hostname)
            if not addresses or any(
                not _public(address, self.deny_networks) for address in addresses
            ):
                raise Rejected(403)
            protected: set[IPAddress] = set()
            for hostname in self.deny_hosts:
                protected.update(await self.resolver.resolve(hostname))
            if protected.intersection(addresses):
                raise Rejected(403)
        return addresses[0]

    async def _handle(self, client: socket.socket) -> None:
        prelude = bytearray()
        try:
            async with asyncio.timeout(self.limits.header_timeout):
                while len(prelude) < 65:
                    value = await asyncio.get_running_loop().sock_recv(
                        client, 65 - len(prelude)
                    )
                    if not value:
                        return
                    prelude.extend(value)
        except (OSError, TimeoutError):
            return
        if re.fullmatch(rb"[0-9a-f]{64}\n", prelude) is None:
            return
        access = self._tokens.get(prelude[:-1].decode("ascii"))
        if access is None:
            return
        task: asyncio.Task[None] | None = asyncio.current_task()
        assert task is not None
        access.connections.add(task)
        try:
            await _ProxyConnection(self, client).run()
        finally:
            access.connections.discard(task)


def _run_owner(session_id: str, session_namespace: str) -> tuple[str, str]:
    for value in (session_id, session_namespace):
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Run identities must be canonical UUIDs")
    return session_namespace, session_id


class _RunAccess:
    """Own all admitted connections and the retained, repeatable revocation task."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.connections: set[asyncio.Task[None]] = set()
        self.closing: asyncio.Task[None] | None = None


class _ProxyConnection:
    def __init__(self, proxy: EgressProxy, client: socket.socket) -> None:
        self.proxy = proxy
        self.client = client
        self.incoming = _Input(client, proxy.limits.chunk)
        self.upstream: socket.socket | None = None
        self.response_started = False

    async def run(self) -> None:
        receiver = asyncio.create_task(self.incoming.receive())
        try:
            try:
                await _first_completed(self._process(), self.incoming.closed.wait())
            except Rejected as failure:
                await self._error(failure.status)
            except TimeoutError:
                await self._error(504)
            except OSError:
                await self._error(502)
        finally:
            if self.upstream is not None:
                self.upstream.close()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def _error(self, status: int) -> None:
        if self.response_started or self.incoming.closed.is_set():
            return
        self.response_started = True
        message = f"HTTP/1.1 {status} Request rejected\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        with contextlib.suppress(OSError, TimeoutError):
            await _send(self.client, message.encode(), self.proxy.limits.idle_timeout)

    async def _process(self) -> None:
        limits = self.proxy.limits
        async with asyncio.timeout(limits.header_timeout):
            header = await self.incoming.until(b"\r\n\r\n", limits.headers)
        request = parse_request(header, limits)
        address = await self.proxy._address(request)
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        self.upstream = socket.socket(family, socket.SOCK_STREAM)
        self.upstream.setblocking(False)
        async with asyncio.timeout(limits.connect_timeout):
            await asyncio.get_running_loop().sock_connect(
                self.upstream, (str(address), request.port)
            )
        if request.tunnel:
            self.response_started = True
            await _send(
                self.client,
                b"HTTP/1.1 200 Connection established\r\n\r\n",
                limits.idle_timeout,
            )
            await _tunnel(self.incoming, self.client, self.upstream, limits)
        else:
            await _send(self.upstream, request.header, limits.idle_timeout)
            await _first_completed(self._upload(request), self._download(request))

    async def _upload(self, request: Request) -> None:
        limits = self.proxy.limits
        assert self.upstream is not None
        await _copy_body(
            self.incoming,
            self.upstream,
            limits,
            request.length,
            request.chunked,
            limits.request_body,
        )
        await self.incoming.read(1)
        raise Rejected()

    async def _download(self, request: Request) -> None:
        assert self.upstream is not None
        limits = self.proxy.limits
        upstream = self.upstream

        async def receive() -> bytes:
            return await asyncio.get_running_loop().sock_recv(upstream, limits.chunk)

        source = _InputBuffer(receive)
        try:
            async with asyncio.timeout(limits.header_timeout):
                for _ in range(9):
                    header = await source.until(b"\r\n\r\n", limits.headers)
                    response = parse_response(header, limits)
                    if response.status >= 200:
                        break
                else:
                    raise Rejected(502)
            self.response_started = True
            await _send(self.client, response.header, limits.idle_timeout)
            if not request.head and response.status not in (204, 304):
                await _copy_body(
                    source, self.client, limits, response.length, response.chunked
                )
        except Rejected as failure:
            raise Rejected(502) from failure


@contextlib.contextmanager
def unix_listener(
    path: Path, clients: int, *, mode: int = 0o666
) -> Iterator[socket.socket]:
    """Bind a new proxy socket; never replace or remove a pre-existing file.

    The parent directory must be owned and protected by the trusted supervisor.
    The socket permits workload connections but exposes only the proxy protocol.
    Filesystem setup and teardown run outside the asynchronous service loop.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        identity = path.stat()
        try:
            path.chmod(mode)
            listener.listen(clients)
            yield listener
        finally:
            with contextlib.suppress(FileNotFoundError):
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    path.unlink()


async def serve_until_signal(operation: Coroutine[None, None, None]) -> None:
    """Settle service shutdown after SIGINT or SIGTERM without orphaned tasks."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(number, stopping.set)
    try:
        await _first_completed(operation, stopping.wait())
    finally:
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(number)


def _nameserver() -> str:
    for line in Path("/etc/resolv.conf").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            return str(ipaddress.ip_address(fields[1]))
    raise ValueError("No numeric nameserver is configured")
