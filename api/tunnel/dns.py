# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Fake-IP DNS for the tunnel.

A device inside the tunnel asks us for ``example.com`` and gets a synthetic
address from a reserved range. When it then connects to that address, the
transparent listener turns the address back into the name and hands the
*name* to the upstream proxy, which resolves it where the exit is. Nothing
is resolved on this host, so nothing leaks and the exit's view of DNS is the
one that counts. AAAA answers are empty so devices stay on IPv4, and every
other type is answered empty too: the tunnel carries proxied TCP, nothing
else needs a record.

The pool is a bounded LRU: a name keeps its address while it is in use, and
the least recently used name is evicted when the range is exhausted. With
the default /15 that is 130,000 names, far more than a device touches. In a
cluster the mapping is shared through Redis (see :class:`FakeIpDirectory`),
so whichever instance a device's connection lands on knows the name.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from api.db.redis import RedisClient

logger = structlog.get_logger()

QTYPE_A = 1
QTYPE_PTR = 12
QTYPE_AAAA = 28
QTYPE_ANY = 255
QCLASS_IN = 1
QCLASS_ANY = 255

RCODE_OK = 0
RCODE_FORMERR = 1
RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5

_FLAG_QR = 0x8000
_FLAG_AA = 0x0400
_FLAG_RD = 0x0100
_FLAG_RA = 0x0080

MAX_NAME_LENGTH = 253
# How often a name served from the local pool has its shared lease refreshed.
LEASE_REFRESH_SECONDS = 3600.0
_PTR_SUFFIX = ".in-addr.arpa"
# Firefox asks the network's resolver for this name before enabling DNS over
# HTTPS and keeps plain DNS when the answer is NXDOMAIN. Answering it with a
# fake address, as every other name gets, would switch Firefox to DoH and
# take its lookups away from the tunnel resolver.
DOH_CANARY = "use-application-dns.net"
# One TCP query stream is served for this long without a new query.
_TCP_IDLE_SECONDS = 10.0


class FakeIpPool:
    """Names to synthetic IPv4 addresses and back, on one instance.

    Addresses are the network's hosts after the first (``.0`` of the first
    block is skipped so no answer is a network address), identified by an
    *offset* from the first usable one. Offsets are what the cluster shares.
    """

    def __init__(self, network: str, *, ttl: int = 60) -> None:
        self._network = ipaddress.IPv4Network(network)
        self._first = int(self._network.network_address) + 1
        self.capacity = self._network.num_addresses - 2
        if self.capacity < 1:
            raise ValueError("fake IP range must hold at least one address")
        self.ttl = ttl
        self._names: OrderedDict[str, int] = OrderedDict()  # name -> offset, LRU order
        self._offsets: dict[int, str] = {}
        self._next = 0

    @property
    def network(self) -> ipaddress.IPv4Network:
        return self._network

    def __len__(self) -> int:
        return len(self._names)

    def contains(self, ip: str) -> bool:
        try:
            return ipaddress.IPv4Address(ip) in self._network
        except ValueError:
            return False

    def address_of(self, offset: int) -> ipaddress.IPv4Address:
        return ipaddress.IPv4Address(self._first + offset)

    def offset_of_address(self, ip: str) -> int | None:
        """The offset of an address in the range, or None for one outside it."""
        try:
            address = ipaddress.IPv4Address(ip)
        except ValueError:
            return None
        if address not in self._network:
            return None
        offset = int(address) - self._first
        return offset if 0 <= offset < self.capacity else None

    def offset_of(self, name: str) -> int | None:
        """The offset ``name`` holds here, or None; a hit counts as use."""
        key = normalize_name(name)
        offset = self._names.get(key)
        if offset is not None:
            self._names.move_to_end(key)
        return offset

    def ip_for(self, name: str) -> ipaddress.IPv4Address:
        """The address for ``name``, allocating one here if it has none."""
        key = normalize_name(name)
        offset = self.offset_of(key)
        if offset is None:
            offset = self._allocate(key)
        return self.address_of(offset)

    def name_for(self, ip: str) -> str | None:
        """The name an address was handed out for here, or None."""
        offset = self.offset_of_address(ip)
        if offset is None:
            return None
        name = self._offsets.get(offset)
        if name is not None:
            self._names.move_to_end(name)
        return name

    def adopt(self, name: str, offset: int) -> None:
        """Record a mapping decided elsewhere, displacing whatever held either side of it."""
        key = normalize_name(name)
        previous = self._names.pop(key, None)
        if previous is not None:
            self._offsets.pop(previous, None)
        occupant = self._offsets.pop(offset, None)
        if occupant is not None:
            self._names.pop(occupant, None)
        if len(self._names) >= self.capacity:
            evicted, freed = self._names.popitem(last=False)
            del self._offsets[freed]
        self._names[key] = offset
        self._offsets[offset] = key

    def _allocate(self, key: str) -> int:
        if len(self._names) >= self.capacity:
            evicted, offset = self._names.popitem(last=False)
            del self._offsets[offset]
            logger.debug("Fake IP pool full, evicting", name=evicted)
        else:
            while self._next in self._offsets:
                self._next = (self._next + 1) % self.capacity
            offset = self._next
            self._next = (self._next + 1) % self.capacity
        self._names[key] = offset
        self._offsets[offset] = key
        return offset


