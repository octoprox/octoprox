# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The fake-IP pool and the resolver's wire format."""

import asyncio
import socket

import pytest

from api.tunnel.dns import (
    QTYPE_A,
    QTYPE_AAAA,
    QTYPE_PTR,
    RCODE_FORMERR,
    RCODE_NXDOMAIN,
    DnsServer,
    FakeIpDirectory,
    FakeIpPool,
    FakeIpResolver,
    encode_name,
    parse_question,
)


def query(name: str, qtype: int, ident: int = 0x1234, rd: bool = True) -> bytes:
    flags = 0x0100 if rd else 0
    return (
        ident.to_bytes(2, "big") + flags.to_bytes(2, "big") + b"\x00\x01" + b"\x00\x00" * 3
        + encode_name(name) + qtype.to_bytes(2, "big") + b"\x00\x01"
    )


def parse_answers(response: bytes) -> tuple[int, int, list[tuple[int, bytes]]]:
    """(rcode, ancount, [(type, rdata)])."""
    flags = int.from_bytes(response[2:4], "big")
    ancount = int.from_bytes(response[6:8], "big")
    _ident, _flags, question = parse_question(response[:2] + b"\x00\x00" + response[4:])
    offset = question.end
    answers = []
    for _ in range(ancount):
        assert response[offset : offset + 2] == b"\xc0\x0c"
        rtype = int.from_bytes(response[offset + 2 : offset + 4], "big")
        rdlength = int.from_bytes(response[offset + 10 : offset + 12], "big")
        answers.append((rtype, response[offset + 12 : offset + 12 + rdlength]))
        offset += 12 + rdlength
    return flags & 0xF, ancount, answers


class TestPool:
    def test_stable_allocation_and_reverse(self) -> None:
        pool = FakeIpPool("198.18.0.0/15")
        a = pool.ip_for("Example.COM.")
        assert str(a) == "198.18.0.1"
        assert pool.ip_for("example.com") == a
        assert str(pool.ip_for("other.test")) == "198.18.0.2"
        assert pool.name_for(str(a)) == "example.com"
        assert pool.name_for("198.18.0.200") is None
        assert pool.name_for("8.8.8.8") is None
        assert pool.contains("198.19.255.1") and not pool.contains("10.0.0.1")

    def test_adopt_displaces_both_sides(self) -> None:
        pool = FakeIpPool("198.18.0.0/15")
        a = pool.ip_for("a.test")  # offset 0
        pool.ip_for("b.test")      # offset 1
        pool.adopt("c.test", 1)    # c takes b's address
        assert pool.name_for("198.18.0.2") == "c.test" and pool.offset_of("b.test") is None
        pool.adopt("a.test", 5)    # a moves; its old address is free
        assert pool.name_for(str(a)) is None and str(pool.ip_for("a.test")) == "198.18.0.6"
        assert pool.offset_of_address("198.18.0.6") == 5 and pool.offset_of_address("10.0.0.1") is None

    def test_lru_eviction_when_full(self) -> None:
        pool = FakeIpPool("198.18.0.0/30")  # capacity 2
        first = pool.ip_for("a.test")
        pool.ip_for("b.test")
        pool.ip_for("a.test")  # a is now most recently used
        third = pool.ip_for("c.test")
        assert third != first
        assert pool.name_for(str(first)) == "a.test"
        assert pool.name_for(str(third)) == "c.test"
        assert len(pool) == 2


