# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests that every client that takes a proxy's URL sends the vendor its credentials exactly as stored.

python-socks (SOCKS upstreams), curl_cffi and rnet (TLS interception) and
httpx (health checks, preflight, discovery) each parse ``Proxy.url``; a URL
parser decodes ``%xx`` in credentials and ends them at ``/``, ``?`` or ``#``.
"""

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from curl_cffi.requests import AsyncSession
from python_socks.async_.asyncio import Proxy as SocksProxy
from rnet import Client

from api.core.http_tunnel import open_http_tunnel
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.sdk.sources import default_proxied_client_factory

# Characters a URL parser treats specially, and an already escaped-looking one.
PASSWORDS = ["plain", "p@ss", "p%40ss", "pa/ss", "pa?ss", "pa#ss", "pa:ss", "pa ss+"]


@contextlib.asynccontextmanager
async def _vendor(heard: list[str]) -> AsyncIterator[int]:
    """An HTTP proxy that records each request's Basic credentials and refuses it."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        for line in head.split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "proxy-authorization":
                heard.append(base64.b64decode(value.split()[-1]).decode())
        writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def _python_socks(url: str) -> None:
    await SocksProxy.from_url(url).connect("example.com", 443)


async def _curl_cffi(url: str) -> None:
    async with AsyncSession() as session:
        await session.get("https://example.com/", proxy=url, timeout=5)


async def _rnet(url: str) -> None:
    await Client().get("https://example.com/", proxy=url)


async def _httpx(url: str) -> None:
    async with default_proxied_client_factory(url, 5) as client:
        await client.get("https://example.com/")


CLIENTS: dict[str, Callable[[str], Awaitable[None]]] = {
    "python-socks": _python_socks,
    "curl_cffi": _curl_cffi,
    "rnet": _rnet,
    "httpx": _httpx,
}


@pytest.mark.parametrize("password", PASSWORDS)
@pytest.mark.parametrize("client", list(CLIENTS))
async def test_a_client_of_the_proxy_url_sends_the_stored_credentials(client: str, password: str) -> None:
    heard: list[str] = []
    async with _vendor(heard) as port:
        proxy = Proxy(
            host="127.0.0.1", port=port, protocol=ProxyProtocol.HTTP, username="user-zone+a", password=password,
            connector_id="c",
        )
        with contextlib.suppress(Exception):  # the vendor refuses; only what it was sent matters
            await CLIENTS[client](proxy.url)

    assert heard == [f"user-zone+a:{password}"]


@pytest.mark.parametrize("password", PASSWORDS)
async def test_octoprox_s_own_connect_sends_the_stored_credentials(password: str) -> None:
    heard: list[str] = []
    async with _vendor(heard) as port:
        with contextlib.suppress(Exception):
            await open_http_tunnel("127.0.0.1", port, "user-zone+a", password, "example.com", 443)

    assert heard == [f"user-zone+a:{password}"]
