# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the clients that check a proxy's exit: one TLS context, however many clients."""

import ssl
from typing import Any

import pytest

from api.providers.sdk.sources import (
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
