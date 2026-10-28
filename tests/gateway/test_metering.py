# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for a gateway's request metering against the real Redis test container, the rest of the gateway in a harness.

Requests are counted through the public ``record_*`` methods the signal receivers call, and an outage is a batch send
that raises as an unreachable Redis does, before anything reaches Redis or, for a lost reply, after Redis applied it.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Any

import pytest
import structlog

from api.core.demand_tracker import (
    DEMAND_REJECTIONS_FIELD,
    DEMAND_REQUESTS_FIELD,
    DEMAND_TTL_SECONDS,
    DemandTracker,
)
from api.core.job_stats import job_stats
from api.core.rate_limiter import RateLimiter
from api.core.roles import JobKind
from api.core.signals import (
    request_completed,
    request_rejected,
    tunnel_encrypted_dns_blocked,
    tunnel_name_unresolved,
)
from api.core.stats import HOST_OVERFLOW, MetricDelta, StepMetricDelta, TunnelPeerMetricDelta
from api.core.timing import Step
from api.core.traffic_limiter import PROGRESS_REPORT_BYTES
from api.core.workers import WorkerName
from api.db.redis import (
    CONNECTOR_METRICS_KEY,
    CONNECTOR_WINDOWS,
    DEMAND_KEY,
    HOST_METRICS_KEY,
    METRIC_WINDOW_KINDS,
    PROJECT_METRICS_KEY,
    PROJECT_WINDOWS,
    PROXY_METRICS_KEY,
    PROXY_REQUESTS_KEY,
    PROXY_WINDOWS,
    QUARANTINE_KEY,
    RUNTIME_STREAM_KEY,
    TUNNEL_PEER_METRICS_KEY,
    TUNNEL_PEER_WINDOWS,
    BatchChannel,
    BatchOutcome,
    BatchPart,
    BatchReply,
    EntityWindows,
    MetricWindows,
    RedisClient,
)
from api.feed.models import RUNTIME_SECTION, ConnectorSection, RuntimeSection
from api.feed.state import RuntimeStateReader
from api.gateway.metering import REJECTION_COUNT_INTERVAL_SECONDS, RequestMetering
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.proxy import Proxy
from api.providers.registry import ProviderRegistry
from api.routing.inflight import InFlight
from api.routing.outcomes import LocalOutcomes
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from tests.conftest import queued_job_keys
from tests.waits import until