class FakeIpDirectory:
    """The mapping as the tunnel sees it: the local pool, backed by Redis when the install has one.

    With a store, every allocation is made cluster-wide and the local pool is
    a cache of it, so a connection that lands on a different instance from
    the one that answered the device's DNS query (a UDP balancer rehashing,
    an instance taking over) still turns the address back into the name.
    Without one, or when Redis is unreachable, the pool works alone and the
    tunnel keeps serving; only the cross-instance property is lost.
    """

    def __init__(self, pool: FakeIpPool, store: RedisClient | None = None) -> None:
        self.pool = pool
        self._store = store
        self.store_errors = 0
        # Loop time of each name's last shared-lease refresh from here.
        self._refreshed: dict[str, float] = {}

    @property
    def network(self) -> ipaddress.IPv4Network:
        return self.pool.network

    @property
    def ttl(self) -> int:
        return self.pool.ttl

    def contains(self, ip: str) -> bool:
        return self.pool.contains(ip)

    async def ip_for(self, name: str) -> ipaddress.IPv4Address:
        key = normalize_name(name)
        offset = self.pool.offset_of(key)
        if offset is not None:
            if self._store is not None:
                offset = await self._refresh_lease(key, offset)
            return self.pool.address_of(offset)
        if self._store is not None:
            try:
                shared = await self._store.fakeip_offset_for(key)
                if shared is None:
                    shared = await self._store.fakeip_allocate(key, self.pool.capacity)
            except Exception as exc:
                self._note_store_error(exc)
            else:
                self.pool.adopt(key, shared)
                return self.pool.address_of(shared)
        return self.pool.ip_for(key)

    async def name_for(self, ip: str) -> str | None:
        offset = self.pool.offset_of_address(ip)
        if offset is None:
            return None
        name = self.pool.name_for(ip)
        if name is not None or self._store is None:
            return name
        try:
            shared = await self._store.fakeip_name_for(offset)
        except Exception as exc:
            self._note_store_error(exc)
            return None
        if shared is not None:
            self.pool.adopt(shared, offset)
        return shared

    async def _refresh_lease(self, key: str, offset: int) -> int:
        """Keep the shared mapping of a locally served name alive; the offset to answer with.

        A local hit never touches Redis, so without this the shared keys
        would expire after TUNNEL_FAKEIP_TTL_SECONDS of steady use and
        another carrier could hand the offset to a different name. A round
        trip on every query is too much for the DNS path, so each name is
        refreshed at most once per LEASE_REFRESH_SECONDS. A lease that has
        gone is taken again, adopting whatever the cluster now says.
        """
        store = self._store
        if store is None:
            return offset
        now = asyncio.get_running_loop().time()
        last = self._refreshed.get(key)
        if last is not None and now - last < LEASE_REFRESH_SECONDS:
            return offset
        self._refreshed[key] = now
        if len(self._refreshed) > self.pool.capacity:
            self._refreshed = {k: t for k, t in self._refreshed.items() if now - t < LEASE_REFRESH_SECONDS}
        try:
            shared = await store.fakeip_offset_for(key)
            if shared is None:
                shared = await store.fakeip_allocate(key, self.pool.capacity)
        except Exception as exc:
            self._note_store_error(exc)
            return offset
        if shared != offset:
            self.pool.adopt(key, shared)
        return shared

    def _note_store_error(self, exc: Exception) -> None:
        self.store_errors += 1
        if self.store_errors in (1, 10, 100) or self.store_errors % 1000 == 0:
            logger.warning("Shared fake IP store unavailable, using the local pool", error=str(exc), count=self.store_errors)


def normalize_name(name: str) -> str:
    return name.rstrip(".").lower()


