# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the declarative HTTP executor."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

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


class _Sleeps:
    """Records the waits the executor asks for instead of taking them."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _executor(descriptor: ProviderDescriptor, handler, *, policy: EgressPolicy = TEST_POLICY, max_bytes: int = 0, sleep: _Sleeps | None = None) -> HttpCallExecutor:  # type: ignore[no-untyped-def]
    return HttpCallExecutor(
        descriptor,
        egress=EgressGuard(policy),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_response_bytes=max_bytes,
        token_cache=AuthTokenCache(),
        sleep=sleep or _Sleeps(),
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


async def test_a_failed_calls_error_ends_with_the_start_of_the_vendors_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list":
            return httpx.Response(404, content=b'{"detail": "Invalid page."}', headers={"content-type": "application/json"})
        return httpx.Response(500, text="<html>\n  <body>Bad token T0K\n" + "x" * 500 + "</body></html>")

    executor = _executor(_descriptor(), handler)
    ctx = RenderContext(credential={"token": "T0K"}, secret_keys=frozenset({"token"}))
    with pytest.raises(HttpCallError) as short:
        await executor.execute(HttpCallSpec(url="https://api.vendor.test/list"), ctx)
    assert str(short.value) == 'GET https://api.vendor.test/list returned HTTP 404: {"detail": "Invalid page."}'
    assert short.value.trace is not None and short.value.trace.error == '{"detail": "Invalid page."}'

    # A long reply is cut, its whitespace collapsed, and a secret in it redacted before the cut.
    with pytest.raises(HttpCallError) as long:
        await executor.execute(HttpCallSpec(url="https://api.vendor.test/boom"), ctx)
    excerpt = long.value.trace.error if long.value.trace else None
    assert excerpt is not None and excerpt.startswith("<html> <body>Bad token *** xxx") and excerpt.endswith("...")
    assert len(excerpt) == 200 and "T0K" not in str(long.value)


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


async def test_on_page_stops_pagination() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        requested.append(page)
        return json_response({"results": [int(page)], "next": f"https://api.vendor.test/list?page={int(page) + 1}"})

    call = HttpCallSpec(url="https://api.vendor.test/list", paginate=PaginationSpec(next_url="next"))
    seen: list[object] = []

    def on_page(page: object) -> bool:
        seen.append(page)
        return len(seen) < 2

    result = await _executor(_descriptor(), handler).execute(call, RenderContext(), on_page=on_page)
    assert requested == ["1", "2"]
    assert len(result.pages) == len(seen) == 2


async def test_rate_limited_page_is_sent_again_after_retry_after() -> None:
    answers = iter([
        json_response({"results": [1], "next": "https://api.vendor.test/list?page=2"}),
        httpx.Response(429, headers={"retry-after": "2"}),
        json_response({"results": [2], "next": None}),
    ])
    sleeps = _Sleeps()
    call = HttpCallSpec(url="https://api.vendor.test/list", paginate=PaginationSpec(next_url="next"))
    result = await _executor(_descriptor(), lambda request: next(answers), sleep=sleeps).execute(call, RenderContext())
    assert sleeps.waits == [2.0]
    assert result.ok and len(result.pages) == 2
    assert [(trace.page, trace.status) for trace in result.traces] == [(1, 200), (2, 429), (2, 200)]


async def test_rate_limit_takes_an_http_date_retry_after() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    answers = iter([httpx.Response(429, headers={"retry-after": when}), json_response({"ok": True})])
    sleeps = _Sleeps()
    result = await _executor(_descriptor(), lambda request: next(answers), sleep=sleeps).execute(
        HttpCallSpec(url="https://api.vendor.test/status"), RenderContext()
    )
    assert result.data == {"ok": True}
    assert len(sleeps.waits) == 1 and 25 <= sleeps.waits[0] <= 30


async def test_rate_limit_without_retry_after_backs_off_then_fails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429)

    sleeps = _Sleeps()
    with pytest.raises(HttpCallError, match="HTTP 429") as excinfo:
        await _executor(_descriptor(), handler, sleep=sleeps).execute(HttpCallSpec(url="https://api.vendor.test/status"), RenderContext())
    assert sleeps.waits == [5.0, 10.0, 20.0]
    assert len(requests) == 4
    assert excinfo.value.trace is not None and excinfo.value.trace.status == 429


async def test_rate_limit_with_a_malformed_retry_after_backs_off() -> None:
    answers = iter([httpx.Response(429, headers={"retry-after": "nan"}), json_response({"ok": True})])
    sleeps = _Sleeps()
    result = await _executor(_descriptor(), lambda request: next(answers), sleep=sleeps).execute(
        HttpCallSpec(url="https://api.vendor.test/status"), RenderContext()
    )
    assert result.data == {"ok": True}
    assert sleeps.waits == [5.0]


async def test_rate_limit_waits_are_capped_per_call_across_pages() -> None:
    """Each page is rate limited once for 50 s: two waits fit the call's 120 s, the third fails it."""
    limited: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page not in limited:
            limited.add(page)
            return httpx.Response(429, headers={"retry-after": "50"})
        return json_response({"results": [int(page)], "next": f"https://api.vendor.test/list?page={int(page) + 1}"})

    sleeps = _Sleeps()
    call = HttpCallSpec(url="https://api.vendor.test/list", paginate=PaginationSpec(next_url="next"))
    with pytest.raises(HttpCallError, match="HTTP 429") as excinfo:
        await _executor(_descriptor(), handler, sleep=sleeps).execute(call, RenderContext())
    assert sleeps.waits == [50.0, 50.0]
    assert excinfo.value.trace is not None and excinfo.value.trace.page == 3


async def test_rate_limit_longer_than_the_longest_wait_fails_at_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "3600"})

    sleeps = _Sleeps()
    with pytest.raises(HttpCallError, match="HTTP 429"):
        await _executor(_descriptor(), handler, sleep=sleeps).execute(HttpCallSpec(url="https://api.vendor.test/status"), RenderContext())
    assert sleeps.waits == []
    assert len(requests) == 1


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
