# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the clients that check a proxy's exit: one TLS context, however many clients, and the vendor's own answer
when it fails the echo."""

import ssl
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from api.core.http_tunnel import TunnelRefusedError
from api.providers.sdk.descriptor import IpDiscoverySpec
from api.providers.sdk.sources import (
    IpDiscoverer,
    _proxy_ssl_context,
    default_proxied_client_factory,
    echo_client_factory,
    httpx_proxy,
    shared_ssl_context,
)
from tests.core.test_http_tunnel import _vendor


@pytest.mark.parametrize("factory", [default_proxied_client_factory, echo_client_factory])
@pytest.mark.parametrize(
    "proxy_url",
    ["http://u:p@127.0.0.1:8080", "https://u:p@127.0.0.1:8443", "socks5://u:p@127.0.0.1:1080"],
)
def test_a_client_per_check_builds_no_tls_context(
    proxy_url: str, factory: Callable[[str, float], httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_ssl_context()
    _proxy_ssl_context()
    built: list[Any] = []
    real = ssl.create_default_context

    def counting(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    for _ in range(3):
        factory(proxy_url, 5.0)
    assert built == []


def test_only_an_https_proxy_carries_the_shared_proxy_context() -> None:
    context = httpx_proxy("https://u:p@proxy.example:8443").ssl_context
    assert context is _proxy_ssl_context()
    assert httpx_proxy("https://u:p@other.example:8443").ssl_context is context
    # httpcore's default for a proxy, system CAs included, not certifi alone.
    assert context is not shared_ssl_context()
    # httpcore refuses a context for an http:// proxy: there is no TLS to it.
    assert httpx_proxy("http://u:p@proxy.example:8080").ssl_context is None


def _refusing_discoverer() -> IpDiscoverer:
    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400)), timeout=timeout)

    return IpDiscoverer(IpDiscoverySpec(), client_factory=factory)


@pytest.mark.parametrize(
    ("log_context", "level"),
    [
        # One echo per request: its failure refuses the request wherever the rules could have, which the Failures page counts.
        ({"proxy_id": "p1", "preflight": True}, "debug"),
        # The health checker and attribution check a proxy now and then: still worth a warning.
        ({"proxy_id": "p1"}, "warning"),
    ],
)
async def test_a_failed_preflight_echo_is_logged_at_debug(log_context: dict[str, Any], level: str) -> None:
    with patch("api.providers.sdk.sources.logger") as logger:
        ip, _place = await _refusing_discoverer().discover_with_place("http://u:p@proxy:8080", log_context=log_context)

    assert ip is None
    getattr(logger, level).assert_called_once_with("IP discovery returned an error", status_code=400, **log_context)
    assert not getattr(logger, "warning" if level == "debug" else "debug").called


@pytest.mark.parametrize(
    "error",
    [ProxyError("General SOCKS server failure"), ProxyConnectionError(None, "Couldn't connect to proxy"), ProxyTimeoutError("Proxy connection timed out")],
    ids=["refused", "unreachable", "timeout"],
)
async def test_a_failed_socks_echo_is_a_failure_not_an_exception(error: Exception) -> None:
    """python-socks raises its own errors, which httpx does not wrap; they fail the echo like any other."""

    def raising(request: httpx.Request) -> httpx.Response:
        raise error

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(raising), timeout=timeout)

    ip, place = await IpDiscoverer(IpDiscoverySpec(), client_factory=factory).discover_with_place(
        "socks5://u:p@proxy:1080", log_context={"preflight": True}
    )
    assert (ip, place) == (None, None)


UPSTREAM_FAILURES = [
    TunnelRefusedError(400, "Bad Request", "no upstream available"),
    ProxyError("General SOCKS server failure"),
    ProxyConnectionError(None, "Couldn't connect to proxy"),
    ProxyTimeoutError("Proxy connection timed out"),
    TimeoutError(),
]


def _failing_discoverer(error: Exception) -> IpDiscoverer:
    def raising(request: httpx.Request) -> httpx.Response:
        raise error

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(raising), timeout=timeout)

    return IpDiscoverer(IpDiscoverySpec(), client_factory=factory)


class TestRaiseUpstream:
    """Preflight asks for the upstream's failure of the echo: its request through the upstream would fail the same way."""

    @pytest.mark.parametrize("error", UPSTREAM_FAILURES, ids=["vendor-400", "socks", "unreachable", "socks-timeout", "timeout"])
    async def test_the_upstream_failing_the_echo_is_raised(self, error: Exception) -> None:
        with pytest.raises(type(error)) as raised:
            await _failing_discoverer(error).discover_with_place("http://u:p@proxy:8080", raise_upstream=True)
        assert raised.value is error

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("echo endpoint down"),
            httpx.ReadTimeout("echo endpoint slow"),
            httpx.RemoteProtocolError("bad"),
            # python-socks runs the echo endpoint's TLS handshake on a SOCKS upstream and lets its failure through.
            ssl.SSLCertVerificationError("certificate verify failed"),
        ],
        ids=["connect", "read-timeout", "protocol", "socks-echo-tls"],
    )
    async def test_an_echo_failing_past_the_upstream_is_no_ip(self, error: Exception) -> None:
        ip, place = await _failing_discoverer(error).discover_with_place("http://u:p@proxy:8080", raise_upstream=True)
        assert (ip, place) == (None, None)

    async def test_an_error_status_from_the_echo_is_no_ip(self) -> None:
        ip, _place = await _refusing_discoverer().discover_with_place("http://u:p@proxy:8080", raise_upstream=True)
        assert ip is None


class TestEchoClient:
    async def test_an_https_echo_through_an_http_proxy_keeps_what_the_vendor_said(self) -> None:
        heard: list[bytes] = []
        answer = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 21\r\n\r\nno upstream available"
        async with _vendor(answer, heard) as port:
            discoverer = IpDiscoverer(IpDiscoverySpec(url="https://echo.example/ip"))
            with pytest.raises(TunnelRefusedError) as refused:
                await discoverer.discover_with_place(f"http://u:p@127.0.0.1:{port}", raise_upstream=True)

        assert (str(refused.value), refused.value.vendor_message) == ("400 Bad Request", "no upstream available")
        assert heard[0].startswith(b"CONNECT echo.example:443 HTTP/1.1\r\n")

    async def test_an_http_echo_is_forwarded_as_before(self) -> None:
        """A plain http:// echo is sent to the vendor to forward, not tunnelled; its refusal is an answer like any other."""
        heard: list[bytes] = []
        async with _vendor(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n", heard) as port:
            discoverer = IpDiscoverer(IpDiscoverySpec(url="http://echo.example/ip"))
            ip, _place = await discoverer.discover_with_place(f"http://u:p@127.0.0.1:{port}", raise_upstream=True)

        assert ip is None
        assert heard[0].startswith(b"GET http://echo.example/ip HTTP/1.1\r\n")
