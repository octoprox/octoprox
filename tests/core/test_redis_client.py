# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Redis layer (api.db.redis) against the session's Redis: the key catalogue, metrics, batches and deadlines.

A test that pauses Redis or lowers its busy threshold sets it back before it
ends, since the container is shared.
"""
import asyncio
import json
import socket
import time
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
import redis.exceptions
import structlog

import api.db.redis as catalogue
from api.core.roles import JobKind
from api.core.stats import FailureCount, FailureCountKey, MetricDelta, TunnelPeerMetricDelta
from api.db.redis import (
    APPLIED_BATCHES_KEY,
    APPLIED_BATCHES_TTL_SECONDS,
    AUTOSCALER_STATE_KEY,
    BATCH_REFUSALS,
    CHANGES_STREAM_KEY,
    CONNECTOR_METRICS_KEY,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEMAND_KEY,
    EARLIER_RELEASES_KEY_GROUP,
    FAILURE_LOG_APPENDED_KEY,
    FAILURE_LOG_KEY,
    FAILURE_WINDOWS,
    FEED_HEAD_KEY,
    FEED_MANIFEST_KEY,
    FEED_SECTIONS_KEY,
    FEED_STREAM_KEY,
    HOST_METRICS_KEY,
    HOST_WINDOWS,
    INSTANCE_INDEX_KEY,
    INSTANCE_REGISTRY_KEY,
    INSTANCE_STATS_KEY,
    JOB_STREAM_KEY,
    KEY_PREFIX,
    METRIC_WINDOW_KINDS,
    METRICS_INDEX_KEY,
    METRICS_TAKEN_KEY,
    METRICS_TAKEN_TTL_SECONDS,
    OBSERVATION_STREAM_KEY,
    OTHER_KEY_GROUP,
    POOL_MAX_CONNECTIONS,
    PROJECT_METRICS_KEY,
    PROXY_HEALTH_KEY,
    PROXY_LAST_EXIT_KEY,
    PROXY_METRICS_KEY,
    QUARANTINE_KEY,
    REDIS_KEY_GROUPS,
    REPLY_TIMEOUT_SECONDS,
    RUNTIME_STREAM_KEY,
    SCALE_CREATING_KEY,
    SIGHTING_STREAM_KEY,
    STICKY_BINDING_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
    TUNNEL_PEER_METRICS_KEY,
    TUNNEL_PEER_STATUS_INDEX_KEY,
    TUNNEL_PEER_STATUS_KEY,
    BatchChannel,
    BatchId,
    BatchOutcome,
    BatchPart,
    BatchReply,
    BatchSender,
    MetricWindows,
    RedisClient,
    StreamId,
    StreamInfo,
    classify_key,
    masked_url,
)
from tests.conftest import queued_job_keys


class TestRedisClient:
    """Tests for RedisClient operations."""

    async def test_connect_and_close(self, test_redis_url: str) -> None:
        """Test connecting and closing Redis client."""
        client = RedisClient(test_redis_url)
        await client.connect()
        assert client._client is not None
        await client.close()

    async def test_a_sticky_binding_lives_for_its_ttl(self, redis_client: RedisClient) -> None:
        await redis_client.set_sticky_binding("proj", "sess", "p1", ttl_seconds=60)

        assert await redis_client.get_sticky_binding("proj", "sess") == "p1"
        assert 0 < await redis_client.client.ttl(STICKY_BINDING_KEY.format(project_id="proj", session_id="sess")) <= 60
        assert await redis_client.get_sticky_binding("proj", "other") is None

    async def test_hashes_are_read_whole_in_order_and_a_missing_one_is_empty(self, redis_client: RedisClient) -> None:
        keys = [f"hash-{i}" for i in range(1200)]
        pipe = redis_client.client.pipeline(transaction=False)
        for i, key in enumerate(keys):
            if i % 2 == 0:
                pipe.hset(key, mapping={"n": str(i)})
        await pipe.execute()

        hashes = await redis_client.read_hashes(keys)

        assert hashes == [{"n": str(i)} if i % 2 == 0 else {} for i in range(1200)]


def _requests(n: int, latency_ms: float = 100.0, sent: int = 10) -> MetricDelta:
    delta = MetricDelta()
    for _ in range(n):
        delta.add_request(True, latency_ms, sent, 2 * sent)
    return delta


def _index(kind: str) -> str:
    return METRICS_INDEX_KEY.format(kind=kind)


# Failure counts by (project_id, connector_id, kind, upstream_status): one at a vendor, one refused before a proxy.
COUNT = ("proj", "conn", "upstream_rejected", 429)
REFUSED = ("proj", None, "no_proxy", None)
# A host whose colons must survive its key.
HOST = ("proj", "conn", "[2001:db8::1]:443")
DEVICE = TunnelPeerMetricDelta(
    request_count=2, success_count=1, failure_count=1, latency_sum_ms=12.5, by_address=3, encrypted_dns_blocked=1
)
# A window of every kind, as gateways leave them between two flushes.
EVERY_KIND = MetricWindows(
    proxies={"p1": _requests(3), "p2": _requests(1, latency_ms=0.25)},
    projects={"proj": _requests(4)},
    connectors={"conn": _requests(4)},
    tunnel_peers={"dev:1": DEVICE},
    hosts={HOST: _requests(2)},
    failures={COUNT: FailureCount(2), REFUSED: FailureCount(1)},
)


async def _meter(redis_client: RedisClient, windows: MetricWindows) -> None:
    """Add ``windows`` to the live ones as gateways do: a metering window and a failure log window, each sent once."""
    parts = redis_client.metering_batch(replace(windows, failures={}))
    counts = {count_key: count.failure_count for count_key, count in windows.failures.items()}
    parts += redis_client.failures_batch([], counts, max_entries=10, retention_seconds=60)
    assert all(reply.landed for reply in await redis_client.send_batch(parts))


def _staging(message_id: str) -> str:
    return METRICS_TAKEN_KEY.format(message_id=message_id)


class TestMetricsTake:
    """A flush moves the live windows into a staging hash named by its job entry, then clears it or puts it back."""

    async def test_every_kind_is_taken_into_one_staging_hash_and_read_back(self, redis_client: RedisClient) -> None:
        await _meter(redis_client, EVERY_KIND)

        taken = await redis_client.take_metric_windows("1-0")

        assert taken == EVERY_KIND
        staged = await redis_client.client.hgetall(_staging("1-0"))
        assert set(staged) == {key for _kind, key, _window in EVERY_KIND.keyed()}
        # Each window as metering left it, its write time included.
        p1 = json.loads(staged[PROXY_METRICS_KEY.format(proxy_id="p1")])
        assert (p1["request_count"], p1["latency_sum_ms"], "updated_at" in p1) == ("3", "300", True)

    async def test_the_live_windows_and_their_index_members_are_gone(self, redis_client: RedisClient) -> None:
        await _meter(redis_client, EVERY_KIND)

        await redis_client.take_metric_windows("1-0")

        for _kind, key, _window in EVERY_KIND.keyed():
            assert not await redis_client.client.exists(key), key
        for kind in METRIC_WINDOW_KINDS:
            assert await redis_client.client.scard(kind.index) == 0, kind.name

    async def test_a_member_whose_window_is_gone_leaves_its_index(self, redis_client: RedisClient) -> None:
        gone = PROXY_METRICS_KEY.format(proxy_id="gone")
        await redis_client.client.sadd(_index("proxy"), gone)
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(1)}))

        taken = await redis_client.take_metric_windows("1-0")

        assert taken == MetricWindows(proxies={"p1": _requests(1)})
        assert await redis_client.client.smembers(_index("proxy")) == set()
        assert gone not in await redis_client.client.hkeys(_staging("1-0"))

    async def test_increments_after_a_take_land_in_fresh_windows_for_the_next_flush(
        self, redis_client: RedisClient
    ) -> None:
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(3)}))
        await redis_client.take_metric_windows("1-0")

        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(1)}))

        assert await redis_client.client.smembers(_index("proxy")) == {PROXY_METRICS_KEY.format(proxy_id="p1")}
        assert (await redis_client.take_metric_windows("2-0")).proxies == {"p1": _requests(1)}

    async def test_a_take_under_the_same_id_keeps_what_it_staged_and_takes_only_new_windows(
        self, redis_client: RedisClient
    ) -> None:
        """A flush delivered again: what it staged stays as it was, and a window metering recreated waits for the next."""
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(3)}))
        await redis_client.take_metric_windows("1-0")
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(1), "p2": _requests(2)}))

        again = await redis_client.take_metric_windows("1-0")

        assert again.proxies == {"p1": _requests(3), "p2": _requests(2)}
        assert await redis_client.client.smembers(_index("proxy")) == {PROXY_METRICS_KEY.format(proxy_id="p1")}
        assert (await redis_client.take_metric_windows("2-0")).proxies == {"p1": _requests(1)}

    async def test_two_takes_at_once_under_two_ids_take_each_window_once(self, redis_client: RedisClient) -> None:
        proxies = {f"p{i}": MetricDelta(request_count=i + 1) for i in range(1200)}
        await _meter(redis_client, MetricWindows(proxies=proxies))

        first, second = await asyncio.gather(
            redis_client.take_metric_windows("1-0"), redis_client.take_metric_windows("2-0")
        )

        assert not set(first.proxies) & set(second.proxies)
        assert {**first.proxies, **second.proxies} == proxies

    async def test_a_put_back_adds_ints_and_floats_to_the_live_windows_and_lists_them_again(
        self, redis_client: RedisClient
    ) -> None:
        await _meter(redis_client, EVERY_KIND)
        await redis_client.take_metric_windows("1-0")
        # Metering goes on while the flush fails.
        later = MetricDelta(request_count=1, latency_sum_ms=0.5)
        await _meter(redis_client, MetricWindows(proxies={"p1": later}))

        await redis_client.put_back_metric_windows("1-0")

        assert not await redis_client.client.exists(_staging("1-0"))
        live = await redis_client.client.hgetall(PROXY_METRICS_KEY.format(proxy_id="p1"))
        assert (live["request_count"], live["latency_sum_ms"]) == ("4", "300.5")
        p1 = MetricDelta.summed(EVERY_KIND.proxies["p1"], later)
        assert await redis_client.take_metric_windows("2-0") == MetricWindows(
            **{**vars(EVERY_KIND), "proxies": {**EVERY_KIND.proxies, "p1": p1}}
        )

    async def test_a_put_back_delivered_twice_adds_the_counts_once(self, redis_client: RedisClient) -> None:
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(2)}))
        await redis_client.take_metric_windows("1-0")

        await redis_client.put_back_metric_windows("1-0")
        await redis_client.put_back_metric_windows("1-0")

        assert (await redis_client.take_metric_windows("2-0")).proxies == {"p1": _requests(2)}

    async def test_a_put_back_writes_each_window_as_its_writer_does(self, redis_client: RedisClient) -> None:
        """Metering stamps a delta window's write time; the failure log writes a count alone."""
        await _meter(redis_client, EVERY_KIND)
        await redis_client.take_metric_windows("1-0")

        await redis_client.put_back_metric_windows("1-0")

        assert "updated_at" in await redis_client.client.hkeys(PROXY_METRICS_KEY.format(proxy_id="p1"))
        assert await redis_client.client.hgetall(FAILURE_WINDOWS.key(COUNT)) == {"failure_count": "2"}

    async def test_a_clear_deletes_the_staging_hash_and_leaves_the_live_windows_alone(
        self, redis_client: RedisClient
    ) -> None:
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(2)}))
        await redis_client.take_metric_windows("1-0")
        await _meter(redis_client, MetricWindows(proxies={"p2": _requests(1)}))

        await redis_client.clear_metric_windows("1-0")

        assert not await redis_client.client.exists(_staging("1-0"))
        assert (await redis_client.take_metric_windows("2-0")).proxies == {"p2": _requests(1)}

    async def test_the_staging_hash_expires_a_day_after_the_take(self, redis_client: RedisClient) -> None:
        await _meter(redis_client, MetricWindows(proxies={"p1": _requests(1)}))

        await redis_client.take_metric_windows("1-0")

        assert METRICS_TAKEN_TTL_SECONDS == 86400
        assert METRICS_TAKEN_TTL_SECONDS - 5 <= await redis_client.client.ttl(_staging("1-0")) <= 86400

    async def test_a_staging_hash_larger_than_a_page_is_read_whole(self, redis_client: RedisClient) -> None:
        hosts = {("proj", "conn", f"host{i}.example.com"): _requests(1) for i in range(1500)}
        await _meter(redis_client, MetricWindows(hosts=hosts))

        taken = await redis_client.take_metric_windows("1-0")

        assert await redis_client.client.hlen(_staging("1-0")) == 1500
        assert taken.hosts == hosts

    async def test_a_staged_window_that_cannot_be_read_is_left_out_and_logged(self, redis_client: RedisClient) -> None:
        unreadable = {
            PROXY_METRICS_KEY.format(proxy_id="garbled"): "{not json",
            PROXY_METRICS_KEY.format(proxy_id="lots"): json.dumps({"request_count": "lots"}),
            KEY_PREFIX + "unheard:of": json.dumps({"request_count": "1"}),
        }
        await redis_client.client.hset(
            _staging("1-0"),
            mapping={
                **unreadable,
                PROXY_METRICS_KEY.format(proxy_id="good"): json.dumps({"request_count": "2"}),
                # Nothing to write: left out without a word.
                FAILURE_WINDOWS.key(COUNT): json.dumps({"failure_count": "0"}),
            },
        )

        with structlog.testing.capture_logs() as logs:
            taken = await redis_client.take_metric_windows("1-0")

        assert taken == MetricWindows(proxies={"good": MetricDelta(request_count=2)})
        left_out = [log["key"] for log in logs if log["event"] == "Unreadable metrics window left out of the flush"]
        assert sorted(left_out) == sorted(unreadable)

    def test_empty_holds_only_without_a_window_of_any_kind(self) -> None:
        assert MetricWindows().empty
        for kind, windows in vars(EVERY_KIND).items():
            assert not MetricWindows(**{kind: windows}).empty, kind


