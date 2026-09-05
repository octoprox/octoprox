# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the declarative HTTP executor."""

import httpx
import pytest

from api.providers.sdk.descriptor import (
    AuthFlowSpec,
    HttpCallSpec,
    PaginationSpec,
    ProviderDescriptor,
    ProxyTypeSpec,
)
from api.providers.sdk.egress import EgressGuard, EgressPolicy
from api.providers.sdk.http import (
    AuthTokenCache,
    HttpCallError,
    HttpCallExecutor,
    ResponseTooLargeError,
)
from api.providers.sdk.templating import RenderContext
from tests.providers.sdk.conftest import TEST_POLICY, json_response


def _descriptor(**overrides: object) -> ProviderDescriptor:
    base: dict[str, object] = {
        "id": "vendor",
        "name": "Vendor",
        "proxy_types": [ProxyTypeSpec(key="res", label="Res", mode="session", host="gw.vendor.test", port=1, username="u")],
    }
    base.update(overrides)
    return ProviderDescriptor.model_validate(base)


def _executor(descriptor: ProviderDescriptor, handler, *, policy: EgressPolicy = TEST_POLICY, max_bytes: int = 0) -> HttpCallExecutor:  # type: ignore[no-untyped-def]
    return HttpCallExecutor(
        descriptor,
        egress=EgressGuard(policy),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_response_bytes=max_bytes,
        token_cache=AuthTokenCache(),
    )


async def test_renders_templates_and_drops_empty_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response({"ok": True})

    call = HttpCallSpec(
        url="https://api.vendor.test/zone",
        headers={"Authorization": "Bearer {credential.token}"},
        params={"zone": "{connector.zone}", "country": "{connector.country|lower}"},
    )
    ctx = RenderContext(credential={"token": "T0K"}, connector={"zone": "z1"}, secret_keys=frozenset({"token"}))
    result = await _executor(_descriptor(), handler).execute(call, ctx)

    assert result.ok and result.data == {"ok": True}
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer T0K"
    assert dict(request.url.params) == {"zone": "z1"}  # empty country dropped
    trace = result.traces[0]
    assert trace.headers["Authorization"] == "***"
    assert "T0K" not in trace.url


async def test_non_2xx_raises_unless_asked_not_to() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"error": "nope"}, 401)

    call = HttpCallSpec(url="https://api.vendor.test/status")
    executor = _executor(_descriptor(), handler)
    with pytest.raises(HttpCallError) as excinfo:
        await executor.execute(call, RenderContext())
    assert excinfo.value.trace is not None and excinfo.value.trace.status == 401

    result = await executor.execute(call, RenderContext(), raise_for_status=False)
    assert not result.ok and result.status == 401


async def test_auth_flow_token_is_cached() -> None:
    logins = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/login":
            logins += 1
            assert request.headers["Authorization"].startswith("Basic ")
            return json_response({"token": "jwt-123"})
        assert request.headers["Authorization"] == "Bearer jwt-123"
        return json_response([{"username": "sub1"}])

    descriptor = _descriptor(
        auth={
            "login": AuthFlowSpec(
                call=HttpCallSpec(method="POST", url="https://api.vendor.test/login", headers={"Authorization": "Basic {credential.basic}"}),
                token_path="token",
            )
        }
    )
    call = HttpCallSpec(url="https://api.vendor.test/users", headers={"Authorization": "Bearer {auth.token}"}, auth="login")
    executor = _executor(descriptor, handler)
    ctx = RenderContext(credential={"basic": "abc"})
    first = await executor.execute(call, ctx)
    second = await executor.execute(call, ctx)
    assert first.data == second.data == [{"username": "sub1"}]
    assert logins == 1


async def test_pagination_follows_same_host_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page == "1":
            return json_response({"results": [1, 2], "next": "https://api.vendor.test/list?page=2"})
        if page == "2":
            return json_response({"results": [3], "next": None})
        raise AssertionError("unexpected page")

    call = HttpCallSpec(url="https://api.vendor.test/list", paginate=PaginationSpec(next_url="next"))
    result = await _executor(_descriptor(), handler).execute(call, RenderContext())
    assert len(result.pages) == 2
    from api.providers.sdk.extract import ValueExtractor

    assert result.items(ValueExtractor(), "results") == [1, 2, 3]

    def evil(request: httpx.Request) -> httpx.Response:
        return json_response({"results": [], "next": "https://evil.test/steal"})

    with pytest.raises(HttpCallError, match="different host"):
        await _executor(_descriptor(), evil).execute(call, RenderContext())


async def test_redirects_are_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    with pytest.raises(HttpCallError, match="HTTP 302"):
        await _executor(_descriptor(), handler).execute(HttpCallSpec(url="https://api.vendor.test/"), RenderContext())


async def test_egress_policy_blocks_private_hosts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    executor = _executor(_descriptor(), handler, policy=EgressPolicy())
    with pytest.raises(HttpCallError, match="egress denied"):
        await executor.execute(HttpCallSpec(url="https://169.254.169.254/latest"), RenderContext())


async def test_response_size_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048, headers={"content-type": "text/plain"})

    executor = _executor(_descriptor(), handler, max_bytes=1024)
    with pytest.raises(ResponseTooLargeError):
        await executor.execute(HttpCallSpec(url="https://api.vendor.test/big"), RenderContext())
    unlimited = _executor(_descriptor(), handler, max_bytes=0)
    result = await unlimited.execute(HttpCallSpec(url="https://api.vendor.test/big"), RenderContext())
    assert result.data == "x" * 2048
