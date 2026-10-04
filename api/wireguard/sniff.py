# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Recover the destination name from the first bytes a device sends.

A transparent connection arrives with an address, not a name. The fake-IP
pool answers for anything the device resolved, but a device may also connect
to a literal address it had from elsewhere. For those, a TLS ClientHello
carries the name in its SNI and a plain HTTP request in its Host header. The
bytes are peeked, not consumed: the MITM handler, when it takes the
connection over, needs the ClientHello still in the stream to run the
handshake.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

_TLS_HANDSHAKE = 0x16
_TLS_CLIENT_HELLO = 0x01
_MAX_RECORD = 16384 + 5
_MAX_HTTP_HEAD = 8192
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "HEAD", "DELETE", "OPTIONS", "PATCH", "CONNECT", "TRACE"})
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*\.?$")


@dataclass(frozen=True)
class SniffResult:
    """What the opening bytes said: whether they begin a TLS handshake, and the name if any."""

    tls: bool
    server_name: str | None


async def peek(reader: asyncio.StreamReader, size: int, timeout: float) -> bytes:
    """Up to ``size`` bytes from the front of the stream without consuming them.

    Waits until that many are buffered, the peer closes, or ``timeout``
    passes, and returns what is there. asyncio.StreamReader has no public
    peek; this reads its buffer and waits on the same future ``read`` does,
    which is stable across every CPython this project supports.
    """
    buffer: bytearray = reader._buffer  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # at_eof() is false while bytes remain buffered; the flag says whether more can come.
    while len(buffer) < size and not reader._eof:  # type: ignore[attr-defined]
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(reader._wait_for_data("peek"), remaining)  # type: ignore[attr-defined]
        except TimeoutError:
            break
    return bytes(buffer[:size])


def take_buffered(reader: asyncio.StreamReader) -> bytes:
    """Remove and return what ``reader`` holds: the peeked bytes, for a caller that replays them itself.

    The MITM handler upgrades the connection with ``loop.start_tls``, which
    only sees bytes still in the socket; a ClientHello that ``peek`` pulled
    into the reader has to be taken out and fed to the handshake by hand.
    """
    buffer: bytearray = reader._buffer  # type: ignore[attr-defined]
    data = bytes(buffer)
    del buffer[:]
    return data


async def _peek_until(reader: asyncio.StreamReader, marker: bytes, limit: int, timeout: float) -> bytes:
    size = 512
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        data = await peek(reader, size, max(0.0, deadline - loop.time()))
        if marker in data or len(data) < size or size >= limit:
            return data
        size = min(size * 2, limit)


async def sniff(reader: asyncio.StreamReader, timeout: float) -> SniffResult:
    """Peek at the opening bytes and read a destination name out of them if there is one.

    Server-speaks-first protocols (SSH, SMTP) send nothing, so the peek times
    out and the connection proceeds by address; the timeout is the price
    those pay once per connection.
    """
    head = await peek(reader, 5, timeout)
    if len(head) >= 5 and head[0] == _TLS_HANDSHAKE and head[1] == 0x03 and head[2] <= 0x04:
        length = int.from_bytes(head[3:5], "big")
        if length == 0 or 5 + length > _MAX_RECORD:
            return SniffResult(tls=True, server_name=None)
        record = await peek(reader, 5 + length, timeout)
        body = record[5:]
        if len(body) != length or body[:1] != bytes([_TLS_CLIENT_HELLO]):
            return SniffResult(tls=True, server_name=None)
        return SniffResult(tls=True, server_name=sni_from_client_hello(body))
    if head[:1].isalpha():
        data = await _peek_until(reader, b"\r\n\r\n", _MAX_HTTP_HEAD, timeout)
        return SniffResult(tls=False, server_name=host_from_http_head(data))
    return SniffResult(tls=False, server_name=None)


def sni_from_client_hello(handshake: bytes) -> str | None:
    """The server_name of a ClientHello handshake message (type byte onward), or None."""
    from api.core.mitm.client_hello import parse_client_hello

    try:
        info = parse_client_hello(handshake)
    except Exception:
        logger.debug("Unparseable ClientHello while sniffing", exc_info=True)
        return None
    return clean_hostname(info.sni)


def host_from_http_head(data: bytes) -> str | None:
    """The Host header of an HTTP/1.x request head, without its port, or None."""
    text = data.decode("latin-1", "replace")
    lines = text.split("\r\n")
    request_line = lines[0].split(" ")
    if len(request_line) < 3 or request_line[0].upper() not in _HTTP_METHODS or not request_line[2].startswith("HTTP/"):
        return None
    for line in lines[1:]:
        if line == "":
            break
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() == "host":
            return clean_hostname(_strip_port(value.strip()))
    return None


def _strip_port(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def clean_hostname(value: str | None) -> str | None:
    """A lower-cased DNS name, or None for an empty, malformed or literal-address value."""
    if not value:
        return None
    name = value.strip().lower().rstrip(".")
    if not name or len(name) > 253:
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return None
    return name if _HOSTNAME.match(name) else None
