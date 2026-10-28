# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the runtime state reader against the real Redis test container.

Each kind of state is written the way its producer writes it: quarantines by
their end as the score, traffic blocks as their end in ISO, pending traffic
decisions as when they were asked in ISO, soft health as the model's JSON
fields, metrics windows as one hash of counters per entity.
"""

import time
from datetime import UTC, datetime

import pytest
import structlog

import api.db.redis as catalogue
from api.core.stats import MetricDelta, TunnelPeerMetricDelta
from api.db.redis import (
    CONNECTOR_METRICS_KEY,
    CONNECTOR_WINDOWS,
    PROJECT_METRICS_KEY,
    PROJECT_WINDOWS,
    PROXY_HEALTH_KEY,
    PROXY_METRICS_KEY,
    PROXY_WINDOWS,
    QUARANTINE_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
    TUNNEL_PEER_METRICS_KEY,
    TUNNEL_PEER_WINDOWS,
    EntityWindows,
    RedisClient,
)
from api.feed.state import HealthSoft, RuntimeStateReader


@pytest.fixture
def reader(redis_client: RedisClient) -> RuntimeStateReader:
    return RuntimeStateReader(redis_client)


async def write_window(redis_client: RedisClient, key: str, delta: MetricDelta) -> None:
    """A metrics hash as gateways leave it: the counters and when they last wrote."""
    await redis_client.client.hset(key, mapping={**delta.to_dict(), "updated_at": "2026-10-17T12:00:00"})


class TestQuarantined:
    async def test_active_quarantines_with_when_they_end(
        self, reader: RuntimeStateReader, redis_client: RedisClient
    ) -> None:
        now = time.time()
        # proxy-b's quarantine is over; index hygiene has not trimmed it yet.
        await redis_client.client.zadd(QUARANTINE_KEY, {"proxy-a": now + 60, "proxy-b": now - 1})

        quarantined = await reader.quarantined()

        assert list(quarantined) == ["proxy-a"]
        ends = datetime.fromtimestamp(now + 60, UTC).replace(tzinfo=None)
        assert abs((quarantined["proxy-a"] - ends).total_seconds()) < 0.001

    async def test_no_quarantines_without_the_set(self, reader: RuntimeStateReader) -> None:
        assert await reader.quarantined() == {}


class TestBlocked:
    async def test_every_block_with_when_it_ends(self, reader: RuntimeStateReader, redis_client: RedisClient) -> None:
        await redis_client.client.hset(
            TRAFFIC_BLOCKED_KEY,
            mapping={
                "connector-a": "2026-10-20T00:00:00",
                # Over already: read until the next decision removes it.
                "connector-b": "2026-10-01T00:00:00",
                # Written with an offset: read as naive UTC all the same.
                "connector-c": "2026-10-20T02:00:00+02:00",
            },
        )

        assert await reader.blocked() == {
            "connector-a": datetime(2026, 10, 20),
            "connector-b": datetime(2026, 10, 1),
            "connector-c": datetime(2026, 10, 20),
        }

    async def test_an_unreadable_entry_is_skipped_and_logged(
        self, reader: RuntimeStateReader, redis_client: RedisClient
    ) -> None:
        await redis_client.client.hset(
            TRAFFIC_BLOCKED_KEY, mapping={"connector-a": "soon", "connector-b": "2026-10-20T00:00:00"}
        )

        with structlog.testing.capture_logs() as logs:
            blocked = await reader.blocked()

        assert blocked == {"connector-b": datetime(2026, 10, 20)}
        assert [log["connector_id"] for log in logs if log["event"] == "Unreadable traffic block skipped"] == [
            "connector-a"
        ]

    async def test_no_blocks_without_the_hash(self, reader: RuntimeStateReader) -> None:
        assert await reader.blocked() == {}


class TestBlockedNow:
    async def test_the_blocks_still_in_their_period_the_end_exclusive(
        self, reader: RuntimeStateReader, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scale job and the api's counts judge a stored block as gateways do: it is over at its end."""
        now = datetime(2026, 10, 20, 12, 0)
        monkeypatch.setattr("api.feed.state.utc_now", lambda: now)
        await redis_client.client.hset(
            TRAFFIC_BLOCKED_KEY,
            mapping={
                "connector-a": "2026-11-01T00:00:00",
                # Ending at this very moment: over.
                "connector-b": now.isoformat(),
                "connector-c": "2026-10-20T12:00:00.000001",
                # Over an hour ago, and not decided again yet.
                "connector-d": "2026-10-20T11:00:00",
                "connector-e": "soon",
            },
        )

        assert await reader.blocked_now() == {"connector-a", "connector-c"}

    async def test_none_without_the_hash(self, reader: RuntimeStateReader) -> None:
        assert await reader.blocked_now() == set()