class TestJobStreams:
    async def test_queued_job_keys_are_a_kind_s_keys_oldest_first(self, redis_client: RedisClient) -> None:
        for key in ("p2", "p1", "p2", "p3"):
            await redis_client.client.xadd(JOB_STREAM_KEY.format(kind=JobKind.HEALTH), {"key": key})
        await redis_client.client.xadd(JOB_STREAM_KEY.format(kind=JobKind.SYNC), {"key": "c1"})

        assert await queued_job_keys(redis_client, JobKind.HEALTH) == ["p2", "p1", "p2", "p3"]
        assert await queued_job_keys(redis_client, JobKind.SCALE) == []


class TestInstanceIndex:
    """Processes are found through the instance index; liveness is still the registry key's TTL."""

    async def test_advertising_lists_the_instance_and_withdrawing_unlists_it(self, redis_client: RedisClient) -> None:
        await redis_client.advertise_instance("a", "all", snapshot='{"x": 1}')
        await redis_client.advertise_instance("b", "all", snapshot=None)
        assert await redis_client.client.smembers(INSTANCE_INDEX_KEY) == {"a", "b"}
        assert await redis_client.client.get(INSTANCE_REGISTRY_KEY.format(instance_id="a")) == "all"
        assert await redis_client.client.get(INSTANCE_STATS_KEY.format(instance_id="a")) == '{"x": 1}'
        assert not await redis_client.client.exists(INSTANCE_STATS_KEY.format(instance_id="b"))

        await redis_client.withdraw_instance("a")

        assert await redis_client.client.smembers(INSTANCE_INDEX_KEY) == {"b"}
        assert not await redis_client.client.exists(
            INSTANCE_REGISTRY_KEY.format(instance_id="a"), INSTANCE_STATS_KEY.format(instance_id="a")
        )

    async def test_an_instance_whose_key_expired_is_pruned(self, redis_client: RedisClient) -> None:
        """A process killed without withdrawing: its key expires, and the controller's hygiene unlists it."""
        await redis_client.advertise_instance("a", "all", snapshot=None)
        await redis_client.advertise_instance("dead", "all", snapshot=None)
        await redis_client.client.delete(INSTANCE_REGISTRY_KEY.format(instance_id="dead"))

        dropped = await redis_client.prune_index(
            INSTANCE_INDEX_KEY, lambda member: INSTANCE_REGISTRY_KEY.format(instance_id=member)
        )

        assert dropped == 1
        assert await redis_client.client.smembers(INSTANCE_INDEX_KEY) == {"a"}


