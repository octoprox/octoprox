# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the TrafficLimiter and the TrafficMeter.

The limiter is driven directly, with a dict standing in for the manager's
connector cache, a recording sink for the metric deltas, and a loader whose
totals the test controls. Redis is the real test container so the block
key and its expiry are exercised.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from api.core import traffic_limiter as tl
from api.core.signals import connector_traffic_changed
from api.core.stats import MetricDelta
from api.core.traffic_limiter import PROGRESS_REPORT_BYTES, TrafficLimiter
from api.db.redis import CONNECTOR_TRAFFIC_BLOCKED_KEY, RedisClient
from api.models.connector import Connector, CredentialType

MB = 1_000_000


def make_connector(traffic_config: dict[str, Any] | None = None, name: str = "vendor") -> Connector:
    return Connector(
        name=name,
        credential_id="cred",
        credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id="project",
        traffic_config=traffic_config or {},
    )


class Harness:
    """A limiter with its collaborators under test control."""

    def __init__(self, redis_client: RedisClient) -> None:
        self.connectors: dict[str, Connector] = {}
        self.progress: list[tuple[str, str, str, int, int]] = []
        self.totals: dict[str, tuple[int, int]] = {}
        self.loads: list[dict[str, datetime]] = []
        self.events: list[tuple[str, str]] = []
        self.limiter = TrafficLimiter(
            redis_client,
            get_connector=self.connectors.get,
            sink=lambda *args: self.progress.append(args),
            loader=self._load,
        )
        connector_traffic_changed.connect(self._on_event)

    async def _load(self, windows: dict[str, datetime]) -> dict[str, tuple[int, int]]:
        self.loads.append(dict(windows))
        return {cid: self.totals[cid] for cid in windows if cid in self.totals}

    async def _on_event(self, sender: Any, **kwargs: Any) -> None:
        self.events.append((kwargs["entity_id"], kwargs["op"]))

    def add(self, connector: Connector) -> Connector:
        self.connectors[connector.id] = connector
        return connector

    def close(self) -> None:
        connector_traffic_changed.disconnect(self._on_event)


@pytest.fixture
async def harness(redis_client: RedisClient) -> AsyncIterator[Harness]:
    h = Harness(redis_client)
    yield h
    h.close()


class TestTrafficMeter:
    async def test_reports_after_a_megabyte(self, harness: Harness) -> None:
        connector = harness.add(make_connector())
        meter = harness.limiter.meter("proxy", "project", connector.id)
        assert meter.add_sent(PROGRESS_REPORT_BYTES - 1) is True
        assert harness.progress == []
        meter.add_received(1)
        assert harness.progress == [("proxy", "project", connector.id, PROGRESS_REPORT_BYTES - 1, 1)]
        # The reported bytes are counted once: finish returns only what came after.
        meter.add_received(10)
        assert meter.finish() == (0, 10)
        assert meter.finish() == (0, 0)

    async def test_reports_after_the_time_bound(self, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tl, "PROGRESS_REPORT_SECONDS", 0.0)
        connector = harness.add(make_connector())
        meter = harness.limiter.meter("proxy", "project", connector.id)
        meter.add_sent(5)
        assert harness.progress == [("proxy", "project", connector.id, 5, 0)]

    async def test_progress_counts_against_the_limit(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 10 * MB, "action": "block"}))
        meter = harness.limiter.meter("proxy", "project", connector.id)
        assert meter.add_received(PROGRESS_REPORT_BYTES) is True
        usage = harness.limiter.usage(connector)
        assert usage.total_bytes == PROGRESS_REPORT_BYTES
        assert usage.bytes_received == PROGRESS_REPORT_BYTES

    async def test_interrupt_stops_the_transfer(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 2 * PROGRESS_REPORT_BYTES, "action": "interrupt"}))
        meter = harness.limiter.meter("proxy", "project", connector.id)
        assert meter.add_received(PROGRESS_REPORT_BYTES) is True
        meter.add_received(PROGRESS_REPORT_BYTES)
        # The block is applied off the transfer loop; give the task a tick.
        await asyncio.sleep(0.05)
        assert harness.limiter.is_interrupted(connector.id) is True
        assert meter.allowed is False
        assert meter.add_received(1) is False
        assert meter.limit_status == 509

    async def test_block_action_does_not_interrupt(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 1, "action": "block"}))
        meter = harness.limiter.meter("proxy", "project", connector.id)
        meter.add_received(PROGRESS_REPORT_BYTES)
        await asyncio.sleep(0.05)
        assert harness.limiter.is_blocked(connector.id) is True
        assert meter.allowed is True


