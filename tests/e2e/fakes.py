# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The world outside Octoprox, served by the test: a vendor's forwarding HTTP proxy and the site behind it.

Both are asyncio servers on free loopback ports of the test's event loop, speaking just enough HTTP/1.1 for one
exchange per connection, and stopping one cancels the connections it still serves before it waits for them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

# Where both servers listen, and the host of every URL the cluster is given.
LOOPBACK = "127.0.0.1"
# The path the target answers with the caller's exit: the cluster's echo
# endpoint (``geo_lookup_url``), which health checks request through each proxy.
ECHO_PATH = "/ip"
# The exit the target reports at ``ECHO_PATH``, from TEST-NET-3 (RFC 5737):
# no IP database places it, so attribution leaves the proxy's place open.
TARGET_EXIT_IP = "203.0.113.10"
# The target answers ``BYTES_PATH`` followed by a number with that many bytes:
# a download of a known size, as a traffic limit counts it.
BYTES_PATH = "/bytes/"
# Bytes copied per read when tunnelling: one socket buffer.
_CHUNK_BYTES = 65536
# Request headers addressed to the proxy itself, never passed on to the site.
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authorization", "proxy-connection"})


@dataclass(frozen=True)
class Head:
    """The request line and the headers of an HTTP/1.1 request, as received."""

    method: str
    # The request target: a path, an absolute URL, or ``host:port`` of a CONNECT.
    target: str
    headers: list[tuple[str, str]]

    @classmethod
    async def read(cls, reader: asyncio.StreamReader) -> Head | None:
        """The request line and headers of the next request; None when the peer closed before sending one."""
        line = await reader.readline()
        if not line.strip():
            return None
        headers: list[tuple[str, str]] = []
        while (raw := await reader.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = raw.decode("latin-1").partition(":")
            headers.append((name.strip(), value.strip()))
        method, target, _version = line.decode("latin-1").strip().split(" ", 2)
        return cls(method, target, headers)

    def values(self, name: str) -> list[str]:
        """Every value of the header ``name`` (lower case), in the order they came."""
        return [value for key, value in self.headers if key.lower() == name]

    @property
    def content_length(self) -> int:
        """The body's length in bytes; 0 when the message declares none."""
        lengths = self.values("content-length")
        return int(lengths[0]) if lengths else 0


@dataclass(frozen=True)
class Carried:
    """One request the vendor endpoint was asked to carry, as it arrived."""

    method: str
    # The absolute URL of a forwarded request, or ``host:port`` of a CONNECT.
    target: str
    # Every Proxy-Authorization value the request bore, in order.
    authorization: list[str]


def response(
    status: int, reason: str, body: bytes = b"", *, headers: Sequence[tuple[str, str]] = ()
) -> bytes:
    """A complete response that frames its body by length and closes the connection after it."""
    lines = [
        f"HTTP/1.1 {status} {reason}",
        *(f"{name}: {value}" for name, value in headers),
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("latin-1") + body


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction of a tunnel until its end, then pass the half-close on."""
    try:
        while data := await reader.read(_CHUNK_BYTES):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        # One leg went away: closing the other tells its peer.
        writer.close()


async def _close(writer: asyncio.StreamWriter) -> None:
    """Close a connection, ignoring a peer that already went."""
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


class _Server(ABC):
    """An asyncio server on a free port of the loopback address, one task per connection."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """The port the server listens on."""
        assert self._server is not None, "the server has not started"
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    @property
    def authority(self) -> str:
        """``host:port`` of the server, as a CONNECT names it."""
        return f"{LOOPBACK}:{self.port}"

    async def start(self) -> None:
        """Listen on a free port."""
        self._server = await asyncio.start_server(self._accept, LOOPBACK, 0)

    async def stop(self) -> None:
        """Stop listening, cancel the connections still served, and wait for them."""
        if self._server is None:
            return
        self._server.close()
        for connection in self._connections:
            connection.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        await self._server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one accepted connection's request, among the connections :meth:`stop` cancels, and close it after."""
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        try:
            head = await Head.read(reader)
            if head is not None:
                await self._handle(head, reader, writer)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            # The peer went away mid-exchange: there is no one to answer.
            pass
        finally:
            self._connections.discard(task)
            await _close(writer)

    @abstractmethod
    async def _handle(self, head: Head, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer the connection's one request, whose body is still to read, or tunnel it."""


class FakeTarget(_Server):
    """The site clients reach through Octoprox: JSON naming each request, the caller's exit at ``ECHO_PATH``, bytes at ``BYTES_PATH``."""

    def __init__(self) -> None:
        super().__init__()
        # The path of every request served, in arrival order.
        self.paths: list[str] = []

    def url(self, path: str) -> str:
        """The absolute URL of ``path`` on this site."""
        return f"http://{self.authority}{path}"

    async def _handle(self, head: Head, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if head.content_length:
            await reader.readexactly(head.content_length)
        path = head.target
        self.paths.append(path)
        if path.startswith(BYTES_PATH):
            body = bytes(int(path.removeprefix(BYTES_PATH)))
            writer.write(response(200, "OK", body, headers=[("Content-Type", "application/octet-stream")]))
        else:
            answer = {"origin": TARGET_EXIT_IP} if path == ECHO_PATH else {"method": head.method, "path": path}
            writer.write(
                response(200, "OK", json.dumps(answer).encode(), headers=[("Content-Type", "application/json")])
            )
        await writer.drain()


class FakeUpstream(_Server):
    """A vendor's HTTP proxy endpoint: checks its credentials, then forwards or tunnels; it can be made to fail or to hang.

    Args:
        username, password: The vendor credentials it accepts in ``Proxy-Authorization``.
    """

    def __init__(self, username: str, password: str) -> None:
        super().__init__()
        self.username = username
        self.password = password
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        # The one Proxy-Authorization value a request may carry.
        self.authorization = f"Basic {token}"
        # While set, every request is answered 503, as a dead vendor endpoint answers.
        self.failing = False
        # Every request it was asked to carry, refused ones included, in arrival order.
        self.carried: list[Carried] = []
        # Requests waiting unanswered now (see hold_next).
        self.held = 0
        # How many of the next requests to hold, and what releases them.
        self._to_hold = 0
        self._released = asyncio.Event()

    def hold_next(self, count: int) -> None:
        """Leave the next ``count`` requests unanswered until :meth:`release`, as a vendor that hangs does."""
        if self._released.is_set():
            self._released = asyncio.Event()
        self._to_hold += count

    def release(self) -> None:
        """Answer every request held, and hold no more."""
        self._to_hold = 0
        self._released.set()

    async def _handle(self, head: Head, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authorization = head.values("proxy-authorization")
        self.carried.append(Carried(head.method, head.target, authorization))
        if self._to_hold:
            self._to_hold -= 1
            self.held += 1
            try:
                await self._released.wait()
            finally:
                self.held -= 1
        if self.failing:
            writer.write(response(503, "Service Unavailable"))
        elif authorization != [self.authorization]:
            writer.write(
                response(
                    407, "Proxy Authentication Required", headers=[("Proxy-Authenticate", "Basic")]
                )
            )
        elif head.method == "CONNECT":
            await self._tunnel(reader, writer, head.target)
            return
        else:
            await self._forward(reader, writer, head)
        await writer.drain()

    async def _forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, head: Head) -> None:
        """Send an absolute-form request on to its site in origin form and relay the whole answer."""
        url = urlsplit(head.target)
        assert url.hostname is not None, f"not an absolute-form target: {head.target}"
        body = await reader.readexactly(head.content_length) if head.content_length else b""
        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"
        kept = [(name, value) for name, value in head.headers if name.lower() not in _HOP_BY_HOP]
        lines = [
            f"{head.method} {path} HTTP/1.1",
            *(f"{name}: {value}" for name, value in kept),
            "Connection: close",
            "",
            "",
        ]
        site_reader, site_writer = await asyncio.open_connection(url.hostname, url.port or 80)
        try:
            site_writer.write("\r\n".join(lines).encode("latin-1") + body)
            await site_writer.drain()
            writer.write(await site_reader.read())
        finally:
            await _close(site_writer)

    async def _tunnel(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        """Connect to ``host:port``, say so, and copy both directions until both have ended."""
        host, _, port = target.rpartition(":")
        site_reader, site_writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pump(reader, site_writer), _pump(site_reader, writer))
        finally:
            await _close(site_writer)