class TestTunnelPeerStatusIndex:
    async def test_carriers_are_read_through_the_index_and_expired_ones_dropped(
        self, redis_client: RedisClient
    ) -> None:
        await redis_client.set_tunnel_peer_status("wireguard", "a", {"peer-1": '{"online": true}'})
        await redis_client.set_tunnel_peer_status("wireguard", "b", {"peer-2": '{"online": false}'})
        await redis_client.client.delete(TUNNEL_PEER_STATUS_KEY.format(protocol="wireguard", instance_id="b"))

        assert await redis_client.get_tunnel_peer_status("wireguard") == [{"peer-1": '{"online": true}'}]
        assert await redis_client.client.smembers(TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol="wireguard")) == {"a"}
        assert await redis_client.get_tunnel_peer_status("openvpn") == []


class TestConnection:
    @pytest.mark.parametrize(
        ("url", "masked"),
        [
            ("redis://:s3cret@redis:6379/0", "redis://:***@redis:6379/0"),
            ("rediss://default:s3cret@redis.internal:6380/1", "rediss://default:***@redis.internal:6380/1"),
            ("redis://redis:6379/0?db=2&password=s3cret", "redis://redis:6379/0?db=2&password=***"),
            ("unix://:s3cret@/var/run/redis.sock?db=0", "unix://:***@/var/run/redis.sock?db=0"),
            ("redis://localhost:6379/0", "redis://localhost:6379/0"),
        ],
    )
    def test_a_masked_url_keeps_everything_but_the_password(self, url: str, masked: str) -> None:
        """redis-py reads a password from the user part and from a query parameter."""
        assert masked_url(url) == masked

    async def test_connecting_logs_the_url_without_its_password(self) -> None:
        # The pool connects on first use, so no server is needed to see the line.
        client = RedisClient("redis://:s3cret@redis.invalid:6379/0")
        with patch("api.db.redis.logger") as log:
            await client.connect()
        await client.close()

        log.info.assert_called_once_with("Connected to Redis", url="redis://:***@redis.invalid:6379/0")

    async def test_every_connection_gets_the_timeouts_and_the_idle_check(self) -> None:
        client = RedisClient(
            "redis://redis.invalid:6379/0", request_timeout=0.75, socket_timeout=1.5, health_check_interval=10
        )
        await client.connect()
        kwargs = client.client.connection_pool.connection_kwargs
        await client.close()

        # A connect is bounded by the request deadline: nothing has been sent when one fails.
        assert (kwargs["socket_timeout"], kwargs["socket_connect_timeout"], kwargs["health_check_interval"]) == (
            1.5,
            0.75,
            10,
        )

    async def test_by_default_a_reply_waits_out_the_backstop_and_a_request_the_deadline(self) -> None:
        client = RedisClient("redis://redis.invalid:6379/0")
        await client.connect()
        kwargs = client.client.connection_pool.connection_kwargs
        await client.close()

        assert (kwargs["socket_timeout"], kwargs["socket_connect_timeout"]) == (
            REPLY_TIMEOUT_SECONDS,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        assert client.socket_timeout == REPLY_TIMEOUT_SECONDS
        async with client.request_deadline() as deadline:
            remaining = (deadline.when() or 0.0) - asyncio.get_running_loop().time()
        assert DEFAULT_REQUEST_TIMEOUT_SECONDS - 0.5 < remaining <= DEFAULT_REQUEST_TIMEOUT_SECONDS

    async def test_the_pool_never_refuses_a_connection(self) -> None:
        """Blocked stream reads hold pooled connections while they wait; a bounded pool would refuse the request path."""
        client = RedisClient("redis://redis.invalid:6379/0")
        await client.connect()
        pool = client.client.connection_pool
        await client.close()

        assert POOL_MAX_CONNECTIONS == 2**31
        assert pool.max_connections == POOL_MAX_CONNECTIONS

    async def test_a_command_cut_by_a_dropped_connection_is_not_sent_again(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Its reply was lost, so it may have been applied: only its caller may send it again (a batch, safely)."""
        client = RedisClient(test_redis_url)
        await client.connect()
        try:
            assert client.client.connection_pool.connection_kwargs["retry"].get_retries() == 0
            victim = await client.client.client_id()
            waiting = asyncio.create_task(client.client.blpop([KEY_PREFIX + "test:nothing"], timeout=3))
            await asyncio.sleep(0.2)
            await redis_client.client.client_kill_filter(_id=victim)
            started = time.monotonic()
            with pytest.raises(redis.exceptions.ConnectionError):
                await waiting
            # Sent again on a new connection, the BLPOP would have waited out its three seconds.
            assert time.monotonic() - started < 1
        finally:
            await client.close()

    async def test_a_new_connection_sends_nothing_redis_refuses(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """On RESP3 redis-py 8 asks each new connection for maintenance notifications, which open-source Redis refuses.

        Counted on the server, so it relies on the documented minimum, Redis
        7.2 (the suite's redis:7-alpine): an older server also refuses the
        CLIENT SETINFO that redis-py sends on every connection.
        """
        refused = (await redis_client.client.info("stats"))["total_error_replies"]
        client = RedisClient(test_redis_url)
        await client.connect()
        try:
            # Three calls at once on an empty pool: three connections open.
            assert await asyncio.gather(*(client.client.ping() for _ in range(3))) == [True] * 3
        finally:
            await client.close()

        assert (await redis_client.client.info("stats"))["total_error_replies"] == refused

    async def test_a_burst_wider_than_redis_py_s_default_pool_is_never_refused(self, redis_client: RedisClient) -> None:
        """redis-py 8 caps a pool at 100 connections and fails the next concurrent call at once."""
        await redis_client.client.set("burst", "v")

        replies = await asyncio.gather(*(redis_client.client.get("burst") for _ in range(150)), return_exceptions=True)

        assert replies == ["v"] * 150

    async def test_a_reply_slower_than_the_reply_bound_fails_the_command(self, test_redis_url: str) -> None:
        """The backstop for a dead connection: no command waits on one until TCP gives up."""
        client = RedisClient(test_redis_url, socket_timeout=0.2)
        await client.connect()
        try:
            with pytest.raises(redis.exceptions.TimeoutError):
                await client.client.blpop(["nothing:queued"], timeout=2)
        finally:
            await client.close()


class TestDeadlines:
    """The request path takes a short deadline from the client; nothing else waits on it."""

    async def test_a_call_under_the_request_deadline_gives_up_on_a_paused_redis(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """And the next call on the same client reads its own reply, not the late one of the call cut short."""
        await redis_client.client.set(KEY_PREFIX + "test:a", "a")
        await redis_client.client.set(KEY_PREFIX + "test:b", "b")
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        try:
            await client.client.ping()
            await redis_client.client.client_pause(800, all=True)
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                async with client.request_deadline():
                    await client.client.get(KEY_PREFIX + "test:a")
            assert time.monotonic() - started < 0.6

            assert await client.client.get(KEY_PREFIX + "test:b") == "b"
        finally:
            await client.close()

    async def test_the_request_path_methods_take_the_deadline_themselves(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Sticky bindings, MITM captures and fake IPs: their callers fail open on any error, a deadline's included."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        calls: list[tuple[str, Any]] = [
            ("get_sticky_binding", lambda: client.get_sticky_binding("proj", "sess")),
            ("set_sticky_binding", lambda: client.set_sticky_binding("proj", "sess", "p1")),
            ("record_mitm_request", lambda: client.record_mitm_request("proj", {"url": "https://example.com/"})),
            ("fakeip_offset_for", lambda: client.fakeip_offset_for("example.com")),
            ("fakeip_name_for", lambda: client.fakeip_name_for(7)),
            ("fakeip_allocate", lambda: client.fakeip_allocate("example.com", 256)),
        ]
        try:
            await redis_client.client.client_pause(2500, all=True)
            for name, call in calls:
                started = time.monotonic()
                with pytest.raises(TimeoutError):
                    await call()
                assert time.monotonic() - started < 0.6, name
        finally:
            await client.close()

    async def test_a_connect_to_a_closed_port_fails_at_once(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = RedisClient(f"redis://127.0.0.1:{port}/0")
        await client.connect()
        try:
            started = time.monotonic()
            with pytest.raises(redis.exceptions.ConnectionError):
                await client.client.ping()
            assert time.monotonic() - started < DEFAULT_REQUEST_TIMEOUT_SECONDS
        finally:
            await client.close()


def _failed_steps(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [log for log in logs if log["event"] == "Redis step after a write failed"]


class TestAfterCommit:
    """A Redis step after a committed write never fails the write: it waits its deadline at most, and a failure is logged."""

    async def test_a_step_that_works_is_true(self, redis_client: RedisClient) -> None:
        key = KEY_PREFIX + "test:k"

        assert await redis_client.after_commit(lambda: redis_client.client.set(key, "v"), "write a key") is True

        assert await redis_client.client.get(key) == "v"

    async def test_a_step_that_fails_is_false_and_logged_with_its_context(self, redis_client: RedisClient) -> None:
        async def refused() -> None:
            raise redis.exceptions.ConnectionError("Redis is down")

        with structlog.testing.capture_logs() as logs:
            assert await redis_client.after_commit(refused, "delete a key", project_id="p") is False

        assert [(f["step"], f["error"], f["project_id"], f["log_level"]) for f in _failed_steps(logs)] == [
            ("delete a key", "Redis is down", "p", "warning")
        ]

    async def test_a_step_redis_does_not_answer_is_given_up_at_the_deadline(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Not at the 30 s reply bound: the deadline cuts it, and the log names the deadline's error."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        try:
            await client.client.ping()
            await redis_client.client.client_pause(800, all=True)
            started = time.monotonic()
            with structlog.testing.capture_logs() as logs:
                done = await client.after_commit(lambda: client.client.delete(KEY_PREFIX + "test:k"), "delete a key")
            assert time.monotonic() - started < 0.6
        finally:
            await client.close()

        assert done is False
        assert [f["error"] for f in _failed_steps(logs)] == ["TimeoutError"]

    async def test_a_cancelled_caller_is_cancelled_not_answered(self, redis_client: RedisClient) -> None:
        """A shutdown cancelling the write's task must end it, not see a step that failed."""
        started = asyncio.Event()

        async def slow() -> None:
            started.set()
            await asyncio.sleep(10)

        step = asyncio.create_task(redis_client.after_commit(slow, "a slow step"))
        await started.wait()
        step.cancel()

        with structlog.testing.capture_logs() as logs, pytest.raises(asyncio.CancelledError):
            await step
        assert _failed_steps(logs) == []


class _Placeholders(dict[str, str]):
    """Fills every placeholder of a key format, naming a real protocol where a key starts with one."""

    def __missing__(self, placeholder: str) -> str:
        return "wireguard" if placeholder == "protocol" else "x"


class TestKeyGroups:
    def test_every_key_in_the_catalogue_has_a_group(self) -> None:
        """Memory held under a key nobody grouped shows up as "Other" on the System page."""
        formats = {name: value for name, value in vars(catalogue).items() if name.endswith("_KEY")}

        assert formats
        for name, key_format in formats.items():
            group = classify_key(key_format.format_map(_Placeholders()))
            assert group not in (OTHER_KEY_GROUP, EARLIER_RELEASES_KEY_GROUP), name

    @pytest.mark.parametrize(
        ("key", "group"),
        [
            (FEED_HEAD_KEY, "Routing feed"),
            (FEED_MANIFEST_KEY, "Routing feed"),
            (FEED_SECTIONS_KEY, "Routing feed"),
            (JOB_STREAM_KEY.format(kind="health"), "Job streams"),
            (JOB_STREAM_KEY.format(kind="metrics_flush"), "Job streams"),
            (TRAFFIC_BLOCKED_KEY, "Traffic blocks"),
            (TRAFFIC_PENDING_KEY, "Traffic blocks"),
            (SCALE_CREATING_KEY.format(connector_id="c1"), "Scale"),
            (METRICS_TAKEN_KEY.format(message_id="1760788800000-0"), "Metrics staging"),
            # The ingest streams, which the event streams' row would take too.
            (SIGHTING_STREAM_KEY, "Ingest streams"),
            (OBSERVATION_STREAM_KEY, "Ingest streams"),
            (DEMAND_KEY.format(project_id="p1", minute=29433600), "Demand"),
            (AUTOSCALER_STATE_KEY.format(connector_id="c1"), "Auto-scaler state"),
            (PROXY_HEALTH_KEY.format(proxy_id="p1"), "Proxy health"),
            (PROXY_LAST_EXIT_KEY.format(proxy_id="p1"), "IP attribution"),
            (QUARANTINE_KEY, "Quarantine"),
            (CHANGES_STREAM_KEY, "Event streams"),
            (RUNTIME_STREAM_KEY, "Event streams"),
            (FEED_STREAM_KEY, "Event streams"),
            (APPLIED_BATCHES_KEY.format(writer="process:ab12cd34"), "Batch marks"),
            (FAILURE_LOG_APPENDED_KEY.format(project_id="p1"), "Failure log"),
            (TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol="openvpn"), "OpenVPN"),
        ],
    )
    def test_the_role_split_keys_have_their_groups(self, key: str, group: str) -> None:
        assert classify_key(key) == group

    @pytest.mark.parametrize(
        "key",
        ["proxy:metrics:p1", "feed:head", "jobs:health", "queue:sightings", "lease:controller", "instance_index"],
    )
    def test_a_key_without_the_prefix_is_an_earlier_release_s(self, key: str) -> None:
        """5.x writes these layouts unprefixed; after the switch they are leftovers to delete."""
        assert classify_key(key) == EARLIER_RELEASES_KEY_GROUP

    def test_a_key_of_this_release_no_group_names_is_other(self) -> None:
        assert classify_key(KEY_PREFIX + "unheard:of") == OTHER_KEY_GROUP


class TestKeyPrefix:
    def test_every_key_format_and_prefix_starts_with_the_prefix(self) -> None:
        """5.x and this release meet on one Redis across the switch and its way back: no key may be shared."""
        constants = {
            name: value
            for name, value in vars(catalogue).items()
            if name.endswith(("_KEY", "_PREFIX")) and name != "KEY_PREFIX" and isinstance(value, str)
        }

        assert {"PROXY_METRICS_KEY", "HOST_METRICS_PREFIX", "CHANGES_STREAM_KEY", "JOB_STREAM_KEY"} <= set(constants)
        for name, value in constants.items():
            assert value.startswith(KEY_PREFIX), name

    def test_the_tables_built_from_the_formats_carry_it_too(self) -> None:
        for kind in METRIC_WINDOW_KINDS:
            assert kind.prefix.startswith(KEY_PREFIX) and kind.index.startswith(KEY_PREFIX), kind.name
        for prefix, label in REDIS_KEY_GROUPS:
            assert prefix.startswith(KEY_PREFIX), label

    async def test_a_tunnel_device_s_id_is_read_back_whole(self, redis_client: RedisClient) -> None:
        """The id follows a prefix with colons of its own: it is cut off the format, not split out."""
        await _meter(redis_client, MetricWindows(tunnel_peers={"dev:1": DEVICE}))

        assert set((await redis_client.take_metric_windows("check")).tunnel_peers) == {"dev:1"}


class TestWindowKeys:
    def test_a_host_window_s_key_reads_back_whole(self) -> None:
        """The host is the key's last segment, so an IPv6 literal keeps its colons."""
        assert HOST_WINDOWS.entity(HOST_WINDOWS.key(HOST)) == HOST

    @pytest.mark.parametrize(
        "key",
        [
            "o6:proxy:metrics:p1",
            "host:metrics:proj:conn:example.com",  # a 5.x key
            "o6:host:metrics:proj:conn",
            "o6:host:metrics:proj::example.com",
        ],
    )
    def test_a_key_that_is_no_host_window_reads_as_none(self, key: str) -> None:
        assert HOST_WINDOWS.entity(key) is None


async def _outcomes(redis_client: RedisClient, parts: list[BatchPart]) -> list[BatchOutcome]:
    return [reply.outcome for reply in await redis_client.send_batch(parts)]


async def _stream_items(redis_client: RedisClient, key: str) -> list[str]:
    """The items of a work stream's entries, oldest first; each entry must hold its item alone."""
    entries = await redis_client.client.xrange(key)
    assert all(set(fields) == {"item"} for _id, fields in entries)
    return [fields["item"] for _id, fields in entries]


class TestBatchIdentity:
    def test_a_writer_is_its_process_s_name_and_a_token_drawn_at_start(self) -> None:
        """A restarted process keeps its instance id, and must not take over its predecessor's marks."""
        first = RedisClient("redis://redis.invalid", name="gateway-1")
        second = RedisClient("redis://redis.invalid", name="gateway-1")

        assert first.writer_id.startswith("gateway-1:") and second.writer_id.startswith("gateway-1:")
        assert first.writer_id != second.writer_id

    def test_each_channel_counts_its_own_sequence_from_one(self) -> None:
        client = RedisClient("redis://redis.invalid")

        parts = [
            *client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, ["a"]),
            *client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, ["b"]),
            *client.append_batch(BatchChannel.OBSERVATION_APPENDS, OBSERVATION_STREAM_KEY, ["c"]),
        ]

        assert [part.id for part in parts] == [
            BatchId(BatchChannel.SIGHTING_APPENDS, 1),
            BatchId(BatchChannel.SIGHTING_APPENDS, 2),
            BatchId(BatchChannel.OBSERVATION_APPENDS, 1),
        ]

    def test_a_renumbered_part_is_the_same_write_under_the_next_sequence(self) -> None:
        client = RedisClient("redis://redis.invalid")
        (part,) = client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, ["a", "b"])

        again = client.renumber(part)

        assert again.id == BatchId(BatchChannel.SIGHTING_APPENDS, 2)
        assert (again.keys, again.args, again.items) == (part.keys, part.args, part.items)

    async def test_the_mark_outlives_its_writer_by_a_day(self, redis_client: RedisClient) -> None:
        await redis_client.send_batch(redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(1)})))
        mark = APPLIED_BATCHES_KEY.format(writer=redis_client.writer_id)

        assert await redis_client.client.hgetall(mark) == {"metering": "1"}
        assert APPLIED_BATCHES_TTL_SECONDS - 10 < await redis_client.client.ttl(mark) <= APPLIED_BATCHES_TTL_SECONDS


