# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the api's read model against the real Postgres and Redis test containers.

One project holds the cases every count tells apart (on an enabled connector a healthy, a quarantined, an unhealthy and
a draining proxy; a disabled connector's healthy proxy; a healthy proxy of a connector over its traffic limit, which the
traffic job blocked), another project one healthy proxy, and soft state is written the way its producers write it.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.demand_tracker import DemandLevel, DemandTracker
from api.core.stats import MetricDelta, StepMetricDelta, TunnelPeerMetricDelta
from api.core.traffic_limiter import ConnectorTraffic
from api.db.redis import (
    CONNECTOR_METRICS_KEY,
    PROJECT_METRICS_KEY,
    PROJECT_WINDOWS,
    PROXY_HEALTH_KEY,
    PROXY_METRICS_KEY,
    PROXY_WINDOWS,
    QUARANTINE_KEY,
    TRAFFIC_BLOCKED_KEY,
    TUNNEL_PEER_METRICS_KEY,
    TUNNEL_PEER_WINDOWS,
    EntityWindows,
    RedisClient,
)
from api.db.repository import MetricsRepository
from api.feed.state import HealthSoft, RuntimeStateReader
from api.geo.models import ExitCoverage
from api.jobs.job import JobBatch
from api.jobs.traffic import TrafficJob
from api.models.connector import Connector, ConnectorCreate, ProxyTarget
from api.models.credential import Credential
from api.models.project import Project, ProjectUpdate
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.services.connectors import ConnectorService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService
from api.services.read_model import PoolCounts, ProjectView, RuntimeView
from tests.jobs.conftest import counted_round_trips, counted_selects
from tests.providers.test_registry import fail_to_build
from tests.services.conftest import AWS, OXYLABS_DYNAMIC, OXYLABS_POOL, STATIC, Connect

# The capped connector's limit, and what its history holds this period.
LIMIT_BYTES = 100
CAPPED_BYTES = 120


class FakeDemand(DemandTracker):
    """Demand as the tracker reports it, recording the pool size each project was weighed against."""

    def __init__(self, redis_client: RedisClient) -> None:
        super().__init__(redis_client)
        self.asked: list[tuple[str, int]] = []

    async def get_demand_info(
        self, project_id: str, current_proxy_count: int, previous_level: DemandLevel | None = None
    ) -> dict[str, Any]:
        self.asked.append((project_id, current_proxy_count))
        return {"demand_level": DemandLevel.MEDIUM, "requests_per_minute": 30.0, "rate_per_proxy": 15.0}


@dataclass
class World:
    project: Project
    other: Project
    isp: Connector
    off: Connector
    capped: Connector
    healthy: Proxy
    quarantined: Proxy
    unhealthy: Proxy
    draining: Proxy
    idle: Proxy
    capped_proxy: Proxy
    # The other project's connector.
    theirs: Connector


@pytest.fixture
def demand(redis_client: RedisClient) -> FakeDemand:
    return FakeDemand(redis_client)


@pytest.fixture
def view(
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    registry: ProviderRegistry,
    demand: FakeDemand,
) -> RuntimeView:
    return RuntimeView(sessions, RuntimeStateReader(redis_client), registry, demand)


def rows(connector: Connector, *statuses: ProxyStatus, start: int = 0, **fields: Any) -> list[Proxy]:
    """Proxies of ``connector`` in ``statuses``, created a second apart so their order is fixed."""
    base = datetime(2026, 10, 1, 12, 0, 0)
    return [
        Proxy(
            host=f"10.0.{start}.{n}",
            port=8080,
            connector_id=connector.id,
            status=status,
            created_at=base + timedelta(seconds=start + n),
            **fields,
        )
        for n, status in enumerate(statuses)
    ]


async def save_history(
    sessions: async_sessionmaker[AsyncSession], connector: Connector, sent: int, received: int
) -> None:
    """A flushed window of the connector's traffic, stamped now: inside its current period."""
    delta = MetricDelta(request_count=1, success_count=1, bytes_sent=sent, bytes_received=received)
    async with sessions() as session:
        await MetricsRepository(session).save_connector_metrics_snapshots({connector.id: delta})
        await session.commit()


async def write_window(redis_client: RedisClient, key: str, delta: MetricDelta) -> None:
    """A metrics hash as gateways leave it."""
    await redis_client.client.hset(key, mapping={**delta.to_dict(), "updated_at": "2026-10-17T12:00:00"})


