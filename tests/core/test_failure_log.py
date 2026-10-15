# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the failure log: describing a failure, counting it, folding a burst, capping a window, writing to Redis."""

import asyncio
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
    FailureKind,
    FailureLog,
    RequestFailure,
    RequestInfo,
    build_failure,
    classify_upstream_error,
    set_request_info,
)
from api.db.redis import (
    FAILURE_LOG_KEY,
    RedisClient,
    failure_metrics_key,
    parse_failure_metrics_key,
)
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
    async def test_a_burst_of_the_same_failure_is_one_example_and_counted_in_full(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        start = utc_now()
        for i in range(500):
            log.record(_failure(at=start + timedelta(milliseconds=i), latency_ms=float(1000 + i % 2 * 2000)))

        assert await log.publish() == 2  # one example, one count
        (entries, counts), kwargs = redis.append_failures.await_args
        assert kwargs == {"max_entries": 1000, "retention_seconds": 168 * 3600}
        project_id, kind, fields = entries[0]
        assert (project_id, kind) == ("project-1", "upstream_timeout")
        assert fields["count"] == "500"
        assert fields["first_at"] == start.isoformat()
        assert fields["last_at"] == (start + timedelta(milliseconds=499)).isoformat()
        assert fields["latency_ms"] == "2000.0"
        assert counts == {("project-1", "connector-1", "upstream_timeout", None): 500}

    async def test_the_session_does_not_split_a_burst_but_the_target_does(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        log.record(_failure(session_id="a"))
        log.record(_failure(session_id="b"))
        log.record(_failure(target="other.example:443"))

        await log.publish()
        entries = redis.append_failures.await_args.args[0]
        assert sorted(fields["count"] for _, _, fields in entries) == ["1", "2"]

    async def test_past_the_cap_failures_are_counted_but_not_kept(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=2))
        for i in range(5):
            log.record(_failure(target=f"host{i}.example:443"))
        # Another kind, and another project, each have their own allowance.
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        log.record(_failure(project_id="project-2"))

        await log.publish()
        entries, counts = redis.append_failures.await_args.args
        kept = [(project_id, kind) for project_id, kind, _ in entries]
        assert kept.count(("project-1", "upstream_timeout")) == 2
        assert kept.count(("project-1", "upstream_rejected")) == 1
        assert kept.count(("project-2", "upstream_timeout")) == 1
        assert counts == {
            ("project-1", "connector-1", "upstream_timeout", None): 5,
            ("project-1", "connector-1", "upstream_rejected", 407): 1,
            ("project-2", "connector-1", "upstream_timeout", None): 1,
        }

    async def test_a_flood_of_one_kind_leaves_room_for_another(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=10))
        for i in range(1000):
            log.record(_failure(target=f"host{i}.example:443"))
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407, message="407"))

        await log.publish()
        kinds = [kind for _, kind, _ in redis.append_failures.await_args.args[0]]
        assert kinds.count("upstream_timeout") == 10
        assert kinds.count("upstream_rejected") == 1

    async def test_a_failure_without_a_connector_is_counted_without_one(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.publish()
        assert redis.append_failures.await_args.args[1] == {("project-1", None, "no_proxy_available", None): 1}

    async def test_a_publish_starts_a_new_window(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        await log.publish()
        log.record(_failure(target="next.example:443"))
        assert await log.publish() == 2
        assert redis.append_failures.await_args.args[0][0][2]["target"] == "next.example:443"
        assert await log.publish() == 0

    async def test_connector_names_are_written_in(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(), connector_name=lambda cid: {"connector-1": "Bright Data"}.get(cid))
        log.record(_failure())
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.publish()
        entries = redis.append_failures.await_args.args[0]
        names = {kind: f.get("connector_name") for _, kind, f in entries}
        assert names == {"upstream_timeout": "Bright Data", "no_proxy_available": None}

    async def test_a_removed_project_is_forgotten(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        log.record(_failure(target="over.example:443"))
        log.record(_failure(project_id="project-2"))
        log.forget("project-1")

        await log.publish()
        entries, counts = redis.append_failures.await_args.args
        assert [p for p, _, _ in entries] == ["project-2"]
        assert {key[0] for key in counts} == {"project-2"}

    async def test_a_removed_connector_loses_its_counts_but_not_its_examples(self) -> None:
        redis = AsyncMock()
        log = FailureLog(redis, _settings())
        log.record(_failure(connector_id="gone"))
        log.record(_failure(connector_id="kept", target="other.example:443"))
        log.forget_connector("gone")

        await log.publish()
        entries, counts = redis.append_failures.await_args.args
        assert len(entries) == 2
        assert {key[1] for key in counts} == {"kept"}

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


class TestCountKeys:
    def test_round_trip(self) -> None:
        for key in [
            ("p-1", "c-1", "upstream_rejected", 407),
            ("p-1", None, "no_proxy_available", None),
        ]:
            assert parse_failure_metrics_key(failure_metrics_key(key)) == key

    def test_foreign_keys_are_ignored(self) -> None:
        assert parse_failure_metrics_key("host:metrics:p:c:example.com") is None
        assert parse_failure_metrics_key("failure:metrics:p:c:kind") is None
        assert parse_failure_metrics_key("failure:metrics:p:c:kind:abc") is None


ALL_KINDS = [kind.value for kind in FailureKind]


async def _drain_failures(redis_client: RedisClient, failures: dict[Any, int]) -> None:
    await redis_client.drain_flushed_metrics(
        proxies={}, projects={}, connectors={}, tunnel_peers={}, hosts={}, failures=failures,
    )


class TestRedisStreams:
    async def test_examples_read_back_newest_first_across_kinds(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        start = utc_now()
        log.record(_failure(target="first.example:443", at=start))
        log.record(_failure(target="second.example:443", at=start + timedelta(seconds=1),
                            kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        await log.publish()

        records = await redis_client.get_failures("project-1", ALL_KINDS)
        assert [r["target"] for r in records] == ["second.example:443", "first.example:443"]
        assert [r["kind"] for r in records] == ["upstream_rejected", "upstream_timeout"]
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 2
        assert await redis_client.count_failures("project-1", ["upstream_rejected"]) == 1
        assert await redis_client.failures_appended("project-1") == {"upstream_timeout": 1, "upstream_rejected": 1}
        assert "session_id" not in records[0]  # empty fields are not written

    async def test_pages_follow_one_another_across_kinds(self, redis_client: RedisClient) -> None:
        # Two streams can hand out the same ID in the same millisecond; the
        # cursor carries the kind so neither is skipped or repeated.
        for i in range(3):
            await redis_client.client.xadd(FAILURE_LOG_KEY.format(project_id="p", kind="upstream_timeout"),
                                           {"target": f"t{i}"}, id=f"100-{i}")
            await redis_client.client.xadd(FAILURE_LOG_KEY.format(project_id="p", kind="no_proxy_available"),
                                           {"target": f"n{i}"}, id=f"100-{i}")
        seen: list[str] = []
        after = None
        while True:
            page = await redis_client.get_failures("p", ALL_KINDS, count=2, after=after)
            if not page:
                break
            seen += [r["target"] for r in page]
            after = (page[-1]["id"], page[-1]["kind"])
        assert seen == ["t2", "n2", "t1", "n1", "t0", "n0"]

    async def test_an_id_another_instance_has_passed_still_lands(self, redis_client: RedisClient) -> None:
        # A peer whose clock runs ahead wrote this stream last.
        key = FAILURE_LOG_KEY.format(project_id="project-1", kind="upstream_timeout")
        await redis_client.client.xadd(key, {"target": "peer"}, id=f"{2**42}-0")  # the year 2109
        log = FailureLog(redis_client, _settings())
        log.record(_failure(target="ours.example:443"))
        log.record(_failure(target="also-ours.example:443", kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.publish()

        targets = {r["target"] for r in await redis_client.get_failures("project-1", ALL_KINDS)}
        assert targets == {"peer", "ours.example:443", "also-ours.example:443"}
        assert await redis_client.failures_appended("project-1") == {"upstream_timeout": 1, "no_proxy_available": 1}

    async def test_each_kind_keeps_its_own_examples(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings(failure_log_max_entries=3, failure_log_max_per_window=1000))
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        await log.publish()
        for i in range(10):
            log.record(_failure(target=f"host{i}.example:443"))
        await log.publish()

        kinds = [r["kind"] for r in await redis_client.get_failures("project-1", ALL_KINDS, count=50)]
        assert kinds.count("upstream_timeout") == 3
        assert kinds.count("upstream_rejected") == 1

    async def test_the_log_expires_after_the_retention(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings(failure_log_retention_hours=2))
        log.record(_failure())
        await log.publish()
        ttl = await redis_client.client.ttl(FAILURE_LOG_KEY.format(project_id="project-1", kind="upstream_timeout"))
        assert 0 < ttl <= 7200

    async def test_counts_land_in_the_metrics_hashes(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        for _ in range(3):
            log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.publish()

        counts = await redis_client.get_all_failure_metrics()
        assert counts == {
            ("project-1", "connector-1", "upstream_rejected", 407): 3,
            ("project-1", None, "no_proxy_available", None): 1,
        }
        await redis_client.reset_failure_metrics_for_connector("connector-1")
        assert await redis_client.get_all_failure_metrics() == {("project-1", None, "no_proxy_available", None): 1}
        await redis_client.reset_failure_metrics_for_project("project-1")
        assert await redis_client.get_all_failure_metrics() == {}

    async def test_draining_keeps_what_was_counted_since_the_read(self, redis_client: RedisClient) -> None:
        key = ("project-1", "connector-1", "upstream_rejected", 407)
        await redis_client.append_failures([], {key: 3}, max_entries=10, retention_seconds=60)
        flushed = await redis_client.get_all_failure_metrics()
        # An instance keeps counting while the flush transaction runs.
        await redis_client.append_failures([], {key: 2}, max_entries=10, retention_seconds=60)
        await _drain_failures(redis_client, flushed)
        assert await redis_client.get_all_failure_metrics() == {key: 2}

        await _drain_failures(redis_client, {key: 2})
        assert not await redis_client.client.exists(failure_metrics_key(key))
        # A hash deleted meanwhile (its connector went) is not left behind below zero.
        await _drain_failures(redis_client, {key: 5})
        assert not await redis_client.client.exists(failure_metrics_key(key))

    async def test_clear_drops_the_examples_but_not_the_counts(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        log.record(_failure())
        await log.publish()
        await redis_client.clear_failures("project-1")
        assert await redis_client.get_failures("project-1", ALL_KINDS) == []
        assert await redis_client.failures_appended("project-1") == {}
        assert await redis_client.get_all_failure_metrics() != {}