# --- wire format -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    name: str
    qtype: int
    qclass: int
    # Where the question section ends, so a response can copy it verbatim.
    end: int


class MalformedQueryError(ValueError):
    pass


def read_name(message: bytes, offset: int) -> tuple[str, int]:
    """Decode a possibly compressed name; returns the name and the offset after it."""
    labels: list[str] = []
    jumped = False
    end = offset
    hops = 0
    while True:
        if offset >= len(message):
            raise MalformedQueryError("name runs past the message")
        length = message[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(message):
                raise MalformedQueryError("truncated pointer")
            pointer = ((length & 0x3F) << 8) | message[offset + 1]
            if not jumped:
                end = offset + 2
            jumped = True
            hops += 1
            if hops > 16 or pointer >= len(message):
                raise MalformedQueryError("bad pointer")
            offset = pointer
            continue
        offset += 1
        label = message[offset : offset + length]
        if len(label) != length:
            raise MalformedQueryError("truncated label")
        labels.append(label.decode("ascii", "replace"))
        offset += length
    if not jumped:
        end = offset
    return ".".join(labels), end


def parse_question(message: bytes) -> tuple[int, int, Question]:
    """The id, flags and single question of a query, or MalformedQueryError."""
    if len(message) < 12:
        raise MalformedQueryError("short header")
    ident = int.from_bytes(message[0:2], "big")
    flags = int.from_bytes(message[2:4], "big")
    qdcount = int.from_bytes(message[4:6], "big")
    if flags & _FLAG_QR:
        raise MalformedQueryError("not a query")
    if qdcount != 1:
        raise MalformedQueryError("expected one question")
    name, offset = read_name(message, 12)
    if offset + 4 > len(message):
        raise MalformedQueryError("truncated question")
    qtype = int.from_bytes(message[offset : offset + 2], "big")
    qclass = int.from_bytes(message[offset + 2 : offset + 4], "big")
    return ident, flags, Question(name=name, qtype=qtype, qclass=qclass, end=offset + 4)


def encode_name(name: str) -> bytes:
    out = bytearray()
    for label in normalize_name(name).split("."):
        if label:
            raw = label.encode("ascii", "replace")[:63]
            out.append(len(raw))
            out += raw
    out.append(0)
    return bytes(out)


def build_response(
    message: bytes,
    question: Question,
    answers: list[tuple[int, bytes]],
    *,
    rcode: int = RCODE_OK,
    ttl: int,
) -> bytes:
    """A response to ``message``: its question echoed, ``answers`` as (type, rdata) records.

    Answer names point back at the question (offset 12). No additional
    section is returned, so a client's EDNS OPT record is simply not echoed;
    every resolver library tolerates that.
    """
    flags = _FLAG_QR | _FLAG_AA | _FLAG_RA | (int.from_bytes(message[2:4], "big") & _FLAG_RD) | (rcode & 0xF)
    header = (
        message[0:2]
        + flags.to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + len(answers).to_bytes(2, "big")
        + b"\x00\x00\x00\x00"
    )
    body = bytearray(header + message[12 : question.end])
    for rtype, rdata in answers:
        body += b"\xc0\x0c"
        body += rtype.to_bytes(2, "big") + QCLASS_IN.to_bytes(2, "big")
        body += ttl.to_bytes(4, "big") + len(rdata).to_bytes(2, "big") + rdata
    return bytes(body)


def _error_response(message: bytes, rcode: int) -> bytes | None:
    if len(message) < 4:
        return None
    flags = _FLAG_QR | (int.from_bytes(message[2:4], "big") & _FLAG_RD) | rcode
    return message[0:2] + flags.to_bytes(2, "big") + b"\x00" * 8


class FakeIpResolver:
    """Answers tunnel DNS queries from the directory."""

    def __init__(self, directory: FakeIpDirectory) -> None:
        self.directory = directory
        self.queries = 0

    @property
    def pool(self) -> FakeIpPool:
        return self.directory.pool

    async def answer(self, message: bytes) -> bytes | None:
        """The response to one query message, or None when there is nothing sensible to send."""
        self.queries += 1
        ttl = self.directory.ttl
        try:
            _ident, _flags, question = parse_question(message)
        except MalformedQueryError:
            return _error_response(message, RCODE_FORMERR)
        if question.qclass not in (QCLASS_IN, QCLASS_ANY):
            return build_response(message, question, [], rcode=RCODE_REFUSED, ttl=ttl)
        name = normalize_name(question.name)
        if name == DOH_CANARY or name.endswith("." + DOH_CANARY):
            return build_response(message, question, [], rcode=RCODE_NXDOMAIN, ttl=ttl)
        if question.qtype == QTYPE_PTR:
            return await self._answer_ptr(message, question, name)
        if question.qtype in (QTYPE_A, QTYPE_ANY) and _is_hostname(name):
            ip = await self.directory.ip_for(name)
            return build_response(message, question, [(QTYPE_A, ip.packed)], ttl=ttl)
        # AAAA, HTTPS, SRV, TXT and friends: nothing to say, and saying so is
        # what lets the device move on to the A record it already has.
        return build_response(message, question, [], ttl=ttl)

    async def _answer_ptr(self, message: bytes, question: Question, name: str) -> bytes:
        ttl = self.directory.ttl
        if not name.endswith(_PTR_SUFFIX):
            return build_response(message, question, [], ttl=ttl)
        octets = name[: -len(_PTR_SUFFIX)].split(".")
        if len(octets) != 4:
            return build_response(message, question, [], rcode=RCODE_NXDOMAIN, ttl=ttl)
        hostname = await self.directory.name_for(".".join(reversed(octets)))
        if hostname is None:
            return build_response(message, question, [], rcode=RCODE_NXDOMAIN, ttl=ttl)
        return build_response(message, question, [(QTYPE_PTR, encode_name(hostname))], ttl=ttl)


def _is_hostname(name: str) -> bool:
    if not name or len(name) > MAX_NAME_LENGTH:
        return False
    # An address literal is not a name to allocate for; the device would not ask anyway.
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True
    return False


# --- servers ---------------------------------------------------------------------------------


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, resolver: FakeIpResolver) -> None:
        self._resolver = resolver
        self._transport: asyncio.DatagramTransport | None = None
        # Answering may wait on Redis, so each datagram is its own task; the
        # set keeps them referenced until they finish.
        self._tasks: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int] | tuple[str, int, int, int]) -> None:
        task = asyncio.create_task(self._reply(data, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reply(self, data: bytes, addr: tuple[str, int] | tuple[str, int, int, int]) -> None:
        try:
            response = await self._resolver.answer(data)
        except Exception as exc:
            logger.debug("DNS answer failed", error=str(exc))
            return
        if response is not None and self._transport is not None and not self._transport.is_closing():
            self._transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("DNS UDP error", error=str(exc))


class DnsServer:
    """The fake-IP resolver on one UDP and one TCP port, bound on every tunnel gateway it is told to serve."""

    def __init__(self, resolver: FakeIpResolver, port: int) -> None:
        self.resolver = resolver
        self._port = port
        self._listeners: dict[str, tuple[asyncio.Server, asyncio.DatagramTransport]] = {}

    @property
    def is_listening(self) -> bool:
        return any(not udp.is_closing() for _tcp, udp in self._listeners.values())

    @property
    def hosts(self) -> list[str]:
        return list(self._listeners)

    @property
    def port(self) -> int:
        for tcp, _udp in self._listeners.values():
            if tcp.sockets:
                bound: int = tcp.sockets[0].getsockname()[1]
                return bound
        return self._port

    async def listen(self, host: str) -> None:
        """Serve on ``host``. The first bind decides the port when the configured one is 0 (tests)."""
        if host in self._listeners:
            return
        loop = asyncio.get_running_loop()
        tcp = await asyncio.start_server(self._serve_tcp, host, self.port)
        port: int = tcp.sockets[0].getsockname()[1]
        try:
            udp, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self.resolver), local_addr=(host, port)
            )
        except Exception:
            tcp.close()
            raise
        self._listeners[host] = (tcp, udp)
        logger.info("Tunnel DNS listening", host=host, port=port, fake_range=str(self.resolver.directory.network))

    async def unlisten(self, host: str) -> None:
        listener = self._listeners.pop(host, None)
        if listener is None:
            return
        tcp, udp = listener
        udp.close()
        tcp.close()
        await tcp.wait_closed()

    async def stop(self) -> None:
        for host in list(self._listeners):
            await self.unlisten(host)

    async def _serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    prefix = await asyncio.wait_for(reader.readexactly(2), _TCP_IDLE_SECONDS)
                    message = await asyncio.wait_for(
                        reader.readexactly(int.from_bytes(prefix, "big")), _TCP_IDLE_SECONDS
                    )
                except (asyncio.IncompleteReadError, TimeoutError):
                    break
                response = await self.resolver.answer(message)
                if response is None:
                    break
                writer.write(len(response).to_bytes(2, "big") + response)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