def period_end(connector: Connector) -> datetime:
    """When the connector's current usage window ends: what a block the traffic job decides now runs until."""
    return ConnectorTraffic.of(connector, utc_now()).end


async def store_block(redis_client: RedisClient, connector: Connector, until: datetime) -> None:
    """A traffic block as the traffic job stores it: connector ID -> its end, ISO in naive UTC."""
    await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, connector.id, until.isoformat())


async def decide_traffic(
    sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, *connectors: Connector
) -> None:
    """The traffic decision a worker makes for ``connectors`` from what Postgres and Redis hold now."""
    batch = JobBatch(ids=("1-0",), keys=tuple(connector.id for connector in connectors))
    await TrafficJob(sessions, redis_client).run(batch)


@pytest.fixture
async def world(
    project: Project,
    other_project: Project,
    static_credential: Credential,
    connect: Connect,
    connector_service: ConnectorService,
    proxy_service: ProxyService,
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
) -> World:
    def static(name: str, **fields: Any) -> ConnectorCreate:
        return ConnectorCreate(name=name, credential_id=static_credential.id, **fields)

    isp = await connector_service.create(project.id, static("ISP"))
    off = await connector_service.create(project.id, static("Off", enabled=False))
    capped = await connector_service.create(
        project.id, static("Capped", traffic_config={"limit_bytes": LIMIT_BYTES, "action": "block"})
    )
    healthy, quarantined, unhealthy, draining = await proxy_service.bulk_add(
        rows(isp, ProxyStatus.HEALTHY, ProxyStatus.HEALTHY, ProxyStatus.UNHEALTHY, ProxyStatus.DRAINING)
    )
    (idle,) = await proxy_service.bulk_add(rows(off, ProxyStatus.HEALTHY, start=10))
    (capped_proxy,) = await proxy_service.bulk_add(rows(capped, ProxyStatus.HEALTHY, start=20))
    await save_history(sessions, capped, CAPPED_BYTES // 2, CAPPED_BYTES // 2)
    await store_block(redis_client, capped, period_end(capped))
    await redis_client.client.zadd(QUARANTINE_KEY, {quarantined.id: time.time() + 600})

    theirs = await connect(other_project, "Theirs", STATIC)
    await proxy_service.bulk_add(rows(theirs, ProxyStatus.HEALTHY, start=30))
    return World(
        project, other_project, isp, off, capped, healthy, quarantined, unhealthy, draining, idle, capped_proxy, theirs
    )


class TestProjects:
    async def test_the_pool_counts_the_routable_connectors_proxies(self, view: RuntimeView, world: World) -> None:
        """A disabled connector and one the traffic job blocked are left out; a quarantined proxy is not healthy."""
        project = await view.project(world.project.id)

        assert project is not None
        assert (project.project, project.credentials, project.connectors) == (world.project, 1, 3)
        assert project.pool == PoolCounts(total=4, healthy=1, quarantined=1, draining=1, terminating=0)
        # Quarantined healthy proxies count as unhealthy, as the dashboard has always shown them.
        assert project.pool.unhealthy == 2

    async def test_lists_every_project_with_its_own_counts(self, view: RuntimeView, world: World) -> None:
        projects = await view.projects()

        assert [p.project.id for p in projects] == [world.project.id, world.other.id]
        ours, theirs = projects
        assert ours.pool == PoolCounts(total=4, healthy=1, quarantined=1, draining=1, terminating=0)
        assert (theirs.credentials, theirs.connectors, theirs.pool) == (1, 1, PoolCounts(total=1, healthy=1))

    async def test_a_project_holding_nothing_counts_zero(self, view: RuntimeView, project: Project) -> None:
        empty = ProjectView(project=project, credentials=0, connectors=0, pool=PoolCounts())

        assert await view.project(project.id) == empty
        assert await view.projects() == [empty]
        assert await view.project("nope") is None


class TestCounters:
    async def test_totals_plus_the_window_not_flushed_yet(
        self, view: RuntimeView, project: Project, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        flushed = MetricDelta(request_count=10, success_count=9, failure_count=1, latency_sum_ms=1000.0)
        async with sessions() as session:
            await MetricsRepository(session).upsert_totals(PROJECT_WINDOWS, {project.id: flushed})
            await session.commit()
        window = MetricDelta(request_count=2, success_count=2, latency_sum_ms=200.0, bytes_sent=5)
        await write_window(redis_client, PROJECT_METRICS_KEY.format(project_id=project.id), window)

        counters = await view.counters(PROJECT_WINDOWS, [project.id, "never-counted"])

        assert counters[project.id] == StepMetricDelta(
            request_count=12, success_count=11, failure_count=1, latency_sum_ms=1200.0, bytes_sent=5
        )
        assert counters[project.id].avg_latency_ms == pytest.approx(1200 / 11)
        assert counters["never-counted"] == StepMetricDelta()

    async def test_a_tunnel_devices_counters_carry_its_name_resolution_signals(
        self, view: RuntimeView, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        async with sessions() as session:
            flushed = TunnelPeerMetricDelta(request_count=3, by_address=2)
            await MetricsRepository(session).upsert_totals(TUNNEL_PEER_WINDOWS, {"device-1": flushed})
            await session.commit()
        window = TunnelPeerMetricDelta(request_count=1, encrypted_dns_blocked=4)
        await write_window(redis_client, TUNNEL_PEER_METRICS_KEY.format(peer_id="device-1"), window)

        devices = await view.device_counters(["device-1", "device-2"])

        assert devices == {
            "device-1": TunnelPeerMetricDelta(request_count=4, by_address=2, encrypted_dns_blocked=4),
            "device-2": TunnelPeerMetricDelta(),
        }


class TestProxies:
    async def test_every_row_with_its_status_counters_health_and_quarantine(
        self, view: RuntimeView, world: World, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        flushed = MetricDelta(request_count=4, success_count=4, latency_sum_ms=400.0)
        async with sessions() as session:
            await MetricsRepository(session).upsert_totals(PROXY_WINDOWS, {world.healthy.id: flushed})
            await session.commit()
        window = MetricDelta(request_count=1, failure_count=1, latency_sum_ms=100.0, bytes_received=7)
        await write_window(redis_client, PROXY_METRICS_KEY.format(proxy_id=world.healthy.id), window)
        soft = HealthSoft(failures=1, latency_ms=153.5, checked_at=datetime(2026, 10, 17, 12, 0, 1))
        health_key = PROXY_HEALTH_KEY.format(proxy_id=world.healthy.id)
        await redis_client.client.hset(health_key, mapping=soft.model_dump(mode="json"))

        listed = await view.proxies(world.project.id)

        in_order = [world.healthy, world.quarantined, world.unhealthy, world.draining, world.idle, world.capped_proxy]
        assert [v.proxy.id for v in listed.proxies] == [p.id for p in in_order]
        # Only the healthy proxy of the enabled connector no limit blocks can take requests.
        assert listed.healthy == 1
        healthy, quarantined, unhealthy, draining = listed.proxies[:4]
        assert healthy.counters == MetricDelta(
            request_count=5, success_count=4, failure_count=1, latency_sum_ms=500.0, bytes_received=7
        )
        assert healthy.health == soft
        assert (healthy.quarantined, healthy.connector) == (False, world.isp)
        assert quarantined.quarantined is True
        assert 590 < quarantined.quarantine_remaining_seconds <= 600
        assert [v.proxy.status for v in (unhealthy, draining)] == [ProxyStatus.UNHEALTHY, ProxyStatus.DRAINING]
        assert (unhealthy.health, unhealthy.counters) == (None, MetricDelta())

    async def test_one_row_of_the_project(self, view: RuntimeView, world: World) -> None:
        one = await view.proxy(world.project.id, world.quarantined.id)

        assert one is not None
        assert (one.proxy, one.connector, one.quarantined) == (world.quarantined, world.isp, True)
        # A proxy of another project reads as absent there.
        assert await view.proxy(world.other.id, world.quarantined.id) is None


class TestConnectors:
    async def test_each_connector_with_its_pool_and_its_traffic(
        self, view: RuntimeView, world: World, redis_client: RedisClient
    ) -> None:
        window = MetricDelta(bytes_sent=30, bytes_received=12)
        await write_window(redis_client, CONNECTOR_METRICS_KEY.format(connector_id=world.isp.id), window)

        connectors = await view.connectors(world.project.id)

        assert [(c.connector.id, c.proxy_count) for c in connectors] == [
            (world.isp.id, 4), (world.off.id, 1), (world.capped.id, 1),
        ]
        isp, _, capped = connectors
        assert (isp.credential_name, isp.credential_type, isp.target) == ("Static", STATIC.type, None)
        # The window not flushed yet counts towards the period.
        assert (isp.traffic_usage.total_bytes, isp.traffic_usage.blocked) == (42, False)
        usage = capped.traffic_usage
        assert (usage.total_bytes, usage.status, usage.blocked, usage.percent) == (CAPPED_BYTES, "exceeded", True, 120)

    async def test_a_traffic_reset_shows_the_stored_block_until_a_worker_decides(
        self,
        view: RuntimeView,
        world: World,
        connector_service: ConnectorService,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """Usage counts from the reset at once; the block is the one gateways enforce, pending, until the traffic job decides."""
        blocked = await view.connector(world.project.id, world.capped.id)
        # Blocked by a decision no request waits on.
        assert blocked is not None and (blocked.traffic_usage.blocked, blocked.traffic_usage.pending) == (True, False)

        reset = await connector_service.reset_traffic(world.project.id, world.capped.id)

        capped = await view.connector(world.project.id, world.capped.id)

        assert capped is not None
        usage = capped.traffic_usage
        assert (usage.total_bytes, usage.status, usage.blocked, usage.pending) == (0, "ok", True, True)
        assert usage.reset_at == reset.traffic_reset_at == usage.period_start
        # Only the connector the reset asked about waits.
        assert [(c.connector.id, c.traffic_usage.pending) for c in await view.connectors(world.project.id)] == [
            (world.isp.id, False), (world.off.id, False), (world.capped.id, True),
        ]
        held = await view.project(world.project.id)
        assert held is not None and held.pool.total == 4

        await decide_traffic(sessions, redis_client, world.capped)

        released = await view.connector(world.project.id, world.capped.id)
        assert released is not None
        assert (released.traffic_usage.blocked, released.traffic_usage.pending) == (False, False)
        project = await view.project(world.project.id)
        assert project is not None and project.pool.total == 5

    async def test_a_connector_of_another_project_reads_as_absent(self, view: RuntimeView, world: World) -> None:
        assert await view.connector(world.other.id, world.isp.id) is None

    async def test_targets_come_from_the_cloud_config_or_the_providers_descriptor(
        self, view: RuntimeView, project: Project, connect: Connect, proxy_service: ProxyService
    ) -> None:
        cloud = await connect(project, "AWS", AWS)
        pool = await connect(project, "Pool", OXYLABS_POOL)
        dynamic = await connect(project, "Dynamic", OXYLABS_DYNAMIC)
        # Slot groups of an "All countries" pool, one per country clients asked for.
        for n, geo in enumerate(("DE", "DE", "US")):
            await proxy_service.bulk_add(rows(pool, ProxyStatus.HEALTHY, start=n, metadata={"geo": geo}))

        targets = {c.connector.id: c.target for c in await view.connectors(project.id)}

        assert targets[cloud.id] == ProxyTarget(total=AWS.connector["max_proxies"])
        assert targets[pool.id] == ProxyTarget(total=6, per_country=2, countries=["DE", "US"], on_demand=["DE", "US"])
        assert targets[dynamic.id] == ProxyTarget(total=1, dynamic=True, exit_sample_percent=5)

    async def test_exit_coverage_of_dynamic_connectors(
        self,
        view: RuntimeView,
        project: Project,
        static_connector: Connector,
        connect: Connect,
        project_service: ProjectService,
    ) -> None:
        dynamic = await connect(project, "Dynamic", OXYLABS_DYNAMIC)
        # A session pool's exits are its rows', which health checks see.
        pool = await connect(project, "Pool", OXYLABS_POOL)
        ids = [dynamic.id, pool.id, static_connector.id, "gone"]

        # Without preflight nothing sees a dynamic connector's exits but Detect or a plugin.
        assert await view.exit_coverage(ids) == {dynamic.id: ExitCoverage(preflight_on=False, sampled_percent=0)}

        await project_service.update(project.id, ProjectUpdate(location_preflight=True))
        assert await view.exit_coverage(ids) == {dynamic.id: ExitCoverage(preflight_on=True, sampled_percent=5)}

    async def test_a_provider_its_config_cannot_build_reads_as_none(
        self, view: RuntimeView, project: Project, connect: Connect, registry: ProviderRegistry
    ) -> None:
        """A config error (a ValueError) shows the connector without a target or a coverage; any other is raised."""
        dynamic = await connect(project, "Dynamic", OXYLABS_DYNAMIC)
        fail_to_build(registry, "oxylabs", ValueError("unknown proxy type"))

        (row,) = await view.connectors(project.id)
        assert (row.connector.id, row.target) == (dynamic.id, None)
        assert await view.exit_coverage([dynamic.id]) == {}

        fail_to_build(registry, "oxylabs", RuntimeError("the vendor SDK is broken"))
        with pytest.raises(RuntimeError):
            await view.connectors(project.id)
        with pytest.raises(RuntimeError):
            await view.exit_coverage([dynamic.id])


class TestScaling:
    async def test_cloud_instances_and_demand_against_the_healthy_pool(
        self,
        view: RuntimeView,
        world: World,
        demand: FakeDemand,
        connect: Connect,
        proxy_service: ProxyService,
    ) -> None:
        cloud = await connect(world.project, "AWS", AWS)
        await proxy_service.bulk_add(
            rows(cloud, ProxyStatus.HEALTHY, ProxyStatus.INITIALIZING, ProxyStatus.TERMINATING, start=40)
        )

        scaling = await view.scaling(world.project.id)

        assert scaling is not None
        assert (scaling.demand_level, scaling.requests_per_minute, scaling.rate_per_proxy) == ("MEDIUM", 30.0, 15.0)
        assert (scaling.current_instances, scaling.healthy_instances, scaling.terminating_instances) == (3, 1, 1)
        assert (scaling.min_instances, scaling.max_instances, scaling.draining_instances) == (1, 4, 0)
        # Demand is weighed against the project's whole healthy pool: the static proxy and the instance.
        assert demand.asked == [(world.project.id, 2)]
        assert await view.scaling("nope") is None

    async def test_the_demand_trackers_read(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        registry: ProviderRegistry,
        project: Project,
    ) -> None:
        view = RuntimeView(sessions, RuntimeStateReader(redis_client), registry, DemandTracker(redis_client))

        scaling = await view.scaling(project.id)

        assert scaling is not None
        assert (scaling.demand_level, scaling.requests_per_minute) == ("LOW", 0.0)


class TestQuarantines:
    async def test_the_quarantines_redis_holds_by_proxy(self, view: RuntimeView, world: World) -> None:
        """What a preview of the project's routing leaves out, each with when its quarantine ends."""
        quarantined = await view.quarantined()

        assert list(quarantined) == [world.quarantined.id]
        assert 590 < (quarantined[world.quarantined.id] - utc_now()).total_seconds() <= 600


class TestTrafficBlocks:
    """Blocked is the traffic job's stored decision, which gateways enforce; usage is computed from the counts."""

    async def test_blocked_exactly_when_the_hash_holds_a_block_still_in_its_period(
        self, view: RuntimeView, world: World, redis_client: RedisClient
    ) -> None:
        # The capped connector's usage is over its limit with no block stored, the ISP's has no limit and one stored.
        await redis_client.client.hdel(TRAFFIC_BLOCKED_KEY, world.capped.id)
        await store_block(redis_client, world.isp, period_end(world.isp))
        await store_block(redis_client, world.theirs, period_end(world.theirs))

        isp, _off, capped = await view.connectors(world.project.id)
        one = await view.connector(world.project.id, world.capped.id)

        assert (isp.traffic_usage.status, isp.traffic_usage.blocked) == ("ok", True)
        usage = capped.traffic_usage
        assert (usage.total_bytes, usage.status, usage.percent, usage.blocked) == (CAPPED_BYTES, "exceeded", 120, False)
        assert one is not None and one.traffic_usage == usage
        # What a routing preview reads: every stored block with its end, for the compile to keep its project's.
        assert await view.blocked() == {world.isp.id: period_end(world.isp), world.theirs.id: period_end(world.theirs)}
        # The blocked connector's proxies leave the pool and the routable count; the capped one's come back.
        project = await view.project(world.project.id)
        assert project is not None and project.pool == PoolCounts(total=1, healthy=1)
        assert (await view.proxies(world.project.id)).healthy == 1

    @pytest.mark.parametrize("ended", [timedelta(hours=1), timedelta(0)], ids=["an_hour_ago", "at_the_read"])
    async def test_an_entry_past_its_period_reads_not_blocked(
        self,
        ended: timedelta,
        view: RuntimeView,
        world: World,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ended block stays in the hash until the traffic job decides again; one ending at the read is over."""
        now = utc_now()
        monkeypatch.setattr("api.services.read_model.utc_now", lambda: now)
        monkeypatch.setattr("api.feed.state.utc_now", lambda: now)
        await store_block(redis_client, world.capped, now - ended)

        capped = await view.connector(world.project.id, world.capped.id)

        assert capped is not None
        assert (capped.traffic_usage.status, capped.traffic_usage.blocked) == ("exceeded", False)
        project = await view.project(world.project.id)
        assert project is not None and project.pool.total == 5
        assert (await view.proxies(world.project.id)).healthy == 2
        # A routing preview reads it all the same, and its router judges it as gateways do.
        assert await view.blocked() == {world.capped.id: now - ended}
        # A microsecond more and it is in its period.
        await store_block(redis_client, world.capped, now + timedelta(microseconds=1))
        still = await view.connector(world.project.id, world.capped.id)
        assert still is not None and still.traffic_usage.blocked is True


class TestWithoutRedis:
    async def test_definitions_are_shown_without_the_soft_state(
        self, sessions: async_sessionmaker[AsyncSession], registry: ProviderRegistry, world: World
    ) -> None:
        """A page shows less rather than failing: no quarantine, no traffic block, no soft health, no windows."""
        unreachable = RedisClient("redis://127.0.0.1:1/0", socket_timeout=0.5)
        await unreachable.connect()
        view = RuntimeView(sessions, RuntimeStateReader(unreachable), registry, DemandTracker(unreachable))
        try:
            listed = await view.proxies(world.project.id)
            project = await view.project(world.project.id)
            with structlog.testing.capture_logs() as usage_logs:
                capped = await view.connector(world.project.id, world.capped.id)
            scaling = await view.scaling(world.project.id)
            with structlog.testing.capture_logs() as load_logs:
                loads = await view.load(24, 3600)
            with structlog.testing.capture_logs() as logs:
                quarantined = await view.quarantined()
                blocked = await view.blocked()
        finally:
            await unreachable.close()

        assert len(listed.proxies) == 6
        assert not any(v.quarantined or v.health for v in listed.proxies)
        # Every enabled connector's healthy proxies count, the capped connector's too.
        assert listed.healthy == 3
        assert project is not None and project.pool == PoolCounts(total=5, healthy=3, draining=1)
        # The flushed history alone still shows the limit reached, with no block and nothing pending.
        assert capped is not None
        usage = capped.traffic_usage
        assert (usage.total_bytes, usage.status, usage.blocked, usage.pending) == (CAPPED_BYTES, "exceeded", False, False)
        assert scaling is not None and scaling.demand_level == "LOW"
        # Every project's load from the flushed history alone, at no rate.
        [load] = [load for load in loads if load.project_id == world.project.id]
        assert (load.requests_per_minute, len(load.trend)) == (0.0, 24)
        assert [log["state"] for log in load_logs] == ["demand"]
        assert (quarantined, blocked) == ({}, {})
        assert [(log["event"], log["log_level"], log["state"]) for log in logs] == [
            ("Runtime state unavailable, shown without it", "warning", "quarantines"),
            ("Runtime state unavailable, shown without it", "warning", "traffic blocks"),
        ]
        assert sorted(log["state"] for log in usage_logs) == [
            "connector windows", "pending traffic decisions", "traffic blocks",
        ]

    async def test_a_read_cut_by_a_deadline_is_logged_by_its_type(
        self, sessions: async_sessionmaker[AsyncSession], registry: ProviderRegistry
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type."""
        state = MagicMock(spec=RuntimeStateReader)
        state.quarantined = AsyncMock(side_effect=TimeoutError)
        view = RuntimeView(sessions, state, registry, MagicMock(spec=DemandTracker))

        with structlog.testing.capture_logs() as logs:
            assert await view.quarantined() == {}

        assert [(log["event"], log["state"], log["error"]) for log in logs] == [
            ("Runtime state unavailable, shown without it", "quarantines", "TimeoutError")
        ]


class TrackedSessions:
    """A session factory that knows how many of its sessions are open."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.open = 0

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self._tracked()

    @asynccontextmanager
    async def _tracked(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            self.open += 1
            try:
                yield session
            finally:
                self.open -= 1


class WatchedState(RuntimeStateReader):
    """The soft state reader, noting each Redis read made while a Postgres session was open."""

    def __init__(self, redis_client: RedisClient, sessions: TrackedSessions) -> None:
        super().__init__(redis_client)
        self._sessions = sessions
        self.inside: list[str] = []

    def _note(self, what: str) -> None:
        if self._sessions.open:
            self.inside.append(what)

    async def quarantined(self) -> dict[str, datetime]:
        self._note("quarantines")
        return await super().quarantined()

    async def blocked(self) -> dict[str, datetime]:
        self._note("traffic blocks")
        return await super().blocked()

    async def pending(self) -> dict[str, str]:
        self._note("pending traffic decisions")
        return await super().pending()

    async def health(self, proxy_ids: Iterable[str]) -> dict[str, HealthSoft]:
        self._note("health")
        return await super().health(proxy_ids)

    async def windows(self, kind: EntityWindows, ids: Iterable[str]) -> dict[str, MetricDelta]:
        self._note(f"{kind.name} windows")
        return await super().windows(kind, ids)


def _reads(view: RuntimeView, world: World) -> dict[str, Callable[[], Awaitable[object]]]:
    """Every read a route makes of the read model, on the world's project."""
    project, isp = world.project.id, world.isp.id
    return {
        "projects": view.projects,
        "project": lambda: view.project(project),
        "scaling": lambda: view.scaling(project),
        "connectors": lambda: view.connectors(project),
        "connector": lambda: view.connector(project, isp),
        "blocked": view.blocked,
        "quarantined": view.quarantined,
        "proxies": lambda: view.proxies(project),
        "proxy": lambda: view.proxy(project, world.healthy.id),
        "counters": lambda: view.counters(PROXY_WINDOWS, [world.healthy.id]),
    }


class TestReadsHoldNoConnectionOnRedis:
    async def test_redis_is_read_only_once_the_postgres_session_is_closed(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        registry: ProviderRegistry,
        demand: FakeDemand,
        world: World,
    ) -> None:
        """A slow Redis must not pin a pooled connection idle in a transaction while other requests wait for one."""
        tracked = TrackedSessions(sessions)
        state = WatchedState(redis_client, tracked)
        view = RuntimeView(tracked, state, registry, demand)

        for name, read in _reads(view, world).items():
            await read()
            assert state.inside == [], name


class TestConnectorsCountTheirProxies:
    async def test_the_connector_pages_count_proxies_without_loading_their_rows(
        self, view: RuntimeView, world: World, connect: Connect, proxy_service: ProxyService, db_engine: AsyncEngine
    ) -> None:
        """A 10,000-slot pool's page would otherwise move every row's metadata to show a number."""
        pool = await connect(world.project, "Pool", OXYLABS_POOL)
        await proxy_service.bulk_add(rows(pool, *[ProxyStatus.HEALTHY] * 2, start=40, metadata={"geo": "DE"}))

        with counted_selects(db_engine) as selects:
            listed = await view.connectors(world.project.id)
            one = await view.connector(world.project.id, pool.id)

        assert [c.proxy_count for c in listed] == [4, 1, 1, 2]
        assert one is not None and one.proxy_count == 2 and one.target is not None and one.target.countries == ["DE"]
        assert [statement for statement in selects if "proxies.host" in statement] == []


class TestBatchedReads:
    """SPEC 8: no read costs a statement or a round trip per row; fifty rows cost what two do."""

    @pytest.mark.parametrize("name", ["projects", "project", "scaling", "connectors", "connector", "proxies"])
    async def test_fifty_rows_cost_what_two_do(
        self,
        name: str,
        view: RuntimeView,
        world: World,
        proxy_service: ProxyService,
        connect: Connect,
        db_engine: AsyncEngine,
    ) -> None:
        pool = await connect(world.project, "Pool", OXYLABS_POOL)
        await proxy_service.bulk_add(rows(pool, ProxyStatus.HEALTHY, start=40, metadata={"geo": "DE"}))
        read = _reads(view, world)[name]

        async def cost() -> tuple[int, int]:
            with counted_selects(db_engine) as selects, counted_round_trips() as trips:
                await read()
            return len(selects), len(trips)

        # The first read opens the clients' connections, which costs round trips of its own.
        await read()
        few = await cost()
        countries = ["DE", "US", "FR"]
        more = [ProxyStatus.HEALTHY, ProxyStatus.UNHEALTHY] * 24
        await proxy_service.bulk_add(rows(world.isp, *more, start=100))
        for n, country in enumerate(countries * 4):
            await proxy_service.bulk_add(rows(pool, ProxyStatus.HEALTHY, start=200 + n, metadata={"geo": country}))

        assert await cost() == few
