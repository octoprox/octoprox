# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Reading the destination name off the first bytes without consuming them."""

import asyncio
import struct

import pytest

from api.tunnel.sniff import (
    clean_hostname,
    host_from_http_head,
    peek,
    sni_from_client_hello,
    sniff,
)


def client_hello(server_name: str | None, *, split_at: int | None = None) -> bytes:
    """A minimal TLS 1.2 ClientHello record, optionally with an SNI extension."""
    extensions = b""
    if server_name is not None:
        name = server_name.encode()
        entry = b"\x00" + struct.pack("!H", len(name)) + name
        sni_list = struct.pack("!H", len(entry)) + entry
        extensions += struct.pack("!HH", 0x0000, len(sni_list)) + sni_list
    # supported_versions: TLS 1.3 and 1.2, so parsers see a modern hello.
    sv = b"\x04\x03\x04\x03\x03"
    extensions += struct.pack("!HH", 0x002B, len(sv)) + sv
    body = (
        b"\x03\x03" + bytes(32) + b"\x00"  # version, random, empty session id
        + b"\x00\x02\x13\x01"  # one cipher suite
        + b"\x01\x00"  # null compression
        + struct.pack("!H", len(extensions)) + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake


def _reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_peek_does_not_consume() -> None:
    reader = _reader(b"hello world")
    assert await peek(reader, 5, 0.5) == b"hello"
    assert await peek(reader, 100, 0.5) == b"hello world"
    assert await reader.read(100) == b"hello world"


@pytest.mark.asyncio
async def test_peek_waits_for_late_bytes_then_gives_up() -> None:
    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, reader.feed_data, b"abcdef")
    assert await peek(reader, 4, 1.0) == b"abcd"
    # Nothing more is coming: the timeout returns what is there.
    started = loop.time()
    assert await peek(reader, 50, 0.1) == b"abcdef"
    assert loop.time() - started < 0.5


@pytest.mark.asyncio
async def test_tls_sni() -> None:
    hello = client_hello("Shop.Example.COM")
    reader = _reader(hello)
    result = await sniff(reader, 0.5)
    assert result.tls is True
    assert result.server_name == "shop.example.com"
    # The handshake is still there for whoever runs TLS next.
    assert await reader.read(len(hello)) == hello


@pytest.mark.asyncio
async def test_tls_without_sni() -> None:
    result = await sniff(_reader(client_hello(None)), 0.5)
    assert result.tls is True and result.server_name is None


@pytest.mark.asyncio
async def test_tls_record_arriving_in_pieces() -> None:
    hello = client_hello("split.example")
    reader = asyncio.StreamReader()
    reader.feed_data(hello[:7])
    asyncio.get_running_loop().call_later(0.05, reader.feed_data, hello[7:])
    result = await sniff(reader, 1.0)
    assert result.server_name == "split.example"


@pytest.mark.asyncio
async def test_http_host_header() -> None:
    head = b"GET /index.html HTTP/1.1\r\nUser-Agent: tv\r\nHost: Media.Example.org:8080\r\n\r\n"
    result = await sniff(_reader(head), 0.5)
    assert result.tls is False
    assert result.server_name == "media.example.org"


@pytest.mark.asyncio
async def test_server_speaks_first_times_out_quietly() -> None:
    reader = asyncio.StreamReader()  # the client sends nothing (SSH, SMTP)
    result = await sniff(reader, 0.1)
    assert result.tls is False and result.server_name is None


@pytest.mark.asyncio
async def test_binary_protocol() -> None:
    result = await sniff(_reader(b"\x00\x01\x02\x03\x04\x05"), 0.5)
    assert result.tls is False and result.server_name is None


def test_sni_from_garbage_is_none() -> None:
    assert sni_from_client_hello(b"\x01\x00\x00\x02\xff\xff") is None


def test_http_head_parsing_rules() -> None:
    assert host_from_http_head(b"POST / HTTP/1.0\r\nhost: a.b\r\n\r\n") == "a.b"
    assert host_from_http_head(b"GET / HTTP/1.1\r\nHost: [2001:db8::1]:443\r\n\r\n") is None  # a literal, not a name
    assert host_from_http_head(b"NOTHTTP junk\r\n\r\n") is None
    assert host_from_http_head(b"GET / HTTP/1.1\r\n\r\n") is None


def test_clean_hostname() -> None:
    assert clean_hostname(" Example.com. ") == "example.com"
    assert clean_hostname("10.0.0.1") is None
    assert clean_hostname("bad host") is None
    assert clean_hostname("") is None and clean_hostname(None) is None