class TestMeteringBatch:
    async def test_a_window_lands_in_its_windows_indexes_and_demand(self, redis_client: RedisClient) -> None:
        """Each delta adds to its entity's window, listed in its kind's index, and each demand count to its minute."""
        device = TunnelPeerMetricDelta()
        device.add_request(True, 10.0)
        device.by_address = 2
        host = ("proj", "conn", "example.com")

        parts = redis_client.metering_batch(
            MetricWindows(
                proxies={"p1": _requests(2, sent=1000)},
                projects={"proj": _requests(2)},
                connectors={"conn": _requests(1)},
                tunnel_peers={"dev": device},
                hosts={host: _requests(1)},
            ),
            demand={("proj", 29433600, "r"): 3, ("proj", 29433600, "x"): 0},
            demand_ttl_seconds=600,
        )

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert {kind.name: await redis_client.client.smembers(kind.index) for kind in METRIC_WINDOW_KINDS} == {
            "proxy": {PROXY_METRICS_KEY.format(proxy_id="p1")},
            "project": {PROJECT_METRICS_KEY.format(project_id="proj")},
            "connector": {CONNECTOR_METRICS_KEY.format(connector_id="conn")},
            "tunnel_peer": {TUNNEL_PEER_METRICS_KEY.format(peer_id="dev")},
            "host": {HOST_METRICS_KEY.format(project_id="proj", connector_id="conn", host="example.com")},
            "failure": set(),
        }
        demand = DEMAND_KEY.format(project_id="proj", minute=29433600)
        assert await redis_client.client.hgetall(demand) == {"r": "3"}
        assert 590 < await redis_client.client.ttl(demand) <= 600
        assert await redis_client.take_metric_windows("check") == MetricWindows(
            proxies={"p1": _requests(2, sent=1000)},
            projects={"proj": _requests(2)},
            connectors={"conn": _requests(1)},
            tunnel_peers={"dev": device},
            hosts={host: _requests(1)},
        )

    async def test_windows_add_up_in_the_live_ones(self, redis_client: RedisClient) -> None:
        """Each window adds to its entity's: the counts, the latency sum (a fraction too) and the bytes."""
        failed = MetricDelta()
        failed.add_request(False, 50.5, 500, 0)
        for window in (_requests(2, sent=1000), failed):
            parts = redis_client.metering_batch(MetricWindows(proxies={"p1": window}, projects={"proj": window}))
            assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]

        taken = await redis_client.take_metric_windows("check")

        for window in (taken.proxies["p1"], taken.projects["proj"]):
            assert (window.request_count, window.success_count, window.failure_count) == (3, 2, 1)
            assert window.latency_sum_ms == 250.5
            assert window.avg_latency_ms == 83.5
            assert (window.bytes_sent, window.bytes_received) == (2500, 4000)

    async def test_a_part_sent_again_after_it_was_applied_changes_nothing(self, redis_client: RedisClient) -> None:
        """What a lost reply leads to: the writer sends the part again, and Redis skips it."""
        parts = redis_client.metering_batch(
            MetricWindows(proxies={"p1": _requests(3)}, projects={"proj": _requests(3)}),
            demand={("proj", 1, "r"): 3},
            demand_ttl_seconds=600,
        )

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert await _outcomes(redis_client, parts) == [BatchOutcome.REPEATED]

        windows = await redis_client.take_metric_windows("check")
        assert (windows.proxies["p1"].request_count, windows.projects["proj"].request_count) == (3, 3)
        assert await redis_client.client.hget(DEMAND_KEY.format(project_id="proj", minute=1), "r") == "3"

    async def test_a_later_sequence_applies_after_one_never_sent(self, redis_client: RedisClient) -> None:
        """The mark says what was applied, so a sequence skipped over holds nothing up."""
        redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(1)}))
        later = redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(2)}))

        assert later[0].id.sequence == 2
        assert await _outcomes(redis_client, later) == [BatchOutcome.APPLIED]
        assert (await redis_client.take_metric_windows("check")).proxies["p1"].request_count == 2

    async def test_a_large_window_is_cut_into_parts_the_first_carrying_every_connector(
        self, redis_client: RedisClient
    ) -> None:
        proxies = {f"p{i}": _requests(1) for i in range(1200)}
        connectors = {f"c{i}": _requests(1) for i in range(3)}

        parts = redis_client.metering_batch(
            MetricWindows(proxies=proxies, connectors=connectors),
            demand={("proj", 1, "r"): 1200},
            demand_ttl_seconds=600,
            metered={"c0", "c2"},
        )

        assert [part.items for part in parts] == [501, 500, 203]
        assert {CONNECTOR_METRICS_KEY.format(connector_id=f"c{i}") for i in range(3)} <= set(parts[0].keys)
        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED] * 3
        assert len((await redis_client.take_metric_windows("check")).proxies) == 1200
        # Asked for once, by the part that carried the connectors' bytes.
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["c0", "c2"]

    def test_more_connectors_than_a_part_holds_all_go_in_the_first(self, redis_client: RedisClient) -> None:
        """Every traffic decision a window asks for comes after all of its connectors' bytes, however many there are."""
        connectors = {f"c{i}": _requests(1) for i in range(600)}

        parts = redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(1)}, connectors=connectors), metered=connectors)

        assert [part.items for part in parts] == [600, 1]
        assert {CONNECTOR_METRICS_KEY.format(connector_id=c) for c in connectors} <= set(parts[0].keys)

    def test_demand_needs_its_expiry_and_an_empty_window_is_no_part(self, redis_client: RedisClient) -> None:
        with pytest.raises(ValueError):
            redis_client.metering_batch(MetricWindows(), demand={("proj", 1, "r"): 1})
        assert redis_client.metering_batch(MetricWindows(), demand={("proj", 1, "r"): 0}, demand_ttl_seconds=60) == []

    async def test_a_script_that_failed_after_its_mark_is_never_applied_twice(self, redis_client: RedisClient) -> None:
        """Never twice over never lost: what it wrote before failing stays written once, and the rest is dropped."""
        await redis_client.client.set(PROXY_METRICS_KEY.format(proxy_id="broken"), "not a hash")
        parts = redis_client.metering_batch(MetricWindows(proxies={"ok": _requests(1), "broken": _requests(1)}))

        with patch("api.db.redis.logger") as log:
            assert await _outcomes(redis_client, parts) == [BatchOutcome.FAILED]
        assert await _outcomes(redis_client, parts) == [BatchOutcome.REPEATED]

        assert log.error.call_args.args == ("Redis batch dropped: its script failed",)
        assert await redis_client.client.hget(PROXY_METRICS_KEY.format(proxy_id="ok"), "request_count") == "1"


