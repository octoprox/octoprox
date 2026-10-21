# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the failure log: describing a failure, counting it, folding a burst, capping a window, writing to Redis.

The window's shape is read off what the log hands the client's
``failures_batch``, on a stand-in client; what Redis ends up holding, and
what becomes of a window Redis does not answer for, against the real Redis
test container.
"""

import asyncio
import socket
import ssl
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from api.core import utc_now
from api.core.failure_log import (
    MAX_MESSAGE_CHARS,
    FailureLog,
    RequestFailure,
    RequestInfo,
    build_failure,
    classify_upstream_error,
    set_request_info,
)
from api.core.job_stats import job_stats
from api.core.stats import FailureKind
from api.core.workers import WorkerName
from api.db.redis import (
    FAILURE_LOG_KEY,
    FAILURE_WINDOWS,
    BatchChannel,
    BatchId,
    BatchOutcome,
    BatchPart,
    BatchReply,
    RedisClient,
)
from api.models.proxy import Proxy, ProxyProtocol
from tests.waits import running, until


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


def _recording_redis() -> MagicMock:
    """A client that takes every window whole: ``failures_batch`` records what it is handed, every part lands."""

    def prepare(entries: list[Any], counts: dict[Any, int], **_: Any) -> list[BatchPart]:
        items = len(entries) + len(counts)
        return [BatchPart(BatchId(BatchChannel.FAILURES, 1), (), (), items)] if items else []

    redis = MagicMock(spec=RedisClient)
    redis.failures_batch.side_effect = prepare
    redis.send_batch = AsyncMock(side_effect=lambda parts: [BatchReply(BatchOutcome.APPLIED, 1) for _ in parts])
    return redis


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
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        start = utc_now()
        for i in range(500):
            log.record(_failure(at=start + timedelta(milliseconds=i), latency_ms=float(1000 + i % 2 * 2000)))

        assert await log.flush() == 2  # one example, one count
        (entries, counts), kwargs = redis.failures_batch.call_args
        assert kwargs == {"max_entries": 1000, "retention_seconds": 168 * 3600}
        project_id, kind, fields = entries[0]
        assert (project_id, kind) == ("project-1", "upstream_timeout")
        assert fields["count"] == "500"
        assert fields["first_at"] == start.isoformat()
        assert fields["last_at"] == (start + timedelta(milliseconds=499)).isoformat()
        assert fields["latency_ms"] == "2000.0"
        assert counts == {("project-1", "connector-1", "upstream_timeout", None): 500}

    async def test_the_session_does_not_split_a_burst_but_the_target_does(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        log.record(_failure(session_id="a"))
        log.record(_failure(session_id="b"))
        log.record(_failure(target="other.example:443"))

        await log.flush()
        entries = redis.failures_batch.call_args.args[0]
        assert sorted(fields["count"] for _, _, fields in entries) == ["1", "2"]

    async def test_past_the_cap_failures_are_counted_but_not_kept(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings(failure_log_max_per_window=2))
        for i in range(5):
            log.record(_failure(target=f"host{i}.example:443"))
        # Another kind, and another project, each have their own allowance.
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        log.record(_failure(project_id="project-2"))

        await log.flush()
        entries, counts = redis.failures_batch.call_args.args
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
        redis = _recording_redis()
        log = FailureLog(redis, _settings(failure_log_max_per_window=10))
        for i in range(1000):
            log.record(_failure(target=f"host{i}.example:443"))
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407, message="407"))

        await log.flush()
        kinds = [kind for _, kind, _ in redis.failures_batch.call_args.args[0]]
        assert kinds.count("upstream_timeout") == 10
        assert kinds.count("upstream_rejected") == 1

    async def test_a_failure_without_a_connector_is_counted_without_one(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.flush()
        assert redis.failures_batch.call_args.args[1] == {("project-1", None, "no_proxy_available", None): 1}

    async def test_a_publish_starts_a_new_window(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        await log.flush()
        log.record(_failure(target="next.example:443"))
        assert await log.flush() == 2
        assert redis.failures_batch.call_args.args[0][0][2]["target"] == "next.example:443"
        assert await log.flush() == 0

    async def test_connector_names_are_written_in(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings(), connector_name=lambda cid: {"connector-1": "Bright Data"}.get(cid))
        log.record(_failure())
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.flush()
        entries = redis.failures_batch.call_args.args[0]
        names = {kind: f.get("connector_name") for _, kind, f in entries}
        assert names == {"upstream_timeout": "Bright Data", "no_proxy_available": None}

    async def test_a_removed_project_is_forgotten(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings(failure_log_max_per_window=1))
        log.record(_failure())
        log.record(_failure(target="over.example:443"))
        log.record(_failure(project_id="project-2"))
        log.forget("project-1")

        await log.flush()
        entries, counts = redis.failures_batch.call_args.args
        assert [p for p, _, _ in entries] == ["project-2"]
        assert {key[0] for key in counts} == {"project-2"}

    async def test_a_removed_connector_loses_its_counts_but_not_its_examples(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        log.record(_failure(connector_id="gone"))
        log.record(_failure(connector_id="kept", target="other.example:443"))
        log.forget_connector("gone")

        await log.flush()
        entries, counts = redis.failures_batch.call_args.args
        assert len(entries) == 2
        assert {key[1] for key in counts} == {"kept"}

    async def test_a_publish_with_nothing_counted_sends_nothing(self) -> None:
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        assert await log.flush() == 0
        assert not redis.send_batch.called

    async def test_examples_left_without_counts_still_go(self) -> None:
        """A connector forgotten after its failures were kept: its examples belong to the project and still go."""
        redis = _recording_redis()
        log = FailureLog(redis, _settings())
        log.record(_failure(connector_id="gone"))
        log.forget_connector("gone")
        await log.flush()
        entries, counts = redis.failures_batch.call_args.args
        assert (len(entries), counts) == (1, {})


    async def test_pending_is_the_window_s_examples_and_counts(self) -> None:
        """What a stop that cannot reach Redis reports as lost."""
        log = FailureLog(None, _settings())
        for _ in range(3):
            log.record(_failure())
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))

        # Two examples, two counts.
        assert log.pending == 4


class TestCountKeys:
    def test_round_trip(self) -> None:
        for key in [
            ("p-1", "c-1", "upstream_rejected", 407),
            ("p-1", None, "no_proxy_available", None),
        ]:
            assert FAILURE_WINDOWS.entity(FAILURE_WINDOWS.key(key)) == key

    def test_foreign_keys_are_ignored(self) -> None:
        assert FAILURE_WINDOWS.entity("o6:host:metrics:p:c:example.com") is None
        assert FAILURE_WINDOWS.entity("o6:failure:metrics:p:c:kind") is None
        assert FAILURE_WINDOWS.entity("o6:failure:metrics:p:c:kind:abc") is None
        assert FAILURE_WINDOWS.entity("o6:failure:metrics:p::kind:407") is None
        assert FAILURE_WINDOWS.entity("failure:metrics:p:c:kind:407") is None  # a 5.x key
        assert FAILURE_WINDOWS.entity("p:c:kind:407") is None  # the shape without the prefix


ALL_KINDS = [kind.value for kind in FailureKind]


async def _counted(redis_client: RedisClient) -> dict[Any, int]:
    """The failure counts Redis holds by key, as the metrics flush takes them, left in place."""
    counts: dict[Any, int] = {}
    for key in await redis_client.client.smembers(FAILURE_WINDOWS.index):
        count = int(await redis_client.client.hget(key, "failure_count") or 0)
        if count > 0:
            counts[FAILURE_WINDOWS.entity(key)] = count
    return counts


class Outage:
    """Makes the batch sends fail, as an unreachable Redis does, while it is on; records every send.

    With ``reply_lost`` the send is applied and only its reply lost, and ``refusals`` is how many of the next sends Redis
    refuses every part of, running none, as it does while another script overruns (``BUSY``).
    """

    def __init__(self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self.on = False
        self.reply_lost = False
        self.refusals = 0
        self.sends: list[list[BatchPart]] = []
        real = redis_client.send_batch

        async def send(parts: list[BatchPart]) -> list[BatchReply]:
            self.sends.append(list(parts))
            if self.refusals:
                self.refusals -= 1
                return [BatchReply(BatchOutcome.REFUSED) for _ in parts]
            if self.on and not self.reply_lost:
                raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
            replies = await real(parts)
            if self.on:
                raise ConnectionError("Connection closed by server.")
            return replies

        monkeypatch.setattr(redis_client, "send_batch", send)


@pytest.fixture
def outage(redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> Outage:
    return Outage(redis_client, monkeypatch)


class TestRedisStreams:
    async def test_examples_read_back_newest_first_across_kinds(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        start = utc_now()
        log.record(_failure(target="first.example:443", at=start))
        log.record(_failure(target="second.example:443", at=start + timedelta(seconds=1),
                            kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        await log.flush()

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
        await log.flush()

        targets = {r["target"] for r in await redis_client.get_failures("project-1", ALL_KINDS)}
        assert targets == {"peer", "ours.example:443", "also-ours.example:443"}
        assert await redis_client.failures_appended("project-1") == {"upstream_timeout": 1, "no_proxy_available": 1}

    async def test_each_kind_keeps_its_own_examples(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings(failure_log_max_entries=3, failure_log_max_per_window=1000))
        log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        await log.flush()
        for i in range(10):
            log.record(_failure(target=f"host{i}.example:443"))
        await log.flush()

        kinds = [r["kind"] for r in await redis_client.get_failures("project-1", ALL_KINDS, count=50)]
        assert kinds.count("upstream_timeout") == 3
        assert kinds.count("upstream_rejected") == 1

    async def test_the_log_expires_after_the_retention(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings(failure_log_retention_hours=2))
        log.record(_failure())
        await log.flush()
        ttl = await redis_client.client.ttl(FAILURE_LOG_KEY.format(project_id="project-1", kind="upstream_timeout"))
        assert 0 < ttl <= 7200

    async def test_counts_land_in_the_metrics_hashes(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        for _ in range(3):
            log.record(_failure(kind=FailureKind.UPSTREAM_REJECTED, upstream_status=407))
        log.record(_failure(connector_id=None, proxy_id=None, proxy=None, kind=FailureKind.NO_PROXY_AVAILABLE))
        await log.flush()

        counts = await _counted(redis_client)
        assert counts == {
            ("project-1", "connector-1", "upstream_rejected", 407): 3,
            ("project-1", None, "no_proxy_available", None): 1,
        }

    async def test_clear_drops_the_examples_but_not_the_counts(self, redis_client: RedisClient) -> None:
        log = FailureLog(redis_client, _settings())
        log.record(_failure())
        await log.flush()
        await redis_client.clear_failures("project-1", ALL_KINDS)
        assert await redis_client.get_failures("project-1", ALL_KINDS) == []
        assert await redis_client.failures_appended("project-1") == {}
        assert await _counted(redis_client) != {}


class TestOutage:
    """A window Redis does not answer for is owed: sent again as it was, never lost to a blip, never counted twice."""

    async def test_a_window_redis_did_not_take_lands_with_the_one_counted_since(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        log = FailureLog(redis_client, _settings())
        log.record(_failure(target="first.example:443"))
        outage.on = True
        with pytest.raises(ConnectionError):
            await log.flush()
        assert await _counted(redis_client) == {}
        log.record(_failure(target="second.example:443"))

        outage.on = False
        assert await log.flush() == 2 + 2

        targets = [r["target"] for r in await redis_client.get_failures("project-1", ALL_KINDS)]
        assert targets == ["second.example:443", "first.example:443"]
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 2
        }
        assert log.dropped == 0

    async def test_a_window_redis_applied_but_did_not_answer_for_lands_once(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        """The reply lost on its way back: sent again, the window is known by its identity and not added twice."""
        log = FailureLog(redis_client, _settings())
        for _ in range(3):
            log.record(_failure())
        outage.on, outage.reply_lost = True, True
        with pytest.raises(ConnectionError):
            await log.flush()

        outage.on = False
        await log.flush()
        first, second = outage.sends
        assert second == first

        assert await redis_client.count_failures("project-1", ALL_KINDS) == 1
        assert await redis_client.failures_appended("project-1") == {"upstream_timeout": 1}
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 3
        }

    async def test_failures_keep_collecting_under_the_cap_while_a_window_is_owed(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        log = FailureLog(redis_client, _settings(failure_log_max_per_window=2))
        for i in range(5):
            log.record(_failure(target=f"early{i}.example:443"))
        outage.on = True
        with pytest.raises(ConnectionError):
            await log.flush()
        for i in range(5):
            log.record(_failure(target=f"late{i}.example:443"))
        with pytest.raises(ConnectionError):
            await log.flush()
        # Only what was owed went again; the second window waits behind it.
        assert outage.sends[1] == outage.sends[0]

        outage.on = False
        await log.flush()

        # Two windows of two examples each, every failure counted.
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 4
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 10
        }

    async def test_a_window_waits_while_what_is_owed_keeps_being_refused(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        """Sent beside a part refused again, the window would take the part's place in what is owed, and lose it."""
        log = FailureLog(redis_client, _settings())
        log.record(_failure(target="first.example:443"))
        outage.refusals = 2
        assert await log.flush() == 0
        log.record(_failure(target="second.example:443"))
        assert await log.flush() == 0
        assert [len(parts) for parts in outage.sends] == [1, 1]

        assert await log.flush() == 2 + 2
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 2
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 2
        }

    async def test_a_part_redis_refused_to_run_goes_again_under_a_new_sequence(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        """The script cache was flushed between two windows: Redis runs nothing of the part and says NOSCRIPT."""
        log = FailureLog(redis_client, _settings())
        log.record(_failure(target="first.example:443"))
        await log.flush()
        await redis_client.client.script_flush()
        log.record(_failure(target="second.example:443"))

        assert await log.flush() == 0
        assert await log.flush() == 2

        refused, resent = outage.sends[1:]
        assert (refused[0].keys, refused[0].args) == (resent[0].keys, resent[0].args)
        assert resent[0].id.sequence > refused[0].id.sequence
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 2
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 2
        }

    async def test_a_window_whose_script_failed_part_way_is_dropped_and_counted(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        """Its mark moved before it wrote anything: sent again it would be skipped, and it may have written half."""
        count_key = ("project-1", "connector-1", "upstream_timeout", None)
        # A key of the wrong type where the window's count goes: the script fails after its examples.
        await redis_client.client.set(FAILURE_WINDOWS.key(count_key), "not a hash")
        log = FailureLog(redis_client, _settings())
        log.record(_failure())

        assert await log.flush() == 0
        # Its example and its count.
        assert log.dropped == 2
        assert await log.flush() == 0
        assert len(outage.sends) == 1

    async def test_one_send_at_a_time_so_no_window_overtakes_an_older_one(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A newer window sent first would mark the older one applied, and Redis would skip it."""
        entered, released = asyncio.Event(), asyncio.Event()
        real = redis_client.send_batch

        async def slow_send(parts: list[BatchPart]) -> list[BatchReply]:
            if not entered.is_set():
                entered.set()
                await released.wait()
            return await real(parts)

        monkeypatch.setattr(redis_client, "send_batch", slow_send)
        log = FailureLog(redis_client, _settings())
        log.record(_failure(target="first.example:443"))
        first = asyncio.create_task(log.flush())
        await asyncio.wait_for(entered.wait(), 2)
        # The gateway stops while the loop's flush is on its way: its last flush waits for it.
        log.record(_failure(target="second.example:443"))
        last = asyncio.create_task(log.flush())
        await asyncio.sleep(0.05)
        assert not last.done()

        released.set()
        await asyncio.wait_for(asyncio.gather(first, last), 2)
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 2
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 2
        }

    async def test_the_loop_counts_a_window_redis_did_not_take_as_a_failed_cycle(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        job_stats.reset()
        log = FailureLog(redis_client, _settings(), interval=0.02)
        log.record(_failure())
        outage.on = True
        try:
            async with running(log):
                await until(
                    lambda: (stats := job_stats.get(WorkerName.FAILURE_LOG_PUBLISHER)) is not None and stats.failures >= 2
                )
                outage.on = False
                await until(lambda: log.owed == 0 and len(outage.sends) >= 3)
        finally:
            job_stats.reset()
        assert await _counted(redis_client) == {
            ("project-1", "connector-1", "upstream_timeout", None): 1
        }


    async def test_a_stop_sends_the_window_once_more(self, redis_client: RedisClient) -> None:
        """The gateway stops its loops once its listeners are closed: what the last seconds failed still lands."""
        log = FailureLog(redis_client, _settings())
        task = asyncio.create_task(log.run())
        await asyncio.sleep(0.01)
        log.record(_failure())

        await log.stop()

        await asyncio.wait_for(task, 1)
        assert await redis_client.count_failures("project-1", ALL_KINDS) == 1
        assert await _counted(redis_client) == {("project-1", "connector-1", "upstream_timeout", None): 1}