PROJECT = "project-1"
# When the feed's traffic blocks end: long after any test.
BLOCKED_UNTIL = datetime(2100, 1, 1)
# Half a minute into an epoch minute, so a test moving the clock by seconds stays in it.
NOW = 1_800_000_030.0
MINUTE = int(NOW // 60)


async def window(redis_client: RedisClient, kind: EntityWindows, entity_id: str) -> MetricDelta | None:
    """An entity's metrics window in Redis as the api's read model sees it; None when it has none."""
    return (await RuntimeStateReader(redis_client).windows(kind, [entity_id])).get(entity_id)


async def live_windows(redis_client: RedisClient) -> MetricWindows:
    """Every metrics window the indexes list, read as the metrics flush takes them, and left in place."""
    found: dict[str, str] = {}
    for kind in METRIC_WINDOW_KINDS:
        for key in await redis_client.client.smembers(kind.index):
            values = await redis_client.client.hgetall(key)
            if values:
                found[key] = json.dumps(values)
    return MetricWindows.of(found)


class Listener:
    """Stands in for one of a gateway's listeners: what its request signals name as their sender."""


# The listener metering is started on in these tests.
LISTENER = Listener()


def make_connector(connector_id: str = "connector-1", **overrides: Any) -> Connector:
    fields: dict[str, Any] = {
        "id": connector_id,
        "name": connector_id,
        "credential_id": "credential-1",
        "credential_type": CredentialType.STATIC_PROXY_PROVIDER,
        "project_id": PROJECT,
    }
    return Connector(**{**fields, **overrides})


class Harness:
    """Request metering with the rest of the gateway under test control: the router's snapshot is the test's to change."""

    def __init__(self, redis_client: RedisClient, **options: Any) -> None:
        self.redis = redis_client
        # The routing snapshot's connector sections by connector id, and the connectors the feed says are blocked.
        self.sections: dict[str, ConnectorSection] = {}
        self.blocked: set[str] = set()
        self.trips: list[tuple[str, datetime]] = []
        self.now = NOW
        self.inflight = InFlight()
        self.outcomes = LocalOutcomes()
        self.rate_limiter = RateLimiter(
            redis_client, on_trip=lambda proxy_id, until: self.trips.append((proxy_id, until))
        )
        self.router = Router(RoutingSnapshot.from_sections({}, registry=ProviderRegistry()))
        self.metering = RequestMetering(
            redis_client,
            self.inflight,
            self.rate_limiter,
            outcomes=self.outcomes,
            router=self.router,
            clock=lambda: self.now,
            **options,
        )
        self.loop: asyncio.Task[None] | None = None

    def apply(self, connector: Connector) -> None:
        """Put ``connector`` in the snapshot as a section apply does, its proxies kept: its configs parsed now, once."""
        held = self.sections.get(connector.id)
        self.sections[connector.id] = ConnectorSection(connector=connector, proxies=held.proxies if held else [])
        self._swap()

    def add(self, connector: Connector | None = None, *, proxy_id: str = "proxy-1") -> Proxy:
        """A proxy of ``connector`` (a fresh default one when None), now in the snapshot."""
        connector = connector or make_connector()
        proxy = Proxy(id=proxy_id, host="10.0.0.1", port=8080, connector_id=connector.id)
        held = self.sections.get(connector.id)
        self.sections[connector.id] = ConnectorSection(
            connector=connector, proxies=[*(held.proxies if held else []), proxy]
        )
        self._swap()
        return proxy

    def block(self, connector_id: str) -> None:
        """Have the feed block ``connector_id`` for its traffic limit."""
        self.blocked.add(connector_id)
        self._swap()

    def _swap(self) -> None:
        """Route on a snapshot of the sections and blocks as they are now."""
        runtime = RuntimeSection(traffic_blocked_until=dict.fromkeys(self.blocked, BLOCKED_UNTIL))
        self.router.swap(
            RoutingSnapshot.from_sections({**self.sections, RUNTIME_SECTION: runtime}, registry=ProviderRegistry())
        )

    def complete(self, proxy: Proxy, *, success: bool = True, latency_ms: float = 10.0, **kwargs: Any) -> None:
        """One finished request through ``proxy``, 1 byte up and 2 down unless told otherwise."""
        self.metering.record_completed(
            proxy.id,
            kwargs.pop("project_id", PROJECT),
            success,
            latency_ms,
            kwargs.pop("bytes_sent", 1),
            kwargs.pop("bytes_received", 2),
            **kwargs,
        )

    async def settle(self) -> None:
        """Wait for the rate-limit windows being counted in held tasks."""
        await self.metering._held.settle()

    def start_loop(self) -> None:
        self.loop = asyncio.create_task(self.metering.run())

    async def close(self) -> None:
        await self.metering.stop()
        if self.loop is not None:
            self.loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop


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


async def read_hash(redis_client: RedisClient, key: str) -> dict[str, str]:
    data: dict[str, str] = await redis_client.client.hgetall(key)  # type: ignore[misc]
    return data


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Iterator[None]:
    """One registry per process, so no test may inherit another's counters."""
    job_stats.reset()
    yield
    job_stats.reset()


@pytest.fixture
async def harness(redis_client: RedisClient) -> AsyncIterator[Harness]:
    h = Harness(redis_client)
    yield h
    await h.close()


@pytest.fixture
def outage(redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> Outage:
    return Outage(redis_client, monkeypatch)


class TestCounting:
    async def test_a_request_counts_on_its_proxy_connector_project_host_and_device(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        harness.complete(proxy, latency_ms=50.0, bytes_sent=10, bytes_received=20, peer_id="device-1",
                         target_host="Shop.Example.com.")
        harness.complete(proxy, success=False, latency_ms=30.0, bytes_sent=1, bytes_received=1)

        # Nothing reaches Redis before the flush.
        assert await read_hash(redis_client, PROXY_METRICS_KEY.format(proxy_id=proxy.id)) == {}
        # Proxy, connector, project, one host row, one device, one demand minute.
        assert harness.metering.pending == 6

        assert await harness.metering.flush() == 6
        # The latency is the success's alone: the failure's time to fail is no latency.
        both = MetricDelta(request_count=2, success_count=1, failure_count=1, latency_sum_ms=50.0, bytes_sent=11,
                           bytes_received=21)
        first = MetricDelta(request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=10, bytes_received=20)
        assert await window(redis_client, PROXY_WINDOWS, proxy.id) == both
        assert await window(redis_client, CONNECTOR_WINDOWS, proxy.connector_id) == StepMetricDelta.summed(both)
        assert await window(redis_client, PROJECT_WINDOWS, PROJECT) == StepMetricDelta.summed(both)
        assert (await live_windows(redis_client)).hosts == {(PROJECT, proxy.connector_id, "shop.example.com"): first}
        assert await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1") == TunnelPeerMetricDelta(
            request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=10, bytes_received=20,
        )
        # Listed in the indexes, so the metrics flush finds every window without a walk.
        assert (await live_windows(redis_client)).proxies == {proxy.id: both}
        assert harness.metering.pending == 0
        assert await harness.metering.flush() == 0

    async def test_the_project_and_connector_keep_the_successes_steps_and_the_histograms_every_request(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add(make_connector(name="geonode"))
        harness.complete(proxy, latency_ms=100.0, steps={Step.ROUTE: 1.0, Step.CONNECT: 19.0, Step.HANDSHAKE: 80.0})
        harness.complete(proxy, latency_ms=700.0, steps={Step.ROUTE: 1.0, Step.PREFLIGHT: 600.0, Step.HANDSHAKE: 99.0})
        harness.complete(proxy, success=False, latency_ms=30000.0, steps={Step.ROUTE: 1.0, Step.HANDSHAKE: 29999.0})
        await harness.metering.flush()

        steps = StepMetricDelta(
            request_count=3, success_count=2, failure_count=1, latency_sum_ms=800.0, bytes_sent=3, bytes_received=6,
            route_sum_ms=2.0, preflight_sum_ms=600.0, connect_sum_ms=19.0, handshake_sum_ms=179.0,
        )
        assert await window(redis_client, PROJECT_WINDOWS, PROJECT) == steps
        assert await window(redis_client, CONNECTOR_WINDOWS, proxy.connector_id) == steps
        # The proxy's and host's windows keep no steps: they would only grow the busiest windows.
        proxy_window = await read_hash(redis_client, PROXY_METRICS_KEY.format(proxy_id=proxy.id))
        assert not {name for name in proxy_window if name.endswith("_sum_ms") and name != "latency_sum_ms"}

        histograms = {(h.success, h.step): h for h in harness.metering.latencies.snapshot()}
        assert {h.connector_name for h in histograms.values()} == {"geonode"}
        assert (sum(histograms[(True, None)].buckets), histograms[(True, None)].sum_seconds) == (2, 0.8)
        assert sum(histograms[(True, "preflight")].buckets) == 1
        assert sum(histograms[(False, "handshake")].buckets) == 1
        assert (True, "response") not in histograms

    async def test_a_proxy_gone_from_the_snapshot_has_no_histogram(self, harness: Harness) -> None:
        """Its connector is unknown, so there is nothing to label the latency with."""
        harness.complete(Proxy(id="gone", host="10.0.0.9", port=8080, connector_id="connector-x"),
                         steps={Step.ROUTE: 1.0})
        assert harness.metering.latencies.snapshot() == []

    async def test_progress_counts_bytes_and_the_request_counts_when_it_ends(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        harness.metering.progress(proxy.id, PROJECT, proxy.connector_id, None, 5, 7, "shop.example.com")
        await harness.metering.flush()
        assert await window(redis_client, PROXY_WINDOWS, proxy.id) == MetricDelta(bytes_sent=5, bytes_received=7)
        assert (await live_windows(redis_client)).hosts == {
            (PROJECT, proxy.connector_id, "shop.example.com"): MetricDelta(bytes_sent=5, bytes_received=7),
        }

        harness.complete(proxy, bytes_sent=1, bytes_received=1, target_host="shop.example.com")
        await harness.metering.flush()
        assert await window(redis_client, PROXY_WINDOWS, proxy.id) == MetricDelta(
            request_count=1, success_count=1, latency_sum_ms=10.0, bytes_sent=6, bytes_received=8,
        )
        assert (harness.metering.totals.request_count, harness.metering.totals.bytes_received) == (1, 8)

    async def test_a_proxy_gone_from_the_snapshot_counts_on_its_project_and_device_only(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        # Deleted while its request ran: nothing to count on but the project and the device.
        harness.complete(Proxy(id="gone", host="10.0.0.9", port=8080, connector_id="connector-x"),
                         peer_id="device-1", target_host="shop.example.com")
        await harness.metering.flush()
        windows = await live_windows(redis_client)
        assert (windows.proxies, windows.connectors, windows.hosts) == ({}, {}, {})
        assert (await window(redis_client, PROJECT_WINDOWS, PROJECT) or MetricDelta()).request_count == 1
        assert (await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1") or MetricDelta()).request_count == 1
        assert harness.outcomes.of_proxy("gone") == (0, 0, 0.0)

    async def test_tunnel_devices_count_their_requests_bytes_and_name_resolution(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        # A device's request is counted whatever the proxy lookup says.
        harness.complete(Proxy(id="unknown", host="10.0.0.9", port=8080, connector_id="unknown"), latency_ms=50.0,
                         bytes_sent=10, bytes_received=20, peer_id="device-1")
        harness.metering.progress("unknown", PROJECT, "unknown", "device-1", 5, 5)
        harness.metering.record_unresolved("device-1")
        harness.metering.record_encrypted_dns_blocked("device-1")
        harness.metering.record_encrypted_dns_blocked("device-1")
        expected = TunnelPeerMetricDelta(
            request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=15, bytes_received=25,
            by_address=1, encrypted_dns_blocked=2,
        )

        await harness.metering.flush()
        data = await read_hash(redis_client, TUNNEL_PEER_METRICS_KEY.format(peer_id="device-1"))
        assert (int(data["by_address"]), int(data["encrypted_dns_blocked"]), int(data["bytes_sent"])) == (1, 2, 15)
        assert await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1") == expected

    async def test_host_metrics_switched_off_count_no_host(self, redis_client: RedisClient) -> None:
        harness = Harness(redis_client, host_metrics_enabled=False)
        proxy = harness.add()
        harness.complete(proxy, target_host="a.example.com")
        await harness.metering.flush()
        assert (await live_windows(redis_client)).hosts == {}
        assert (await window(redis_client, CONNECTOR_WINDOWS, proxy.connector_id) or MetricDelta()).request_count == 1

    async def test_a_ceiling_keeps_each_windows_busiest_hosts_and_folds_the_rest(
        self, redis_client: RedisClient
    ) -> None:
        harness = Harness(redis_client, host_metrics_max_hosts=2)
        proxy = harness.add()
        # Arrival order is not rank: the one-off hosts come first, the busy ones after.
        for host in ["a.example.com", "b.example.com", "c.example.com", "d.example.com", "d.example.com",
                     "e.example.com", "e.example.com", "e.example.com"]:
            harness.complete(proxy, bytes_sent=1, bytes_received=1, target_host=host)
        # Nothing is folded on the request path.
        assert len(harness.metering._windows.hosts) == 5

        await harness.metering.flush()
        flushed = (await live_windows(redis_client)).hosts
        assert {host: d.request_count for (_p, _c, host), d in flushed.items()} == {
            "e.example.com": 3, "d.example.com": 2, HOST_OVERFLOW: 3,
        }
        assert flushed[(PROJECT, proxy.connector_id, HOST_OVERFLOW)].bytes_sent == 3

    async def test_the_loop_writes_once_per_window_and_idles_when_nothing_was_counted(
        self, redis_client: RedisClient
    ) -> None:
        harness = Harness(redis_client, interval=0.05)
        proxy = harness.add()
        harness.complete(proxy)
        harness.complete(proxy)
        harness.start_loop()
        try:
            await until(lambda: harness.metering.pending == 0)
            assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2
            await until(lambda: (s := job_stats.get(WorkerName.REQUEST_METERING)) is not None and s.idle_runs >= 1)
            stats = job_stats.get(WorkerName.REQUEST_METERING)
            assert stats is not None and stats.failures == 0 and stats.interval_seconds == 0.05
            # Counted once: the empty windows since wrote nothing more.
            assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2
        finally:
            await harness.close()


class TestDemand:
    async def test_completed_and_rejected_requests_count_per_project_and_minute(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        for _ in range(3):
            harness.complete(proxy)
        harness.metering.record_rejection(PROJECT)
        harness.now += 60  # the next minute has its own hash
        harness.complete(proxy)

        await harness.metering.flush()
        key = DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)
        assert await read_hash(redis_client, key) == {DEMAND_REQUESTS_FIELD: "3", DEMAND_REJECTIONS_FIELD: "1"}
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE + 1)) == {
            DEMAND_REQUESTS_FIELD: "1",
        }
        # Each hash goes a minute after it has left the demand window.
        assert DEMAND_TTL_SECONDS - 5 <= await redis_client.client.ttl(key) <= DEMAND_TTL_SECONDS

    async def test_a_burst_of_rejections_counts_once_a_second(self, harness: Harness, redis_client: RedisClient) -> None:
        for _ in range(10):
            harness.metering.record_rejection(PROJECT)
        harness.metering.record_rejection("project-2")
        # A second later the project's rejections count again.
        harness.metering._last_rejection[PROJECT] -= REJECTION_COUNT_INTERVAL_SECONDS + 0.1
        harness.metering.record_rejection(PROJECT)

        await harness.metering.flush()
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == {
            DEMAND_REJECTIONS_FIELD: "2",
        }
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id="project-2", minute=MINUTE)) == {
            DEMAND_REJECTIONS_FIELD: "1",
        }

    async def test_the_tracker_reads_what_gateways_counted(self, harness: Harness, redis_client: RedisClient) -> None:
        proxy = harness.add()
        for _ in range(9):
            harness.complete(proxy)
        harness.metering.record_rejection(PROJECT)
        await harness.metering.flush()

        tracker = DemandTracker(redis_client, clock=lambda: harness.now)
        # Ten requests in the current minute: two a minute over the five-minute window.
        assert await tracker.get_requests_per_minute(PROJECT) == pytest.approx(2.0)
        assert await tracker.has_recent_activity(PROJECT) is True


class TestFailedFlush:
    async def test_a_window_redis_did_not_take_is_owed_and_lands_with_what_was_counted_since(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        proxy = harness.add()
        harness.complete(proxy, peer_id="device-1", target_host="a.example.com")
        harness.complete(proxy)
        outage.on = True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        assert harness.metering.failed_flushes == 1
        assert (await live_windows(redis_client)).proxies == {}
        # Proxy, connector, project, host, device and demand: owed, and still pending.
        assert harness.metering.pending == 6

        # Counting goes on meanwhile, into the pending window.
        harness.complete(proxy)
        outage.on = False
        assert await harness.metering.flush() == 6 + 4
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 3
        assert (await window(redis_client, PROJECT_WINDOWS, PROJECT) or MetricDelta()).request_count == 3
        assert (await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1") or MetricDelta()).request_count == 1
        assert len((await live_windows(redis_client)).hosts) == 1
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == {
            DEMAND_REQUESTS_FIELD: "3",
        }
        assert harness.metering.pending == 0

    async def test_a_window_redis_applied_but_did_not_answer_for_is_counted_once(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        """The reply lost on its way back: sent again, the window is known by its identity and not added twice."""
        proxy = harness.add()
        harness.complete(proxy, bytes_sent=10, bytes_received=20)
        outage.on, outage.reply_lost = True, True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        # It reached Redis all the same.
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 1

        outage.on = False
        assert await harness.metering.flush() == 4
        reported = await window(redis_client, PROXY_WINDOWS, proxy.id)
        assert reported is not None and (reported.request_count, reported.bytes_received) == (1, 20)
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == {
            DEMAND_REQUESTS_FIELD: "1",
        }

    async def test_what_is_owed_goes_again_alone_and_unchanged(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        """Merged into a part already sent, new counts would make it another batch under the same identity."""
        proxy = harness.add()
        harness.complete(proxy)
        outage.on = True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        harness.complete(proxy)
        harness.complete(proxy)
        with pytest.raises(ConnectionError):
            await harness.metering.flush()

        first, second = outage.sends
        assert second == first
        assert [part.id.channel for part in first] == [BatchChannel.METERING]
        # The two requests since wait in the pending window: proxy, connector, project, demand minute.
        assert harness.metering.pending == 4 + 4

        outage.on = False
        await harness.metering.flush()
        third, fourth = outage.sends[2:]
        assert third == first
        assert fourth[0].id.sequence > first[0].id.sequence
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 3

    async def test_a_part_redis_refused_to_run_goes_again_under_a_new_sequence(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        """The script cache was flushed between two flushes: Redis runs nothing of the part and says NOSCRIPT."""
        proxy = harness.add()
        harness.complete(proxy)
        await harness.metering.flush()
        await redis_client.client.script_flush()
        harness.complete(proxy)

        assert await harness.metering.flush() == 0
        assert harness.metering.failed_flushes == 1
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 1

        assert await harness.metering.flush() == 4
        refused, resent = outage.sends[1:]
        assert (refused[0].keys, refused[0].args) == (resent[0].keys, resent[0].args)
        assert resent[0].id.sequence > refused[0].id.sequence
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2

    async def test_a_window_waits_while_what_is_owed_keeps_being_refused(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        """Sent beside a part refused again, the window would take the part's place in what is owed, and lose it."""
        proxy = harness.add()
        harness.complete(proxy)
        outage.refusals = 2
        assert await harness.metering.flush() == 0
        harness.complete(proxy)
        assert await harness.metering.flush() == 0
        # Only the refused part went, under a new sequence; the request since waits behind it.
        assert [len(parts) for parts in outage.sends] == [1, 1]
        assert outage.sends[1][0].id.sequence > outage.sends[0][0].id.sequence
        assert harness.metering.pending == 4 + 4

        assert await harness.metering.flush() == 4 + 4
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2

    async def test_demand_minutes_no_reader_looks_at_are_left_out_of_a_window_cut_late(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        proxy = harness.add()
        harness.complete(proxy)
        outage.on = True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        # Counted while the first window was owed; Redis is away for ten minutes.
        harness.now += 60
        harness.complete(proxy)
        harness.now += 540

        outage.on = False
        await harness.metering.flush()
        # The window sent before the outage goes as it was; the one cut ten minutes late keeps no minute
        # the readers have left behind.
        assert await redis_client.client.exists(DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == 1
        assert await redis_client.client.exists(DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE + 1)) == 0
        # The requests themselves are counted.
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2

    async def test_the_loop_counts_a_failed_cycle_and_keeps_going(
        self, redis_client: RedisClient, outage: Outage
    ) -> None:
        harness = Harness(redis_client, interval=0.02)
        proxy = harness.add()
        harness.complete(proxy)
        outage.on = True
        harness.start_loop()
        try:
            await until(lambda: harness.metering.failed_flushes >= 2)
            stats = job_stats.get(WorkerName.REQUEST_METERING)
            assert stats is not None and stats.failures >= 2 and stats.consecutive_failures >= 2
            outage.on = False
            await until(lambda: harness.metering.pending == 0)
            assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 1
        finally:
            await harness.close()

    async def test_a_receiver_never_raises_into_the_request(self, harness: Harness) -> None:
        def broken() -> RoutingSnapshot:
            raise RuntimeError("snapshot bug")

        harness.metering._views = broken
        harness.metering.start([LISTENER])
        # The listener's ``finally`` awaits the signal: it must come back clean.
        await request_completed.send_async(
            LISTENER, proxy_id="proxy-1", project_id=PROJECT, success=True, latency_ms=1.0, bytes_sent=0,
            bytes_received=0,
        )
        harness.metering.progress("proxy-1", PROJECT, "connector-1", None, 1, 1)


async def publish_all(sender: object, proxy_id: str) -> None:
    """Every signal metering counts, as one listener publishes them: a request completed and one rejected, and the tunnel signals."""
    await request_completed.send_async(
        sender, proxy_id=proxy_id, project_id=PROJECT, success=True, latency_ms=5.0, bytes_sent=1,
        bytes_received=2, peer_id="device-1", target_host="a.example.com",
    )
    await request_rejected.send_async(sender, project_id=PROJECT, reason="no_proxy_available")
    await tunnel_name_unresolved.send_async(sender, peer_id="device-1")
    await tunnel_encrypted_dns_blocked.send_async(sender, peer_id="device-1")


class TestListeners:
    async def test_only_what_the_gateways_own_listeners_publish_is_counted(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        """Gateways sharing one process each count their own requests, and trip their own rate limits."""
        proxy = harness.add()
        harness.metering.start([LISTENER])

        # Another gateway's listener.
        await publish_all(Listener(), proxy.id)
        assert harness.metering.pending == 0

        await publish_all(LISTENER, proxy.id)
        await harness.metering.stop()
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 1
        device = await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1")
        assert isinstance(device, TunnelPeerMetricDelta) and (device.by_address, device.encrypted_dns_blocked) == (1, 1)
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == {
            DEMAND_REQUESTS_FIELD: "1", DEMAND_REJECTIONS_FIELD: "1",
        }


class TestStop:
    async def test_stop_writes_what_is_pending_and_stops_listening(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        harness.metering.start([LISTENER])
        await publish_all(LISTENER, proxy.id)

        await harness.metering.stop()
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 1
        device = await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1")
        assert isinstance(device, TunnelPeerMetricDelta) and (device.by_address, device.encrypted_dns_blocked) == (1, 1)
        assert await read_hash(redis_client, DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE)) == {
            DEMAND_REQUESTS_FIELD: "1", DEMAND_REJECTIONS_FIELD: "1",
        }

        # Stopped: the signals reach it no more.
        await publish_all(LISTENER, proxy.id)
        assert harness.metering.pending == 0

    async def test_stop_waits_for_the_rate_limit_windows_being_counted(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A count still on its way when the gateway stops is not cut off: the vendor saw the request."""
        counted: list[str] = []

        async def slow_count(proxy_id: str, *_: Any) -> None:
            await asyncio.sleep(0.2)
            counted.append(proxy_id)

        monkeypatch.setattr(harness.rate_limiter, "record_request", slow_count)
        rate = {"max_requests": 3, "window_seconds": 60, "quarantine_seconds_min": 30, "quarantine_seconds_max": 30}
        proxy = harness.add(make_connector(rate_limit_config=rate))
        harness.complete(proxy)

        await harness.metering.stop()

        assert counted == [proxy.id]

    async def test_stop_lets_a_flush_in_progress_finish_first(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One send at a time: a newer window sent first would mark the older one applied, and Redis would skip it."""
        harness = Harness(redis_client, interval=0.01)
        proxy = harness.add()
        entered, released = asyncio.Event(), asyncio.Event()
        real = redis_client.send_batch

        async def slow_send(parts: list[BatchPart]) -> list[BatchReply]:
            # Only the loop's send is held up; a send that overtook it would reach Redis first.
            if not entered.is_set():
                entered.set()
                await released.wait()
            return await real(parts)

        monkeypatch.setattr(redis_client, "send_batch", slow_send)
        harness.complete(proxy)
        harness.start_loop()
        await asyncio.wait_for(entered.wait(), 2)
        # Counted while the loop's flush is on its way.
        harness.complete(proxy)

        stopping = asyncio.create_task(harness.metering.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        released.set()
        await asyncio.wait_for(stopping, 2)
        await harness.close()
        # Both requests, each written once.
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2

    async def test_stop_never_raises_when_redis_is_away(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        harness.complete(harness.add())
        outage.on = True
        await harness.metering.stop()
        assert harness.metering.failed_flushes == 1
        # Still owed: proxy, connector, project and the demand minute.
        assert harness.metering.pending == 4

    async def test_stop_sends_what_is_owed_and_then_the_window(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        proxy = harness.add()
        harness.complete(proxy)
        outage.on = True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        harness.complete(proxy)
        outage.on = False

        await harness.metering.stop()

        assert harness.metering.pending == 0
        assert (await window(redis_client, PROXY_WINDOWS, proxy.id) or MetricDelta()).request_count == 2


class TestOutcomes:
    async def test_outcomes_go_to_the_local_outcomes_it_was_given_and_are_never_sent(self, harness: Harness) -> None:
        """Only for a proxy the snapshot holds: one deleted while its request ran has nothing left to route on."""
        proxy = harness.add()
        harness.complete(proxy, latency_ms=100.0)
        harness.complete(proxy, success=False, latency_ms=300.0)
        harness.complete(Proxy(id="gone", host="10.0.0.9", port=8080, connector_id=proxy.connector_id))
        assert harness.outcomes.of_proxy(proxy.id) == (2, 1, 400.0)
        assert harness.outcomes.of_proxy("gone") == (0, 0, 0.0)
        await harness.metering.flush()
        harness.complete(proxy, latency_ms=50.0)
        assert harness.outcomes.of_proxy(proxy.id) == (3, 2, 450.0)


class TestMeters:
    async def test_a_transfer_is_in_flight_from_its_meter_to_its_finish(self, harness: Harness) -> None:
        proxy = harness.add()
        meter = harness.metering.meter(proxy, PROJECT)
        other = harness.metering.meter(proxy, PROJECT)
        assert (harness.inflight.of_proxy(proxy.id), harness.inflight.of_connector(proxy.connector_id)) == (2, 2)
        meter.add_sent(10)
        assert meter.finish() == (10, 0)
        assert (harness.inflight.of_proxy(proxy.id), harness.inflight.of_connector(proxy.connector_id)) == (1, 1)
        # Finishing again ends nothing again: the other transfer stays counted.
        meter.finish()
        assert harness.inflight.of_proxy(proxy.id) == 1
        other.finish()
        assert harness.inflight.total == 0

    async def test_bytes_land_in_the_pending_window_while_the_transfer_runs(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        meter = harness.metering.meter(proxy, PROJECT, peer_id="device-1", target_host="shop.example.com")
        # A long tunnel: two megabytes down while it is still open.
        for _ in range(4):
            assert meter.add_received(PROGRESS_REPORT_BYTES // 2) is True
        meter.add_sent(5)
        await harness.metering.flush()
        reported = await window(redis_client, PROXY_WINDOWS, proxy.id)
        assert reported is not None and reported.request_count == 0
        assert reported.bytes_received >= PROGRESS_REPORT_BYTES
        device = await window(redis_client, TUNNEL_PEER_WINDOWS, "device-1")
        assert device is not None and device.bytes_received == reported.bytes_received

        # The completion carries only what the meter had not reported: nothing is counted twice.
        sent, received = meter.finish()
        harness.complete(proxy, bytes_sent=sent, bytes_received=received, peer_id="device-1",
                         target_host="shop.example.com")
        await harness.metering.flush()
        total = await window(redis_client, PROXY_WINDOWS, proxy.id)
        assert total is not None and (total.request_count, total.bytes_sent, total.bytes_received) == (
            1, 5, 2 * PROGRESS_REPORT_BYTES,
        )
        hosts = (await live_windows(redis_client)).hosts
        assert hosts[(PROJECT, proxy.connector_id, "shop.example.com")].bytes_received == 2 * PROGRESS_REPORT_BYTES

    async def test_a_blocked_connector_with_the_interrupt_action_stops_its_transfers(self, harness: Harness) -> None:
        interrupt = {"limit_bytes": 10, "action": "interrupt", "limit_status": 429}
        proxy = harness.add(make_connector(traffic_config=interrupt))
        meter = harness.metering.meter(proxy, PROJECT)
        assert meter.add_received(100) is True
        harness.block(proxy.connector_id)
        assert meter.allowed is False
        assert meter.add_received(1) is False
        assert meter.limit_status == 429

    async def test_the_block_action_lets_running_transfers_finish(self, harness: Harness) -> None:
        proxy = harness.add(make_connector(traffic_config={"limit_bytes": 10, "action": "block"}))
        meter = harness.metering.meter(proxy, PROJECT)
        harness.block(proxy.connector_id)
        assert meter.allowed is True
        assert meter.limit_status == 509

    async def test_a_running_transfer_follows_a_config_change_at_its_next_report(self, harness: Harness) -> None:
        proxy = harness.add(make_connector(traffic_config={"limit_bytes": 10, "action": "block"}))
        meter = harness.metering.meter(proxy, PROJECT)
        harness.block(proxy.connector_id)
        assert meter.allowed is True
        # The feed brings the connector back with the interrupt action.
        harness.apply(make_connector(traffic_config={"limit_bytes": 10, "action": "interrupt"}))
        assert meter.add_received(PROGRESS_REPORT_BYTES) is False

    async def test_an_unknown_connector_is_never_interrupted(self, harness: Harness) -> None:
        harness.block("nope")
        assert harness.metering.is_interrupted("nope") is False
        assert harness.metering.limit_status_for("nope") == 509


class TestTrafficRequests:
    """A window asks the workers for a traffic decision on each connector with a limit whose bytes it carries."""

    async def test_a_limited_connector_s_bytes_ask_for_one_decision_per_window(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        limited = harness.add(make_connector("limited", traffic_config={"limit_bytes": 10_000}), proxy_id="p1")
        free = harness.add(make_connector("free"), proxy_id="p2")
        harness.complete(limited, bytes_sent=10, bytes_received=20)
        harness.complete(limited, bytes_sent=10, bytes_received=20)
        harness.complete(free, bytes_sent=10, bytes_received=20)

        await harness.metering.flush()

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["limited"]
        # In the same script as the connector's bytes: the decision never reads the usage without them.
        assert (await window(redis_client, CONNECTOR_WINDOWS, "limited") or MetricDelta()).bytes_received == 40
        # Nothing for the controller on the runtime stream any more.
        assert await redis_client.client.xlen(RUNTIME_STREAM_KEY) == 0

        # The next window that moves its bytes asks again.
        harness.metering.progress(limited.id, PROJECT, "limited", None, 5, 5)
        await harness.metering.flush()
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["limited", "limited"]

    async def test_no_decision_without_a_limit_or_without_bytes(self, harness: Harness, redis_client: RedisClient) -> None:
        free = harness.add(make_connector("free"), proxy_id="p1")
        harness.complete(free, bytes_sent=10, bytes_received=20)
        await harness.metering.flush()
        # A refused request carried no byte through the limited connector: its usage did not move.
        limited = harness.add(make_connector("limited", traffic_config={"limit_bytes": 10_000}), proxy_id="p2")
        harness.complete(limited, success=False, bytes_sent=0, bytes_received=0)
        await harness.metering.flush()

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []

    async def test_a_window_owed_asks_once_however_often_it_is_sent(
        self, harness: Harness, redis_client: RedisClient, outage: Outage
    ) -> None:
        limited = harness.add(make_connector("limited", traffic_config={"limit_bytes": 10_000}))
        harness.complete(limited, bytes_sent=10, bytes_received=20)
        outage.on, outage.reply_lost = True, True
        with pytest.raises(ConnectionError):
            await harness.metering.flush()
        outage.on = False
        await harness.metering.flush()

        assert len(outage.sends) == 2 and outage.sends[1] == outage.sends[0]
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["limited"]

    async def test_a_snapshot_that_cannot_be_read_keeps_the_window_for_the_next_flush(
        self, harness: Harness, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which connectors have a limit is read before the window is cut, so a failure loses nothing."""
        limited = harness.add(make_connector("limited", traffic_config={"limit_bytes": 10_000}))
        harness.complete(limited, bytes_sent=10, bytes_received=20)

        def broken(_router: Router) -> RoutingSnapshot:
            raise RuntimeError("snapshot bug")

        with monkeypatch.context() as patched:
            patched.setattr(Router, "snapshot", property(broken))
            with pytest.raises(RuntimeError):
                await harness.metering.flush()
        assert harness.metering.pending == 4

        assert await harness.metering.flush() == 4
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == ["limited"]


class TestRateLimits:
    async def test_a_full_window_trips_the_proxy_for_every_gateway(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        rate = {"max_requests": 3, "window_seconds": 60, "quarantine_seconds_min": 30, "quarantine_seconds_max": 30}
        proxy = harness.add(make_connector(rate_limit_config=rate))
        harness.complete(proxy)
        harness.complete(proxy)
        await harness.settle()
        assert harness.trips == []
        assert await redis_client.client.zcard(PROXY_REQUESTS_KEY.format(proxy_id=proxy.id)) == 2

        harness.complete(proxy)
        await harness.settle()
        assert [proxy_id for proxy_id, _until in harness.trips] == [proxy.id]
        until = await redis_client.client.zscore(QUARANTINE_KEY, proxy.id)
        assert until == pytest.approx(time.time() + 30, abs=5)
        # The window starts over.
        assert await redis_client.client.exists(PROXY_REQUESTS_KEY.format(proxy_id=proxy.id)) == 0

    async def test_an_unreadable_rate_limit_is_none_and_metering_has_nothing_to_say_about_it(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        """The snapshot parsed it, and logged it, when the section was applied; metering only reads the result."""
        proxy = harness.add(make_connector(rate_limit_config={"max_requests": "many"}))
        with structlog.testing.capture_logs() as logs:
            harness.complete(proxy)
            await harness.settle()
        assert logs == []
        assert await redis_client.client.exists(PROXY_REQUESTS_KEY.format(proxy_id=proxy.id)) == 0
        assert harness.outcomes.of_proxy(proxy.id) == (1, 1, 10.0)

    async def test_a_connector_without_a_rate_limit_keeps_no_window(
        self, harness: Harness, redis_client: RedisClient
    ) -> None:
        proxy = harness.add()
        harness.complete(proxy)
        await harness.settle()
        assert await redis_client.client.exists(PROXY_REQUESTS_KEY.format(proxy_id=proxy.id)) == 0

    async def test_a_window_redis_cannot_count_lets_the_request_through(self) -> None:
        # Nothing listens there: every command fails fast, as with Redis down.
        unreachable = RedisClient("redis://127.0.0.1:1/0", socket_timeout=0.5)
        await unreachable.connect()
        try:
            harness = Harness(unreachable)
            rate = {"max_requests": 1, "window_seconds": 60, "quarantine_seconds_min": 30,
                    "quarantine_seconds_max": 30}
            proxy = harness.add(make_connector(rate_limit_config=rate))
            harness.complete(proxy)
            await harness.settle()
            assert harness.rate_limiter.window_errors == 1
            assert harness.trips == []
            # The request is counted locally all the same.
            assert harness.outcomes.of_proxy(proxy.id) == (1, 1, 10.0)
        finally:
            await unreachable.close()


class TestRedisKeys:
    async def test_windows_use_the_catalogue_keys(self, harness: Harness, redis_client: RedisClient) -> None:
        proxy = harness.add()
        harness.complete(proxy, target_host="a.example.com")
        await harness.metering.flush()
        for key in (
            PROXY_METRICS_KEY.format(proxy_id=proxy.id),
            CONNECTOR_METRICS_KEY.format(connector_id=proxy.connector_id),
            PROJECT_METRICS_KEY.format(project_id=PROJECT),
            HOST_METRICS_KEY.format(project_id=PROJECT, connector_id=proxy.connector_id, host="a.example.com"),
            DEMAND_KEY.format(project_id=PROJECT, minute=MINUTE),
        ):
            assert await redis_client.client.exists(key) == 1, key