class TestTrafficRequests:
    """A metering part asks the workers for a traffic decision on each limited connector whose bytes it carries."""

    async def test_a_limited_connector_s_bytes_give_one_traffic_entry_per_part(self, redis_client: RedisClient) -> None:
        for _ in range(2):
            parts = redis_client.metering_batch(MetricWindows(connectors={"limited": _requests(1)}), metered=["limited"])
            assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]

        entries = await redis_client.client.xrange(JOB_STREAM_KEY.format(kind=JobKind.TRAFFIC))
        # A job entry like any other, so a worker's traffic runner takes it.
        assert [fields for _id, fields in entries] == [{"key": "limited"}, {"key": "limited"}]
        # Asked for in the script that added the bytes: no decision reads the usage without them.
        window = (await redis_client.take_metric_windows("check")).connectors["limited"]
        assert (window.bytes_sent, window.bytes_received) == (20, 40)

    async def test_a_part_sent_again_asks_for_nothing_more(self, redis_client: RedisClient) -> None:
        """A lost reply: the part goes again as it was, and Redis skips it, its entries included."""
        parts = redis_client.metering_batch(MetricWindows(connectors={"limited": _requests(1)}), metered=["limited"])

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert await _outcomes(redis_client, parts) == [BatchOutcome.REPEATED]

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["limited"]

    async def test_a_connector_without_a_limit_or_without_bytes_asks_for_nothing(self, redis_client: RedisClient) -> None:
        refused = MetricDelta()
        refused.add_request(False, 5.0)
        parts = redis_client.metering_batch(
            MetricWindows(connectors={"free": _requests(1), "limited": refused}), metered=["limited", "elsewhere"]
        )

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []
        assert set((await redis_client.take_metric_windows("check")).connectors) == {"free", "limited"}

    async def test_the_traffic_stream_is_capped_as_every_job_stream(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kind no worker takes cannot fill Redis: each append trims the stream to about its cap."""
        stream = JOB_STREAM_KEY.format(kind=JobKind.TRAFFIC)
        pipe = redis_client.client.pipeline(transaction=False)
        for n in range(1000):
            pipe.xadd(stream, {"key": f"old-{n}"})
        await pipe.execute()
        monkeypatch.setattr(catalogue, "JOB_STREAM_MAXLEN", 10)

        parts = redis_client.metering_batch(MetricWindows(connectors={"limited": _requests(1)}), metered=["limited"])
        await redis_client.send_batch(parts)

        # Approximate: Redis drops whole nodes of a hundred entries, newest kept.
        assert await redis_client.client.xlen(stream) <= 110
        assert (await queued_job_keys(redis_client, JobKind.TRAFFIC))[-1] == "limited"


# Holds Redis in a script for ARGV[1] seconds, as another client's script
# overrunning does: past busy-reply-threshold Redis answers BUSY to the rest.
_BUSY_SCRIPT = """
local start = redis.call('TIME')
local began = tonumber(start[1]) + tonumber(start[2]) / 1e6
while true do
  local now = redis.call('TIME')
  if tonumber(now[1]) + tonumber(now[2]) / 1e6 - began > tonumber(ARGV[1]) then
    return 1
  end
end
"""


async def _hold_redis(redis_url: str, seconds: float) -> None:
    other = RedisClient(redis_url)
    await other.connect()
    try:
        await other.client.eval(_BUSY_SCRIPT, 0, str(seconds))
    finally:
        await other.close()


class TestRefusals:
    """A part Redis refused applied nothing: it goes again, under a new sequence."""

    async def test_a_part_refused_while_redis_was_busy_lands_once_renumbered(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Sent under its own sequence once a later one had run, it would be skipped as applied, and lost."""
        # The script is loaded before Redis is busy, as it is after a process's first send.
        await redis_client.send_batch(redis_client.metering_batch(MetricWindows(proxies={"warm": _requests(1)})))
        refused = redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(1)}))
        await redis_client.client.config_set("busy-reply-threshold", 50)
        busy = asyncio.create_task(_hold_redis(test_redis_url, 1.0))
        try:
            await asyncio.sleep(0.3)
            with patch("api.db.redis.logger") as log:
                assert await _outcomes(redis_client, refused) == [BatchOutcome.REFUSED]
        finally:
            # Once the script is over: Redis answers nothing else while it runs.
            await asyncio.gather(busy, return_exceptions=True)
            await redis_client.client.config_set("busy-reply-threshold", 5000)
        assert log.warning.call_args.args == ("Redis batch refused; sending its content again",)

        # A later window of the channel lands meanwhile.
        later = redis_client.metering_batch(MetricWindows(proxies={"p1": _requests(2)}))
        assert await _outcomes(redis_client, later) == [BatchOutcome.APPLIED]
        assert await _outcomes(redis_client, refused) == [BatchOutcome.REPEATED]
        assert await _outcomes(redis_client, [redis_client.renumber(refused[0])]) == [BatchOutcome.APPLIED]

        assert (await redis_client.take_metric_windows("check")).proxies["p1"].request_count == 3

    async def test_a_script_redis_forgot_is_refused_and_loaded_again(self, redis_client: RedisClient) -> None:
        """A SCRIPT FLUSH, or a restart, between the client loading its script and the call: the call never ran."""
        first = redis_client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, ["a"])
        assert await _outcomes(redis_client, first) == [BatchOutcome.APPLIED]
        await redis_client.client.script_flush()

        forgot = redis_client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, ["b"])
        assert await _outcomes(redis_client, forgot) == [BatchOutcome.REFUSED]
        assert await _outcomes(redis_client, [redis_client.renumber(forgot[0])]) == [BatchOutcome.APPLIED]

        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == ["a", "b"]

    async def test_a_send_nobody_answered_raises_and_lands_once_sent_again_unchanged(
        self, redis_client: RedisClient
    ) -> None:
        """Whether Redis ran it is unknown, so it goes again under the same identity: applied once either way."""
        key = KEY_PREFIX + "test:stream"
        await redis_client.send_batch(redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, key, ["first"]))
        parts = redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, key, ["a", "b"])
        await redis_client.client.client_pause(800, all=True)
        with patch("api.db.redis.logger") as log, pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await redis_client.send_batch(parts)
        assert log.warning.call_args.args == ("Redis batch not confirmed; sending it again",)
        await asyncio.sleep(0.8)

        assert (await redis_client.send_batch(parts))[0].outcome in (BatchOutcome.APPLIED, BatchOutcome.REPEATED)
        assert await _outcomes(redis_client, parts) == [BatchOutcome.REPEATED]
        assert await _stream_items(redis_client, key) == ["first", "a", "b"]


