# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the failure log: describing a failure, folding a burst, capping a window, writing to Redis."""

import asyncio
import json
import socket
import ssl
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from api.core import utc_now
from api.core.failure_log import (
    MAX_MESSAGE_CHARS,
    SUPPRESSED,
    FailureKind,
    FailureLog,
    RequestFailure,
    RequestInfo,
    build_failure,
    classify_upstream_error,
    set_request_info,
)
from api.db.redis import FAILURE_LOG_KEY, RedisClient
from api.models.proxy import Proxy, ProxyProtocol


def _settings(**overrides: Any) -> Any:
    values = {
        "failure_log_enabled": True,
        "failure_log_max_entries": 1000,
        "failure_log_retention_hours": 168,
        "failure_log_max_per_window": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _proxy(protocol: ProxyProtocol = ProxyProtocol.HTTP) -> Proxy:
    return Proxy(
        host="gate.vendor.example",
        port=7000,
        protocol=protocol,
        username="vendor-user",
        password="s3cret-pass",
        connector_id="connector-1",
    )


def _failure(**overrides: Any) -> RequestFailure:
    values: dict[str, Any] = {
        "project_id": "project-1",
        "kind": FailureKind.UPSTREAM_TIMEOUT,
        "status": 504,
        "message": "No answer from the upstream proxy within 30 seconds",
        "target": "example.com:443",
        "method": "CONNECT",
        "proxy_id": "proxy-1",
        "connector_id": "connector-1",
        "proxy": "gate.vendor.example:7000",
        "latency_ms": 30000.0,
    }
    values.update(overrides)
    return RequestFailure(**values)


class TestBuildFailure:
    async def test_fills_in_what_the_connection_asked_for(self) -> None:
        # Run in a task of its own so the request info does not leak into other tests.
        async def run() -> RequestFailure:
            set_request_info(RequestInfo(
                target="example.com:443", method="CONNECT", session_id="s1", location="US/NY",
                peer_id="peer-1", peer_name="laptop",
            ))
            return build_failure(
                "project-1", FailureKind.UPSTREAM_REJECTED, 502, "407 Proxy Authentication Required",
                proxy=_proxy(), exc=ProxyError("407 Proxy Authentication Required", error_code=407),
                latency_ms=12.345, upstream_status=407,
            )

        failure = await asyncio.create_task(run())
        assert failure.target == "example.com:443"
        assert failure.method == "CONNECT"
        assert failure.session_id == "s1"
        assert failure.location == "US/NY"
        assert (failure.peer_id, failure.peer_name) == ("peer-1", "laptop")
        assert failure.proxy == "gate.vendor.example:7000"
        assert failure.connector_id == "connector-1"
        assert failure.error_type == "ProxyError"
        assert failure.upstream_status == 407
        assert failure.latency_ms == 12.3

    async def test_explicit_method_and_target_win(self) -> None:
        async def run() -> RequestFailure:
            set_request_info(RequestInfo(target="example.com:443", method="CONNECT"))
            return build_failure(
                "project-1", FailureKind.INTERCEPTED_REQUEST_FAILED, 502, "boom",
                method="POST", target="https://example.com/api",
            )

        failure = await asyncio.create_task(run())
        assert (failure.method, failure.target) == ("POST", "https://example.com/api")

    def test_without_request_info_the_fields_stay_empty(self) -> None:
        failure = build_failure("project-1", FailureKind.NO_PROXY_AVAILABLE, 502, "none")
        assert failure.target is None and failure.session_id is None and failure.proxy is None

    def test_upstream_credentials_never_reach_the_log(self) -> None:
        proxy = _proxy()
        failure = build_failure(
            "project-1", FailureKind.UPSTREAM_ERROR, 502,
            f"curl: (56) could not reach {proxy.url}, password was {proxy.password}",
            proxy=proxy,
        )
        assert "s3cret-pass" not in failure.message
        assert "vendor-user:" not in failure.message
        assert "http://gate.vendor.example:7000" in failure.message

    def test_percent_encoded_passwords_are_scrubbed_too(self) -> None:
        proxy = _proxy().model_copy(update={"username": "u", "password": "p@ss/word"})
        failure = build_failure("project-1", FailureKind.UPSTREAM_ERROR, 502, "proxy http://u:p%40ss%2Fword@gate", proxy=proxy)
        assert "p%40ss%2Fword" not in failure.message

    def test_queries_of_quoted_urls_are_cut(self) -> None:
        # rnet's error text names the full request URL.
        message = 'is_connect error: wreq::Error { kind: Request, url: "https://example.com/search?token=SECRET" }'
        failure = build_failure("project-1", FailureKind.INTERCEPTED_REQUEST_FAILED, 502, message)
        assert "SECRET" not in failure.message
        assert 'url: "https://example.com/search"' in failure.message

    def test_long_messages_are_cut(self) -> None:
        failure = build_failure("project-1", FailureKind.UPSTREAM_ERROR, 502, "x" * 5000)
        assert len(failure.message) == MAX_MESSAGE_CHARS


class TestClassifyUpstreamError:
    def test_http_vendor_refusal_carries_its_status(self) -> None:
        exc = ProxyError("407 Proxy Authentication Required", error_code=407)
        assert classify_upstream_error(exc, _proxy()) == (FailureKind.UPSTREAM_REJECTED, 407)

    def test_socks_reply_code_is_not_an_http_status(self) -> None:
        exc = ProxyError("Host unreachable", error_code=4)
        assert classify_upstream_error(exc, _proxy(ProxyProtocol.SOCKS5)) == (FailureKind.UPSTREAM_REJECTED, None)

    def test_a_dropped_handshake_is_an_error_not_a_refusal(self) -> None:
        exc = ProxyError("Connection closed unexpectedly")
        assert classify_upstream_error(exc, _proxy()) == (FailureKind.UPSTREAM_ERROR, None)

    def test_timeouts(self) -> None:
        assert classify_upstream_error(TimeoutError(), _proxy())[0] is FailureKind.UPSTREAM_TIMEOUT
        assert classify_upstream_error(ProxyTimeoutError("t"), _proxy())[0] is FailureKind.UPSTREAM_TIMEOUT

    def test_unreachable(self) -> None:
        assert classify_upstream_error(ConnectionRefusedError(), _proxy())[0] is FailureKind.UPSTREAM_UNREACHABLE
        refused = ProxyConnectionError(61, "Could not connect to proxy")
        assert classify_upstream_error(refused, _proxy())[0] is FailureKind.UPSTREAM_UNREACHABLE

    def test_socks_credential_refusal_is_a_refusal(self) -> None:
        exc = ProxyError("Username and password authentication failure")
        assert classify_upstream_error(exc, _proxy(ProxyProtocol.SOCKS5)) == (FailureKind.UPSTREAM_REJECTED, None)

    def test_no_route_and_unresolvable_names_are_unreachable(self) -> None:
        assert classify_upstream_error(OSError(65, "No route to host"), _proxy())[0] is FailureKind.UPSTREAM_UNREACHABLE
        assert classify_upstream_error(OSError("Multiple exceptions: ..."), _proxy())[0] is FailureKind.UPSTREAM_UNREACHABLE
        name_error = socket.gaierror(8, "nodename nor servname provided")
        assert classify_upstream_error(name_error, _proxy())[0] is FailureKind.UPSTREAM_UNREACHABLE
        assert classify_upstream_error(ssl.SSLError("bad handshake"), _proxy())[0] is FailureKind.UPSTREAM_ERROR
        assert classify_upstream_error(ConnectionResetError(), _proxy())[0] is FailureKind.UPSTREAM_ERROR

    def test_anything_else(self) -> None:
        assert classify_upstream_error(ValueError("bad"), _proxy()) == (FailureKind.UPSTREAM_ERROR, None)


class TestWindow:
    async def test_a_burst_of_the_same_failure_is_one_entry(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        start = utc_now()
        for i in range(500):
            log.record(_failure(at=start + timedelta(milliseconds=i), latency_ms=float(1000 + i % 2 * 2000)))

        assert await log.publish() == 1
        (entries,), kwargs = redis.append_failures.await_args
        assert kwargs == {"max_entries": 1000, "retention_seconds": 168 * 3600}
        project_id, fields = entries[0]
        assert project_id == "project-1"
        assert fields["count"] == "500"
        assert fields["first_at"] == start.isoformat()
        assert fields["last_at"] == (start + timedelta(milliseconds=499)).isoformat()
        assert fields["latency_ms"] == "2000.0"

    async def test_the_session_does_not_split_a_burst_but_the_target_does(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        log.record(_failure(session_id="a"))
        log.record(_failure(session_id="b"))
        log.record(_failure(target="other.example:443"))

        assert await log.publish() == 2
        entries = redis.append_failures.await_args.args[0]
        assert sorted(fields["count"] for _, fields in entries) == ["1", "2"]

    async def test_past_the_cap_failures_are_only_tallied(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=2))
        for i in range(5):
            log.record(_failure(target=f"host{i}.example:443"))
        log.record(_failure(target="host9.example:443", kind=FailureKind.NO_PROXY_AVAILABLE))
        # A second project has its own allowance.
        log.record(_failure(project_id="project-2"))

        await log.publish()
        entries = redis.append_failures.await_args.args[0]
        logged = [(p, f) for p, f in entries if f["kind"] != SUPPRESSED]
        suppressed = [(p, f) for p, f in entries if f["kind"] == SUPPRESSED]
        assert [p for p, _ in logged].count("project-1") == 2
        assert [p for p, _ in logged].count("project-2") == 1
        assert len(suppressed) == 1
        project_id, fields = suppressed[0]
        assert project_id == "project-1"
        assert fields["count"] == "4"
        assert json.loads(fields["breakdown"]) == {"upstream_timeout": 3, "no_proxy_available": 1}

    async def test_a_publish_starts_a_new_window(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        await log.publish()
        log.record(_failure(target="next.example:443"))
        assert await log.publish() == 1
        assert redis.append_failures.await_args.args[0][0][1]["kind"] == "upstream_timeout"
        assert await log.publish() == 0

    async def test_connector_names_are_written_in(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(), connector_name=lambda cid: {"connector-1": "Bright Data"}.get(cid))
        log.record(_failure())
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.publish()
        entries = redis.append_failures.await_args.args[0]
        names = {f["kind"]: f.get("connector_name") for _, f in entries}
        assert names == {"upstream_timeout": "Bright Data", "no_proxy_available": None}

    async def test_a_removed_project_is_forgotten(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        log.record(_failure(target="over.example:443"))
        log.record(_failure(project_id="project-2"))
        log.forget("project-1")

        assert await log.publish() == 1
        assert [p for p, _ in redis.append_failures.await_args.args[0]] == ["project-2"]

    async def test_an_unreachable_redis_drops_the_window(self) -> None:
        redis = AsyncMock()
        redis.append_failures.side_effect = ConnectionError("down")
        log = FailureLog(redis, _settings())
        log.record(_failure())
        assert await log.publish() == 0
        assert log.dropped == 1
        redis.append_failures.side_effect = None
        assert await log.publish() == 0  # nothing kept for a retry

    async def test_disabled_log_does_not_subscribe(self) -> None:
        from api.core.signals import request_failed

        log = FailureLog(AsyncMock(), _settings(failure_log_enabled=False))
        before = len(request_failed.receivers)
        log.subscribe_to_signals()
        assert len(request_failed.receivers) == before


class TestRedisStream:
    async def test_entries_read_back_newest_first(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        start = utc_now()
        log.record(_failure(target="first.example:443", at=start))
        log.record(_failure(target="second.example:443", at=start + timedelta(seconds=1)))
        await log.publish()

        records = await redis_client.get_failures("project-1")
        assert [r["target"] for r in records] == ["second.example:443", "first.example:443"]
        assert await redis_client.count_failures("project-1") == 2
        assert "session_id" not in records[0]  # empty fields are not written

    async def test_the_log_expires_after_the_retention(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings(failure_log_retention_hours=2))
        log.record(_failure())
        await log.publish()
        ttl = await redis_client.client.ttl(FAILURE_LOG_KEY.format(project_id="project-1"))
        assert 0 < ttl <= 7200

    async def test_clear(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        log.record(_failure())
        await log.publish()
        await redis_client.clear_failures("project-1")
        assert await redis_client.get_failures("project-1") == []