class TestRecordAndEvaluate:
    async def test_alert_flags_without_blocking(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100}))
        assert harness.limiter.record(connector.id, 60, 50) is True
        await harness.limiter.evaluate(connector.id)
        usage = harness.limiter.usage(connector)
        assert usage.status == "exceeded"
        assert usage.blocked is False
        assert usage.percent == 110.0
        assert harness.limiter.is_blocked(connector.id) is False
        assert harness.events == []

    async def test_block_sets_redis_key_and_signal(self, harness: Harness, redis_client: RedisClient) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        assert harness.limiter.record(connector.id, 30, 30) is True  # first sight of the window
        await harness.limiter.evaluate(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False
        assert harness.limiter.record(connector.id, 10, 5) is False  # 75: under the warning line
        assert harness.limiter.record(connector.id, 30, 0) is True  # 105: over the limit
        await harness.limiter.evaluate(connector.id)
        assert harness.limiter.is_blocked(connector.id) is True
        assert harness.limiter.blocked_count == 1
        key = CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=connector.id)
        ttl = await redis_client.client.ttl(key)
        assert ttl > 0
        until = float(await redis_client.client.get(key))
        period_end = harness.limiter.usage(connector).period_end
        assert until == pytest.approx(tl._epoch(period_end))
        assert harness.events == [(connector.id, "blocked")]
        # Evaluating again changes nothing.
        await harness.limiter.evaluate(connector.id)
        assert harness.events == [(connector.id, "blocked")]

    async def test_warning_threshold(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "warn_percent": 50}))
        harness.limiter.record(connector.id, 0, 0)  # first sight
        assert harness.limiter.record(connector.id, 49, 0) is False
        assert harness.limiter.usage(connector).status == "ok"
        assert harness.limiter.record(connector.id, 1, 0) is True
        await harness.limiter.evaluate(connector.id)
        assert harness.limiter.usage(connector).status == "warning"
        assert harness.limiter.is_blocked(connector.id) is False

    async def test_no_limit_never_needs_evaluation(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"price_per_gb": 3}))
        assert harness.limiter.record(connector.id, 10, 10) is True  # first sight of the window
        assert harness.limiter.record(connector.id, 10 ** 12, 0) is False
        usage = harness.limiter.usage(connector)
        assert usage.limit_bytes is None
        assert usage.percent is None
        assert usage.status == "ok"
        assert usage.cost == pytest.approx((10 ** 12 + 20) / 1e9 * 3, rel=1e-6)
        assert usage.currency == "USD"

    async def test_mark_flushed_moves_bytes_without_changing_the_total(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 1000}))
        harness.limiter.record(connector.id, 100, 200)
        harness.limiter.mark_flushed({connector.id: MetricDelta(bytes_sent=100, bytes_received=200)})
        state = harness.limiter._states[connector.id]
        assert (state.known_sent, state.known_received) == (100, 200)
        assert (state.unflushed_sent, state.unflushed_received) == (0, 0)
        assert harness.limiter.usage(connector).total_bytes == 300

    async def test_peer_deltas_can_trigger_the_block(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.limiter.record(connector.id, 50, 0)
        await harness.limiter.evaluate(connector.id)
        await harness.limiter.apply_peer({connector.id: MetricDelta(bytes_sent=30, bytes_received=30)})
        assert harness.limiter.usage(connector).total_bytes == 110
        assert harness.limiter.is_blocked(connector.id) is True

    async def test_refresh_reads_history_and_keeps_unflushed(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 1000}))
        harness.totals[connector.id] = (400, 500)
        harness.limiter.record(connector.id, 7, 0)
        await harness.limiter.refresh([connector.id])
        assert harness.loads[-1] == {connector.id: harness.limiter.usage(connector).period_start}
        assert harness.limiter.usage(connector).total_bytes == 907

    async def test_refresh_blocks_when_history_is_over_the_limit(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.totals[connector.id] = (100, 100)
        await harness.limiter.refresh([connector.id])
        assert harness.limiter.is_blocked(connector.id) is True

    async def test_raising_the_limit_releases(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.totals[connector.id] = (100, 100)
        await harness.limiter.refresh([connector.id])
        assert harness.limiter.is_blocked(connector.id) is True
        # The manager replaces the cached object on every update; mirror that.
        raised = make_connector({"limit_bytes": 1000, "action": "block"})
        raised.id = connector.id
        harness.connectors[connector.id] = raised
        await harness.limiter.reconfigure(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False
        assert harness.events[-1] == (connector.id, "released")
        assert harness.limiter.usage(raised).total_bytes == 200

    async def test_switching_to_alert_releases(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.totals[connector.id] = (100, 100)
        await harness.limiter.refresh([connector.id])
        alert = make_connector({"limit_bytes": 100})
        alert.id = connector.id
        harness.connectors[connector.id] = alert
        await harness.limiter.reconfigure(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False
        assert harness.limiter.usage(alert).status == "exceeded"

    async def test_manual_reset_starts_the_count_over(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.totals[connector.id] = (100, 100)
        await harness.limiter.refresh([connector.id])
        assert harness.limiter.is_blocked(connector.id) is True
        connector.traffic_reset_at = tl.utc_now()
        harness.totals[connector.id] = (0, 0)  # nothing flushed since the reset
        await harness.limiter.reconfigure(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False
        usage = harness.limiter.usage(connector)
        assert usage.total_bytes == 0
        assert usage.reset_at == connector.traffic_reset_at
        assert usage.period_start == connector.traffic_reset_at

    async def test_block_expires_with_the_period(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        harness.limiter._blocked[connector.id] = time.time() - 1
        assert harness.limiter.is_blocked(connector.id) is False
        assert connector.id not in harness.limiter._blocked

    async def test_rollover_starts_a_fresh_window(self, harness: Harness) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "period": "day"}))
        harness.limiter.record(connector.id, 90, 0)
        state = harness.limiter._states[connector.id]
        # Pretend the day ended: the stored window is behind the clock.
        state.period_start -= timedelta(days=1)
        state.period_end -= timedelta(days=1)
        state.known_sent = 500
        assert harness.limiter.record(connector.id, 1, 0) is True
        fresh = harness.limiter._states[connector.id]
        assert fresh is not state
        assert fresh.known_sent == 0
        assert fresh.unflushed_sent == 91
        assert fresh.stale is True
        # Evaluate reads the new window from history.
        harness.totals[connector.id] = (0, 0)
        await harness.limiter.evaluate(connector.id)
        assert harness.limiter._states[connector.id].stale is False

    async def test_peer_block_is_mirrored_from_redis(self, harness: Harness, redis_client: RedisClient) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        await redis_client.set_connector_traffic_blocked(connector.id, time.time() + 60)
        assert harness.limiter.is_blocked(connector.id) is False
        await harness.limiter.refresh_blocked_for(connector.id)
        assert harness.limiter.is_blocked(connector.id) is True
        await redis_client.clear_connector_traffic_blocked(connector.id)
        await harness.limiter.refresh_blocked_for(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False

    async def test_hydrate_restores_blocks(self, harness: Harness, redis_client: RedisClient) -> None:
        connector = harness.add(make_connector({"limit_bytes": 100, "action": "block"}))
        await redis_client.set_connector_traffic_blocked(connector.id, time.time() + 60)
        await harness.limiter.hydrate_blocked_from_redis([connector.id, "unknown"])
        assert harness.limiter.is_blocked(connector.id) is True
        assert harness.limiter.is_blocked("unknown") is False

    async def test_forget_clears_everything(self, harness: Harness, redis_client: RedisClient) -> None:
        connector = harness.add(make_connector({"limit_bytes": 1, "action": "block"}))
        harness.limiter.record(connector.id, 5, 5)
        await harness.limiter.evaluate(connector.id)
        assert harness.limiter.is_blocked(connector.id) is True
        await harness.limiter.forget(connector.id)
        assert harness.limiter.is_blocked(connector.id) is False
        assert connector.id not in harness.limiter._states
        key = CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=connector.id)
        assert await redis_client.client.exists(key) == 0

    async def test_unknown_connector_is_ignored(self, harness: Harness) -> None:
        assert harness.limiter.record("nope", 1, 1) is False
        await harness.limiter.evaluate("nope")
        assert harness.limiter.is_blocked("nope") is False
        assert harness.limiter.limit_status_for("nope") == 509

    async def test_usage_period_and_price(self, harness: Harness) -> None:
        connector = harness.add(make_connector({
            "limit_bytes": 10 * MB, "period": "week", "reset_day": 1, "price_per_gb": 2.5, "currency": "EUR",
        }))
        harness.limiter.record(connector.id, MB, MB)
        usage = harness.limiter.usage(connector)
        assert usage.period.value == "week"
        assert (usage.period_end - usage.period_start).days == 7
        assert usage.percent == 20.0
        assert usage.cost == pytest.approx(0.005)
        assert usage.currency == "EUR"
        assert usage.action.value == "alert"