class TestAppendBatch:
    """The work streams' appends: an entry per item, in order, applied once whatever the resends."""

    async def test_items_land_in_order_once_whatever_the_resends(self, redis_client: RedisClient) -> None:
        items = [f"item-{i}" for i in range(2500)]
        parts = redis_client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, items)

        assert await redis_client.send_batch(parts) == [BatchReply(BatchOutcome.APPLIED, 0)]
        assert await redis_client.send_batch(parts) == [BatchReply(BatchOutcome.REPEATED, 0)]

        assert parts[0].items == 2500
        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == items

    async def test_the_cap_trims_the_oldest_and_says_how_many_went(self, redis_client: RedisClient) -> None:
        old = [f"old-{i}" for i in range(500)]
        new = [f"new-{i}" for i in range(500)]
        await redis_client.send_batch(redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, OBSERVATION_STREAM_KEY, old))

        (reply,) = await redis_client.send_batch(
            redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, OBSERVATION_STREAM_KEY, new, max_items=200)
        )

        length = await redis_client.client.xlen(OBSERVATION_STREAM_KEY)
        # Trimmed whole stream nodes (of 100 entries) at a time, so up to a node over the cap.
        assert 200 <= length < 300
        assert reply == BatchReply(BatchOutcome.APPLIED, len(old) + len(new) - length)
        assert await _stream_items(redis_client, OBSERVATION_STREAM_KEY) == (old + new)[-length:]

    def test_only_the_stream_channels_append(self) -> None:
        client = RedisClient("redis://redis.invalid")
        appending = {BatchChannel.SIGHTING_APPENDS, BatchChannel.OBSERVATION_APPENDS}

        for channel in BatchChannel:
            if channel in appending:
                assert client.append_batch(channel, SIGHTING_STREAM_KEY, ["a"])
            else:
                with pytest.raises(ValueError):
                    client.append_batch(channel, SIGHTING_STREAM_KEY, ["a"])

    def test_nothing_to_append_is_no_part(self, redis_client: RedisClient) -> None:
        assert redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, OBSERVATION_STREAM_KEY, []) == []