class TestPending:
    async def test_every_connector_waiting_for_a_decision_with_when_it_was_asked_as_stored(
        self, reader: RuntimeStateReader, redis_client: RedisClient
    ) -> None:
        """The traffic job compares the stored text to settle a request, so it comes back as written."""
        asked = {"connector-a": "2026-10-20T09:15:02.123456", "connector-b": "2026-10-20T09:16:00"}
        await redis_client.client.hset(TRAFFIC_PENDING_KEY, mapping=asked)

        assert await reader.pending() == asked

    async def test_none_without_the_hash(self, reader: RuntimeStateReader) -> None:
        assert await reader.pending() == {}


class TestHealth:
    async def test_soft_health_as_the_health_job_writes_it(
        self, reader: RuntimeStateReader, redis_client: RedisClient
    ) -> None:
        soft = HealthSoft(failures=2, latency_ms=153.5, checked_at=datetime(2026, 10, 17, 12, 0, 1))
        key = PROXY_HEALTH_KEY.format(proxy_id="proxy-a")
        await redis_client.client.hset(key, mapping=soft.model_dump(mode="json"))

        # proxy-b was never checked, or not within the hash's expiry.
        assert await reader.health(["proxy-a", "proxy-b"]) == {"proxy-a": soft}

    async def test_an_unreadable_hash_is_skipped(self, reader: RuntimeStateReader, redis_client: RedisClient) -> None:
        await redis_client.client.hset(PROXY_HEALTH_KEY.format(proxy_id="proxy-a"), mapping={"failures": "many"})

        assert await reader.health(["proxy-a"]) == {}

    async def test_a_large_pool_is_read_in_batches(
        self, reader: RuntimeStateReader, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(catalogue, "_HASH_BATCH", 2)
        checked_at = datetime(2026, 10, 17, 12, 0, 1)
        ids = [f"proxy-{n}" for n in range(5)]
        for n, proxy_id in enumerate(ids):
            soft = HealthSoft(failures=n, checked_at=checked_at)
            key = PROXY_HEALTH_KEY.format(proxy_id=proxy_id)
            await redis_client.client.hset(key, mapping=soft.model_dump(mode="json"))

        health = await reader.health(ids)

        assert {proxy_id: soft.failures for proxy_id, soft in health.items()} == {f"proxy-{n}": n for n in range(5)}


class TestWindows:
    @pytest.mark.parametrize(
        ("kind", "key"),
        [
            (PROXY_WINDOWS, PROXY_METRICS_KEY.format(proxy_id="entity-1")),
            (PROJECT_WINDOWS, PROJECT_METRICS_KEY.format(project_id="entity-1")),
            (CONNECTOR_WINDOWS, CONNECTOR_METRICS_KEY.format(connector_id="entity-1")),
        ],
        ids=["proxy", "project", "connector"],
    )
    async def test_each_kind_reads_its_own_hashes(
        self, reader: RuntimeStateReader, redis_client: RedisClient, kind: EntityWindows, key: str
    ) -> None:
        delta = kind.counts(
            request_count=4, success_count=3, failure_count=1, latency_sum_ms=410.5, bytes_sent=50, bytes_received=600
        )
        await write_window(redis_client, key, delta)

        # entity-2 has no window: nothing since the last flush.
        assert await reader.windows(kind, ["entity-1", "entity-2"]) == {"entity-1": delta}

    async def test_a_tunnel_device_window_carries_its_name_resolution_counters(
        self, reader: RuntimeStateReader, redis_client: RedisClient
    ) -> None:
        delta = TunnelPeerMetricDelta(request_count=3, success_count=3, by_address=2, encrypted_dns_blocked=1)
        await write_window(redis_client, TUNNEL_PEER_METRICS_KEY.format(peer_id="device-1"), delta)

        windows = await reader.windows(TUNNEL_PEER_WINDOWS, ["device-1"])

        assert windows == {"device-1": delta}
        assert isinstance(windows["device-1"], TunnelPeerMetricDelta)

    async def test_an_unreadable_window_is_skipped(self, reader: RuntimeStateReader, redis_client: RedisClient) -> None:
        await redis_client.client.hset(PROXY_METRICS_KEY.format(proxy_id="proxy-a"), mapping={"request_count": "lots"})

        assert await reader.windows(PROXY_WINDOWS, ["proxy-a"]) == {}
