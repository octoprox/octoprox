# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the clients that check a proxy's exit: one TLS context, however many clients."""

import ssl
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from api.providers.sdk.descriptor import IpDiscoverySpec
from api.providers.sdk.sources import (
    IpDiscoverer,
    _proxy_ssl_context,
    default_proxied_client_factory,
    httpx_proxy,
    shared_ssl_context,
)


@pytest.mark.parametrize(
    "proxy_url",
    ["http://u:p@127.0.0.1:8080", "https://u:p@127.0.0.1:8443", "socks5://u:p@127.0.0.1:1080"],
)
def test_a_client_per_check_builds_no_tls_context(proxy_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    shared_ssl_context()
    _proxy_ssl_context()
    built: list[Any] = []
    real = ssl.create_default_context

    def counting(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    for _ in range(3):
        default_proxied_client_factory(proxy_url, 5.0)
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
        # One echo per request: its failure fails the request, which the Failures page counts.
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