async def _failure_counts(redis_client: RedisClient) -> dict[FailureCountKey, int]:
    """The failure counts the live windows hold, as the next flush takes them."""
    taken = await redis_client.take_metric_windows("check")
    return {count_key: count.failure_count for count_key, count in taken.failures.items()}


class TestFailuresBatch:
    async def test_a_window_lands_its_examples_their_tallies_and_its_counts(self, redis_client: RedisClient) -> None:
        entries = [
            ("proj", "upstream_timeout", {"target": "a"}),
            ("proj", "upstream_rejected", {"target": "b"}),
            ("proj", "upstream_timeout", {"target": "c"}),
        ]
        counts = {("proj", "conn", "upstream_timeout", None): 2, ("proj", None, "upstream_rejected", 407): 1}

        parts = redis_client.failures_batch(entries, counts, max_entries=10, retention_seconds=3600)

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        kinds = ["upstream_timeout", "upstream_rejected"]
        assert [entry["target"] for entry in await redis_client.get_failures("proj", kinds)] == ["c", "b", "a"]
        assert await redis_client.failures_appended("proj") == {"upstream_timeout": 2, "upstream_rejected": 1}
        assert await _failure_counts(redis_client) == counts
        stream = FAILURE_LOG_KEY.format(project_id="proj", kind="upstream_timeout")
        for key in (stream, FAILURE_LOG_APPENDED_KEY.format(project_id="proj")):
            assert 0 < await redis_client.client.ttl(key) <= 3600

    async def test_a_window_sent_again_adds_no_example_and_no_count(self, redis_client: RedisClient) -> None:
        """What lets the failure log keep a window Redis did not answer for, rather than drop it."""
        counts = {("proj", "conn", "upstream_timeout", None): 1}
        parts = redis_client.failures_batch(
            [("proj", "upstream_timeout", {"target": "a"})], counts, max_entries=10, retention_seconds=3600
        )

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert await _outcomes(redis_client, parts) == [BatchOutcome.REPEATED]

        assert await redis_client.count_failures("proj", ["upstream_timeout"]) == 1
        assert await redis_client.failures_appended("proj") == {"upstream_timeout": 1}
        assert await _failure_counts(redis_client) == counts

    async def test_an_id_another_gateway_has_passed_still_lands(self, redis_client: RedisClient) -> None:
        key = FAILURE_LOG_KEY.format(project_id="proj", kind="upstream_timeout")
        await redis_client.client.xadd(key, {"target": "peer"}, id=f"{2**42}-0")  # a clock far ahead

        parts = redis_client.failures_batch(
            [("proj", "upstream_timeout", {"target": "ours"})], {}, max_entries=10, retention_seconds=3600
        )

        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED]
        assert {entry["target"] for entry in await redis_client.get_failures("proj", ["upstream_timeout"])} == {
            "peer",
            "ours",
        }

    async def test_a_large_window_is_cut_into_parts_and_reads_in_order(self, redis_client: RedisClient) -> None:
        entries = [("proj", "upstream_timeout", {"target": f"t{i}"}) for i in range(600)]
        counts = {("proj", f"c{i}", "upstream_timeout", None): 1 for i in range(450)}

        parts = redis_client.failures_batch(entries, counts, max_entries=1000, retention_seconds=3600)

        assert [part.items for part in parts] == [500, 500, 50]
        assert await _outcomes(redis_client, parts) == [BatchOutcome.APPLIED] * 3
        newest_first = await redis_client.get_failures("proj", ["upstream_timeout"], count=600)
        assert [entry["target"] for entry in newest_first] == [f"t{i}" for i in reversed(range(600))]
        assert len(await _failure_counts(redis_client)) == 450

    def test_an_empty_window_is_no_part(self, redis_client: RedisClient) -> None:
        assert redis_client.failures_batch([], {}, max_entries=10, retention_seconds=60) == []

    async def test_a_page_ending_on_the_larger_kind_still_reads_the_smaller_one_at_that_id(
        self, redis_client: RedisClient
    ) -> None:
        """Two kinds' streams can hand out one ID: the cursor carries the kind, so neither is skipped."""
        kinds = ["upstream_timeout", "no_proxy_available"]
        for i in range(3):
            for kind in kinds:
                key = FAILURE_LOG_KEY.format(project_id="p", kind=kind)
                await redis_client.client.xadd(key, {"target": f"{kind[0]}{i}"}, id=f"100-{i}")
        seen: list[str] = []
        after = None
        while page := await redis_client.get_failures("p", kinds, count=3, after=after):
            seen += [entry["target"] for entry in page]
            after = (page[-1]["id"], page[-1]["kind"])

        assert seen == ["u2", "n2", "u1", "n1", "u0", "n0"]