class TestResolver:
    def setup_method(self) -> None:
        self.pool = FakeIpPool("198.18.0.0/15", ttl=60)
        self.resolver = FakeIpResolver(FakeIpDirectory(self.pool))

    async def test_a_query_gets_a_fake_ip(self) -> None:
        response = await self.resolver.answer(query("example.com", QTYPE_A))
        assert response is not None
        assert response[:2] == b"\x12\x34"
        assert int.from_bytes(response[2:4], "big") & 0x8000  # QR
        assert int.from_bytes(response[2:4], "big") & 0x0100  # RD echoed
        rcode, ancount, answers = parse_answers(response)
        assert rcode == 0 and ancount == 1
        assert answers[0][0] == QTYPE_A
        assert socket.inet_ntoa(answers[0][1]) == "198.18.0.1"
        assert self.pool.name_for("198.18.0.1") == "example.com"

    async def test_firefox_canary_is_nxdomain(self) -> None:
        # Firefox keeps plain DNS, and so the tunnel resolver, when the canary does not resolve.
        for name in ("use-application-dns.net", "foo.use-application-dns.net"):
            response = await self.resolver.answer(query(name, QTYPE_A))
            assert response is not None
            rcode, ancount, _ = parse_answers(response)
            assert (rcode, ancount) == (RCODE_NXDOMAIN, 0)

    async def test_aaaa_is_empty_noerror(self) -> None:
        response = await self.resolver.answer(query("example.com", QTYPE_AAAA))
        assert response is not None
        rcode, ancount, _ = parse_answers(response)
        assert (rcode, ancount) == (0, 0)

    async def test_ptr_reverses_and_unknown_is_nxdomain(self) -> None:
        self.pool.ip_for("tv.example.net")
        response = await self.resolver.answer(query("1.0.18.198.in-addr.arpa", QTYPE_PTR))
        assert response is not None
        rcode, ancount, answers = parse_answers(response)
        assert (rcode, ancount) == (0, 1)
        assert answers[0] == (QTYPE_PTR, encode_name("tv.example.net"))
        unknown = await self.resolver.answer(query("9.9.18.198.in-addr.arpa", QTYPE_PTR))
        assert unknown is not None
        assert parse_answers(unknown)[0] == RCODE_NXDOMAIN

    async def test_malformed_is_formerr(self) -> None:
        response = await self.resolver.answer(b"\x00\x01\x01\x00\x00\x01")
        assert response is not None
        assert int.from_bytes(response[2:4], "big") & 0xF == RCODE_FORMERR
        assert await self.resolver.answer(b"\x00") is None

    async def test_responses_are_not_queries(self) -> None:
        reply = await self.resolver.answer(query("example.com", QTYPE_A))
        assert reply is not None
        again = await self.resolver.answer(reply)
        assert again is not None
        assert int.from_bytes(again[2:4], "big") & 0xF == RCODE_FORMERR


@pytest.mark.asyncio
async def test_server_answers_udp_and_tcp() -> None:
    pool = FakeIpPool("198.18.0.0/15")
    server = DnsServer(FakeIpResolver(FakeIpDirectory(pool)), 0)
    await server.listen("127.0.0.1")
    try:
        assert server.is_listening
        loop = asyncio.get_running_loop()

        class _Client(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.received: asyncio.Future[bytes] = loop.create_future()

            def datagram_received(self, data: bytes, addr: object) -> None:
                self.received.set_result(data)

        transport, protocol = await loop.create_datagram_endpoint(_Client, remote_addr=("127.0.0.1", server.port))
        transport.sendto(query("udp.test", QTYPE_A))
        udp_response = await asyncio.wait_for(protocol.received, 2)
        transport.close()
        assert parse_answers(udp_response)[1] == 1

        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        message = query("tcp.test", QTYPE_A)
        writer.write(len(message).to_bytes(2, "big") + message)
        await writer.drain()
        length = int.from_bytes(await reader.readexactly(2), "big")
        tcp_response = await reader.readexactly(length)
        writer.close()
        _, ancount, answers = parse_answers(tcp_response)
        assert ancount == 1
        assert pool.name_for(socket.inet_ntoa(answers[0][1])) == "tcp.test"
    finally:
        await server.stop()


class _Store:
    """A stand-in for the Redis-backed mapping: what another instance already decided."""

    def __init__(self, fail: bool = False) -> None:
        self.names: dict[str, int] = {}
        self.fail = fail
        self.next = 0

    async def fakeip_offset_for(self, name: str) -> int | None:
        if self.fail:
            raise ConnectionError("redis down")
        return self.names.get(name)

    async def fakeip_name_for(self, offset: int) -> str | None:
        if self.fail:
            raise ConnectionError("redis down")
        return next((n for n, o in self.names.items() if o == offset), None)

    async def fakeip_allocate(self, name: str, capacity: int) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        while self.next in self.names.values():
            self.next += 1
        self.names[name] = self.next
        return self.next


class TestDirectory:
    async def test_adopts_the_cluster_mapping(self) -> None:
        store = _Store()
        store.names["shared.test"] = 7  # allocated by another instance
        directory = FakeIpDirectory(FakeIpPool("198.18.0.0/15"), store)  # type: ignore[arg-type]
        assert str(await directory.ip_for("shared.test")) == "198.18.0.8"
        # A connection for an address this instance never handed out resolves through the store.
        store.names["elsewhere.test"] = 9
        assert await directory.name_for("198.18.0.10") == "elsewhere.test"
        assert await directory.name_for("198.18.0.200") is None
        # New names are allocated cluster-wide, then served from the local pool.
        fresh = await directory.ip_for("new.test")
        assert store.names["new.test"] == 0 and str(fresh) == "198.18.0.1"
        assert directory.pool.name_for(str(fresh)) == "new.test"

    async def test_falls_back_to_the_local_pool(self) -> None:
        directory = FakeIpDirectory(FakeIpPool("198.18.0.0/15"), _Store(fail=True))  # type: ignore[arg-type]
        ip = await directory.ip_for("local.test")
        assert await directory.name_for(str(ip)) == "local.test"
        assert directory.store_errors >= 1