def _append(redis_client: RedisClient, *items: str) -> list[BatchPart]:
    return redis_client.append_batch(BatchChannel.SIGHTING_APPENDS, SIGHTING_STREAM_KEY, list(items))


class TestBatchSender:
    """A loop's sends: what Redis has not confirmed goes again first, and the window waits behind it."""

    async def test_a_send_cuts_the_window_and_sends_it(self, redis_client: RedisClient) -> None:
        sender = BatchSender(redis_client)

        answered = await sender.send(lambda: _append(redis_client, "a", "b"))

        assert [(part.items, reply) for part, reply in answered] == [(2, BatchReply(BatchOutcome.APPLIED, 0))]
        assert sender.owed == ()
        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == ["a", "b"]

    async def test_a_send_nobody_answered_stays_owed_unchanged_and_the_window_waits_behind_it(
        self, redis_client: RedisClient
    ) -> None:
        sender = BatchSender(redis_client)
        await sender.send(lambda: _append(redis_client, "warm"))  # the script is loaded
        unanswered = _append(redis_client, "a")
        cuts: list[int] = []

        def cut() -> list[BatchPart]:
            cuts.append(1)
            return _append(redis_client, "b")

        await redis_client.client.client_pause(1000, all=True)
        for window in (lambda: unanswered, cut):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await sender.send(window)
            assert sender.owed == tuple(unanswered)
        assert cuts == []
        await asyncio.sleep(0.8)

        answered = await sender.send(cut)

        assert [part for part, _reply in answered][0] == unanswered[0]
        assert (cuts, sender.owed) == ([1], ())
        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == ["warm", "a", "b"]

    async def test_a_refused_part_is_owed_renumbered_and_the_window_waits_until_it_lands(
        self, redis_client: RedisClient
    ) -> None:
        sender = BatchSender(redis_client)
        await sender.send(lambda: _append(redis_client, "a"))
        cuts: list[int] = []

        def cut() -> list[BatchPart]:
            cuts.append(1)
            return _append(redis_client, "c")

        await redis_client.client.script_flush()
        refused = _append(redis_client, "b")
        assert [reply.outcome for _part, reply in await sender.send(lambda: refused)] == [BatchOutcome.REFUSED]
        (owed,) = sender.owed
        assert (owed.id, owed.args) == (BatchId(BatchChannel.SIGHTING_APPENDS, refused[0].id.sequence + 1), refused[0].args)
        # Another channel of the same script loads it again, and a second flush has Redis refuse the part again.
        await redis_client.send_batch(
            redis_client.append_batch(BatchChannel.OBSERVATION_APPENDS, KEY_PREFIX + "test:stream", ["x"])
        )
        await redis_client.client.script_flush()
        assert [reply.outcome for _part, reply in await sender.send(cut)] == [BatchOutcome.REFUSED]
        assert cuts == []

        answered = await sender.send(cut)

        assert [reply.outcome for _part, reply in answered] == [BatchOutcome.APPLIED, BatchOutcome.APPLIED]
        assert cuts == [1]
        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == ["a", "b", "c"]

    async def test_each_send_is_seen_as_it_is_answered(self, redis_client: RedisClient) -> None:
        """So a writer counts the owed send even when what follows it in the same call fails."""
        seen: list[list[BatchOutcome]] = []
        sender = BatchSender(redis_client, on_answer=lambda answered: seen.append([r.outcome for _p, r in answered]))
        await sender.send(lambda: _append(redis_client, "warm"))  # the script is loaded
        await redis_client.client.script_flush()
        await sender.send(lambda: _append(redis_client, "a"))  # refused: owed, renumbered

        def cut() -> list[BatchPart]:
            raise RuntimeError("no window")

        with pytest.raises(RuntimeError):
            await sender.send(cut)

        assert seen == [[BatchOutcome.APPLIED], [BatchOutcome.REFUSED], [BatchOutcome.APPLIED]]
        assert sender.owed == ()
        assert await _stream_items(redis_client, SIGHTING_STREAM_KEY) == ["warm", "a"]


class TestStreamHead:
    async def test_a_stream_nobody_wrote_has_no_head(self, redis_client: RedisClient) -> None:
        assert await redis_client.stream_head(CHANGES_STREAM_KEY) is None

    async def test_the_head_is_the_last_id_handed_out_even_once_trimmed(self, redis_client: RedisClient) -> None:
        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": "a"}, id="1000-0")
        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": "b"}, id="2000-0")
        await redis_client.client.xtrim(FEED_STREAM_KEY, maxlen=0)

        assert await redis_client.stream_head(FEED_STREAM_KEY) == "2000-0"

    async def test_in_a_pipeline_a_missing_stream_reads_as_none_and_other_errors_raise(
        self, redis_client: RedisClient
    ) -> None:
        """How the System page reads the head beside its other replies."""
        await redis_client.client.set(KEY_PREFIX + "test:string", "x")
        pipe = redis_client.client.pipeline(transaction=False)
        pipe.xinfo_stream(RUNTIME_STREAM_KEY)
        pipe.xinfo_stream(KEY_PREFIX + "test:string")
        missing, wrong = await pipe.execute(raise_on_error=False)

        assert StreamInfo.of(missing) is None
        with pytest.raises(redis.exceptions.ResponseError):
            StreamInfo.of(wrong)

    async def test_several_streams_are_described_in_one_round_trip(self, redis_client: RedisClient) -> None:
        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": "a"}, id="1000-0")
        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": "b"}, id="2000-0")
        await redis_client.client.xtrim(FEED_STREAM_KEY, maxlen=1, approximate=False)

        feed, runtime = await redis_client.stream_infos([FEED_STREAM_KEY, RUNTIME_STREAM_KEY])

        assert feed == StreamInfo(length=1, last_id="2000-0", entries_added=2, first_id="2000-0")
        assert runtime is None


class TestStreamId:
    def test_ids_order_by_milliseconds_then_sequence(self) -> None:
        ids = ["10-2", "9-15", "10-10", "0-0"]

        assert [str(sid) for sid in sorted(StreamId.of(i) for i in ids)] == ["0-0", "9-15", "10-2", "10-10"]

    def test_the_id_before_is_an_exclusive_upper_bound(self) -> None:
        assert StreamId.of("10-3").before() == StreamId(10, 2)
        assert str(StreamId.of("10-0").before()) == "9-18446744073709551615"
        assert StreamId.of("0-0").before() is None

    def test_a_malformed_id_is_refused(self) -> None:
        with pytest.raises(ValueError):
            StreamId.of("1000")


def test_the_refusals_are_the_replies_of_a_script_that_never_ran() -> None:
    """Only these may go again under a new sequence: anything else may have written half."""
    assert {"BUSY", "NOSCRIPT", "OOM", "MISCONF"} <= BATCH_REFUSALS
    assert "WRONGTYPE" not in BATCH_REFUSALS and "ERR" not in BATCH_REFUSALS


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        (1, BatchReply(BatchOutcome.APPLIED, 1)),
        (0, BatchReply(BatchOutcome.APPLIED, 0)),
        (-1, BatchReply(BatchOutcome.REPEATED)),
        (redis.exceptions.ResponseError("BUSY Redis is busy running a script"), BatchReply(BatchOutcome.REFUSED)),
        (redis.exceptions.OutOfMemoryError("command not allowed", status_code="OOM"), BatchReply(BatchOutcome.REFUSED)),
        (redis.exceptions.NoScriptError("No matching script", status_code="NOSCRIPT"), BatchReply(BatchOutcome.REFUSED)),
        (redis.exceptions.ResponseError("WRONGTYPE Operation against a key"), BatchReply(BatchOutcome.FAILED)),
    ],
)
def test_each_script_reply_is_classed(reply: object, answer: BatchReply) -> None:
    assert BatchReply.of(reply) == answer
    assert BatchReply.of(reply).landed is (answer.outcome in (BatchOutcome.APPLIED, BatchOutcome.REPEATED))
