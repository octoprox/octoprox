# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's read model: stored definitions joined with the runtime state other roles keep in Redis.

Every read is batched, Postgres first and Redis after the session closes; without Redis a view shows less, logged.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from redis.exceptions import RedisError

from api.core import utc_now
from api.core.logging import error_text
from api.core.stats import MetricDelta, TunnelPeerMetricDelta
from api.core.traffic_limiter import TrafficHistory
from api.db.redis import PROXY_WINDOWS, TUNNEL_PEER_WINDOWS
from api.db.repository import (
    ConnectorRepository,
    ConnectorStatusCount,
    CredentialRepository,
    MetricsRepository,
    ProjectCounts,
    ProjectRepository,
    ProxyPlacement,
    ProxyRepository,
)
from api.geo.models import ExitCoverage
from api.models.connector import ProxyTarget, TrafficUsage
from api.models.proxy import ProxyStatus
from api.providers.sdk.provider import DescriptorProvider
from api.providers.sdk.strategies import META_GEO_COUNTRY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.demand_tracker import DemandTracker
    from api.db.redis import TotalledWindows
    from api.db.session import SessionFactory
    from api.feed.state import HealthSoft, RuntimeStateReader
    from api.models.connector import CloudConnectorConfig, Connector
    from api.models.credential import Credential
    from api.models.project import Project
    from api.models.proxy import Proxy
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()


# --- Views --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolCounts:
    """A project's pool as the dashboard counts it: the proxies of its enabled connectors no traffic limit blocks."""

    total: int = 0
    # Healthy and not quarantined: what requests can be routed to.
    healthy: int = 0
    # Quarantined by a rate limit, whatever their status.
    quarantined: int = 0
    draining: int = 0
    terminating: int = 0

    @property
    def unhealthy(self) -> int:
        """What is neither routable nor on its way out (quarantined healthy proxies count here)."""
        return self.total - self.healthy - self.draining - self.terminating


@dataclass(frozen=True)
class ProjectView:
    """A project with what it holds."""

    project: Project
    credentials: int
    connectors: int
    pool: PoolCounts

    @classmethod
    def of(cls, project: Project, counts: ProjectCounts | None, pool: PoolCounts) -> ProjectView:
        """A project with its counts; a project holding nothing has no counts row."""
        held = counts or ProjectCounts()
        return cls(project=project, credentials=held.credentials, connectors=held.connectors, pool=pool)


@dataclass(frozen=True)
class ProjectLoad:
    """A project's load: its request rate now, and its requests over a range, in total and as a trend."""

    project_id: str
    # Over the demand window, rejected requests included: the rate the Overview and scaling read.
    requests_per_minute: float
    # The range's counters, from the flushed history.
    counters: MetricDelta
    # Requests per bucket, oldest first, the newest ending at the last flush.
    trend: tuple[int, ...]


@dataclass(frozen=True)
class ScalingView:
    """A project's demand and its cloud instances, as the scaling metrics show them."""

    # The demand level's name in capitals (LOW, MEDIUM, HIGH).
    demand_level: str
    requests_per_minute: float
    rate_per_proxy: float
    # Instances of the project's enabled cloud connectors that no traffic limit blocks.
    current_instances: int
    healthy_instances: int
    # Sums over the project's enabled cloud connectors.
    min_instances: int
    max_instances: int
    draining_instances: int
    terminating_instances: int


@dataclass(frozen=True)
class ConnectorRow:
    """A connector with its credential, its pool and its traffic this period."""

    connector: Connector
    credential_name: str | None
    credential_type: str | None
    # Every proxy of the connector, whatever its status.
    proxy_count: int
    # How many proxies the connector is meant to hold; None when it has no target of its own.
    target: ProxyTarget | None
    traffic_usage: TrafficUsage

    @classmethod
    def of(
        cls,
        connector: Connector,
        credential: Credential | None,
        proxy_count: int,
        target: ProxyTarget | None,
        usage: TrafficUsage,
    ) -> ConnectorRow:
        """A connector with its credential's name and type, its proxies counted, its target and its usage."""
        return cls(
            connector=connector,
            credential_name=credential.name if credential else None,
            credential_type=credential.type if credential else None,
            proxy_count=proxy_count,
            target=target,
            traffic_usage=usage,
        )


@dataclass(frozen=True)
class ProxyView:
    """A proxy row with its runtime state."""

    # The stored row: its definition and its status column.
    proxy: Proxy
    # Its connector; None only while a delete is under way.
    connector: Connector | None
    # All-time totals plus the window not flushed yet.
    counters: MetricDelta
    # Failures in a row and latency from the last health check; None when not checked lately.
    health: HealthSoft | None
    # Seconds left of a rate-limit quarantine, 0 when none.
    quarantine_remaining_seconds: float

    @property
    def quarantined(self) -> bool:
        """Whether a rate limit has the proxy in quarantine now."""
        return self.quarantine_remaining_seconds > 0


@dataclass(frozen=True)
class ProxyListView:
    """A project's proxies, every one, with how many requests can be routed to."""

    proxies: list[ProxyView]
    # Healthy, not quarantined, on an enabled connector no traffic limit blocks.
    healthy: int


@dataclass(frozen=True)
class _PoolRows:
    """What Postgres holds for a read's pool counts, read in the caller's session; ``RuntimeView._pools`` adds Redis."""

    # The project read (None: every one).
    project_id: str | None
    # The connectors whose proxies count unless a traffic limit blocks them.
    enabled: list[Connector]
    counts: list[ConnectorStatusCount]


@dataclass(frozen=True)
class _Pools:
    """The inputs every pool count of a read is computed from, read once."""

    counts: list[ConnectorStatusCount]
    # Quarantined proxy -> where it sits.
    quarantined: dict[str, ProxyPlacement]
    # Connectors whose proxies are counted: enabled, and not blocked by a traffic limit.
    counted: set[str]

    def pool(self, connector_ids: Collection[str]) -> PoolCounts:
        """The pool of the given connectors (a project's)."""
        counted = self.counted.intersection(connector_ids)
        statuses = self.statuses(counted)
        quarantined = [placement for placement in self.quarantined.values() if placement.connector_id in counted]
        return PoolCounts(
            total=sum(statuses.values()),
            healthy=statuses[ProxyStatus.HEALTHY] - sum(1 for p in quarantined if p.status == ProxyStatus.HEALTHY),
            quarantined=len(quarantined),
            draining=statuses[ProxyStatus.DRAINING],
            terminating=statuses[ProxyStatus.TERMINATING],
        )

    def statuses(self, connector_ids: Collection[str]) -> Counter[ProxyStatus]:
        """Proxies per status over the given connectors."""
        statuses: Counter[ProxyStatus] = Counter()
        for row in self.counts:
            if row.connector_id in connector_ids:
                statuses[row.status] += row.count
        return statuses


class RuntimeView:
    """What the api's routes show, read from Postgres and Redis in batches."""

    def __init__(
        self,
        session_factory: SessionFactory,
        state_reader: RuntimeStateReader,
        provider_registry: ProviderRegistry,
        demand_tracker: DemandTracker,
    ) -> None:
        self._session_factory = session_factory
        self._state = state_reader
        self._registry = provider_registry
        self._demand = demand_tracker

    # --- Projects ---------------------------------------------------------------------------

    async def projects(self) -> list[ProjectView]:
        """Every project with what it holds, oldest first."""
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
            counts = await ProjectRepository(session).get_counts()
            connectors = await ConnectorRepository(session).get_all()
            held = await self._pool_rows(session, connectors, None)
        pools = await self._pools(held)
        by_project: dict[str, set[str]] = {}
        for connector in connectors:
            by_project.setdefault(connector.project_id, set()).add(connector.id)
        return [
            ProjectView.of(project, counts.get(project.id), pools.pool(by_project.get(project.id, set())))
            for project in sorted(projects, key=lambda p: (p.created_at, p.id))
        ]

    async def project(self, project_id: str) -> ProjectView | None:
        """One project with what it holds; None when there is no such project."""
        async with self._session_factory() as session:
            projects = ProjectRepository(session)
            project = await projects.get_by_id(project_id)
            if project is None:
                return None
            counts = await projects.get_counts(project_id)
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            held = await self._pool_rows(session, connectors, project_id)
        pools = await self._pools(held)
        return ProjectView.of(project, counts.get(project_id), pools.pool({c.id for c in connectors}))

    async def load(self, buckets: int, bucket_seconds: int) -> list[ProjectLoad]:
        """Every project's load over the last ``buckets`` buckets of ``bucket_seconds``, oldest project first.

        The counters and the trend are the flushed history alone: a flush holds the minute before it, so every bucket
        holds its own span's traffic and the newest ends at the last flush, at most a minute ago; the rate covers now.
        """
        now = utc_now()
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
            rows = await MetricsRepository(session).get_projects_metrics_buckets(now, buckets, bucket_seconds)
        ids = [project.id for project in sorted(projects, key=lambda p: (p.created_at, p.id))]
        rates = await _soft(self._demand.get_requests_per_minute_of(ids), {}, "demand")
        counters = {project_id: MetricDelta() for project_id in ids}
        trends = {project_id: [0] * buckets for project_id in ids}
        for project_id, bucket, delta in rows:
            # A project created after the project list was read, its first rows flushed before the history read.
            if project_id not in counters:
                continue
            counters[project_id].merge(delta)
            trends[project_id][buckets - 1 - min(max(bucket, 0), buckets - 1)] += delta.request_count
        return [
            ProjectLoad(
                project_id=project_id,
                requests_per_minute=rates.get(project_id, 0.0),
                counters=counters[project_id],
                trend=tuple(trends[project_id]),
            )
            for project_id in ids
        ]

    async def scaling(self, project_id: str) -> ScalingView | None:
        """A project's demand and cloud instances; None when there is no such project.

        Instances are the proxies of the project's enabled cloud connectors
        (AWS, GCP, Azure) that no traffic limit blocks; the demand level
        weighs the project's request rate against its whole healthy pool.
        """
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                return None
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            held = await self._pool_rows(session, connectors, project_id)
        pools = await self._pools(held)
        clouds: dict[str, CloudConnectorConfig] = {}
        for connector in connectors:
            config = connector.cloud_config if connector.enabled else None
            if config is not None:
                clouds[connector.id] = config
        instances = pools.statuses(pools.counted.intersection(clouds))
        pool = pools.pool({c.id for c in connectors})
        demand = await _soft(
            self._demand.get_demand_info(project_id, pool.healthy),
            {"demand_level": "low", "requests_per_minute": 0.0, "rate_per_proxy": 0.0},
            "demand",
        )
        level = demand.get("demand_level", "low")
        return ScalingView(
            demand_level=str(level).upper(),
            requests_per_minute=float(demand.get("requests_per_minute", 0.0)),
            rate_per_proxy=float(demand.get("rate_per_proxy", 0.0)),
            current_instances=sum(instances.values()),
            healthy_instances=instances[ProxyStatus.HEALTHY],
            min_instances=sum(config.min_proxies for config in clouds.values()),
            max_instances=sum(config.max_proxies for config in clouds.values()),
            draining_instances=instances[ProxyStatus.DRAINING],
            terminating_instances=instances[ProxyStatus.TERMINATING],
        )

    # --- Counters ---------------------------------------------------------------------------

    async def counters(self, kind: TotalledWindows, ids: Iterable[str]) -> dict[str, MetricDelta]:
        """All-time counters of each of ``ids`` of ``kind`` (proxies, projects or tunnel devices): totals plus the current window.

        Every id is answered, one nothing was counted on reading as zero, in the kind's counts type: a tunnel device's
        is a :class:`TunnelPeerMetricDelta`.
        """
        wanted = list(dict.fromkeys(ids))
        async with self._session_factory() as session:
            totals = await MetricsRepository(session).get_totals(kind, wanted)
        windows = await _soft(self._state.windows(kind, wanted), {}, f"{kind.name} windows")
        return {entity_id: kind.counts.summed(totals.get(entity_id), windows.get(entity_id)) for entity_id in wanted}

    async def device_counters(self, peer_ids: Iterable[str]) -> dict[str, TunnelPeerMetricDelta]:
        """All-time counters of tunnel devices, name-resolution signals included; zero for a device never counted."""
        counters = await self.counters(TUNNEL_PEER_WINDOWS, peer_ids)
        return {peer_id: TunnelPeerMetricDelta.summed(delta) for peer_id, delta in counters.items()}

    # --- Connectors -------------------------------------------------------------------------

    async def connectors(self, project_id: str) -> list[ConnectorRow]:
        """Every connector of a project with its pool, target and traffic, oldest first.

        Proxies are counted, not loaded: of a pool's rows, its target depends
        only on the country groups they carry.
        """
        now = utc_now()
        async with self._session_factory() as session:
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            credentials = {c.id: c for c in await CredentialRepository(session).get_by_project(project_id)}
            sizes = _sizes(await ProxyRepository(session).count_by_connector(project_id))
            targets = await self._targets(session, connectors, credentials)
            traffic = await TrafficHistory.load(session, connectors, now)
        usage = await self._usage(traffic)
        return [
            ConnectorRow.of(c, credentials.get(c.credential_id), sizes[c.id], targets[c.id], usage[c.id])
            for c in sorted(connectors, key=lambda c: (c.created_at, c.id))
        ]

    async def connector(self, project_id: str, connector_id: str) -> ConnectorRow | None:
        """One connector of a project with its pool, target and traffic; None when the project has no such connector."""
        now = utc_now()
        async with self._session_factory() as session:
            found = await ConnectorRepository(session).get_with_credential(connector_id, project_id=project_id)
            if found is None:
                return None
            connector, credential = found
            sizes = _sizes(await ProxyRepository(session).count_by_connector(connector_id=connector_id))
            targets = await self._targets(session, [connector], {credential.id: credential})
            traffic = await TrafficHistory.load(session, [connector], now)
        usage = await self._usage(traffic)
        return ConnectorRow.of(connector, credential, sizes[connector_id], targets[connector_id], usage[connector_id])

    async def quarantined(self) -> dict[str, datetime]:
        """Proxy ID -> quarantine end, as Redis holds them; none, logged, while Redis cannot be read."""
        return await _soft(self._state.quarantined(), {}, "quarantines")

    async def blocked(self) -> dict[str, datetime]:
        """Connector ID -> traffic block end, as the traffic job stored them, ended ones included; none, logged, while Redis cannot be read."""
        return await _soft(self._state.blocked(), {}, "traffic blocks")

    async def exit_coverage(self, connector_ids: Iterable[str]) -> dict[str, ExitCoverage]:
        """The exit coverage of each dynamic-sessions connector among ``connector_ids``; others are absent."""
        async with self._session_factory() as session:
            connectors = await ConnectorRepository(session).get_many_with_credentials(connector_ids)
            owners = {connector.project_id for connector, _credential in connectors}
            projects = {p.id: p for p in await ProjectRepository(session).get_many(owners)}
        providers = self._providers(connectors)
        coverage: dict[str, ExitCoverage] = {}
        for connector, _credential in connectors:
            provider = providers[connector.id]
            if provider is None or not provider.is_dynamic:
                continue
            project = projects.get(connector.project_id)
            preflight_on = project is not None and project.location_preflight
            coverage[connector.id] = ExitCoverage(
                preflight_on=preflight_on, sampled_percent=provider.exit_sample_percent if preflight_on else 0
            )
        return coverage

    # --- Proxies ----------------------------------------------------------------------------

    async def proxies(self, project_id: str) -> ProxyListView:
        """Every proxy of a project, disabled connectors' included, in creation order."""
        now = utc_now()
        async with self._session_factory() as session:
            rows = await ProxyRepository(session).get_by_project(project_id)
            connectors = {c.id: c for c in await ConnectorRepository(session).get_by_project(project_id)}
            totals = await MetricsRepository(session).get_totals(PROXY_WINDOWS, [p.id for p in rows])
        views, blocked = await asyncio.gather(self._proxy_views(rows, connectors, totals, now), self._blocked_now())
        routable = {c.id for c in connectors.values() if c.enabled and c.id not in blocked}
        healthy = sum(
            1
            for view in views
            if view.proxy.status == ProxyStatus.HEALTHY and not view.quarantined and view.proxy.connector_id in routable
        )
        return ProxyListView(proxies=views, healthy=healthy)

    async def proxy(self, project_id: str, proxy_id: str) -> ProxyView | None:
        """One proxy of a project with its runtime state; None when the project has no such proxy."""
        now = utc_now()
        async with self._session_factory() as session:
            proxy = await ProxyRepository(session).get_in_project(project_id, proxy_id)
            if proxy is None:
                return None
            connector = await ConnectorRepository(session).get_by_id(proxy.connector_id)
            totals = await MetricsRepository(session).get_totals(PROXY_WINDOWS, [proxy_id])
        connectors = {connector.id: connector} if connector is not None else {}
        return (await self._proxy_views([proxy], connectors, totals, now))[0]

    # --- Shared reads -----------------------------------------------------------------------

    async def _pool_rows(
        self, session: AsyncSession, connectors: Sequence[Connector], project_id: str | None
    ) -> _PoolRows:
        """What Postgres holds for the pool counts of ``connectors`` (a project's, or every one)."""
        return _PoolRows(
            project_id=project_id,
            enabled=[connector for connector in connectors if connector.enabled],
            counts=await ProxyRepository(session).count_by_connector(project_id),
        )

    async def _pools(self, rows: _PoolRows) -> _Pools:
        """The pool counts' inputs: ``rows`` with the quarantines and traffic blocks read from Redis.

        Called once the caller's session is closed; where the quarantined proxies sit is read after Redis, in a session
        of its own, and only when some proxy is quarantined.
        """
        quarantined, blocked = await asyncio.gather(self.quarantined(), self._blocked_now())
        placements: dict[str, ProxyPlacement] = {}
        if quarantined:
            async with self._session_factory() as session:
                placements = await ProxyRepository(session).placements(quarantined, rows.project_id)
        counted = {connector.id for connector in rows.enabled if connector.id not in blocked}
        return _Pools(counts=rows.counts, quarantined=placements, counted=counted)

    async def _usage(self, traffic: TrafficHistory) -> dict[str, TrafficUsage]:
        """Each connector's traffic in its current window against its limit, with the block the traffic job stored and whether one is pending.

        Redis is read once the caller's session is closed; without it the history alone is shown, with no block and nothing pending.
        """
        current, blocked, pending = await asyncio.gather(
            _soft(traffic.with_live(self._state), traffic.plus({}), "connector windows"),
            self._blocked_now(),
            _soft(self._state.pending(), {}, "pending traffic decisions"),
        )
        return {
            connector_id: window.usage(blocked=connector_id in blocked, pending=connector_id in pending)
            for connector_id, window in current.items()
        }

    async def _blocked_now(self) -> set[str]:
        """Connectors whose stored traffic block is still in its period; none, logged, without Redis."""
        return await _soft(self._state.blocked_now(), set(), "traffic blocks")

    async def _targets(
        self, session: AsyncSession, connectors: Sequence[Connector], credentials: Mapping[str, Credential]
    ) -> dict[str, ProxyTarget | None]:
        """How many proxies each connector is meant to hold, or None when it has no target of its own.

        Cloud connectors scale to ``max_proxies``; provider connectors count per country group, from their descriptor.
        """
        providers = self._providers((c, credentials.get(c.credential_id)) for c in connectors)
        groups = await ProxyRepository(session).metadata_values(
            [connector_id for connector_id, provider in providers.items() if provider is not None], META_GEO_COUNTRY
        )
        targets: dict[str, ProxyTarget | None] = {}
        for connector in connectors:
            cloud, provider = connector.cloud_config, providers[connector.id]
            if cloud is not None:
                targets[connector.id] = ProxyTarget(total=cloud.max_proxies)
            elif provider is not None:
                targets[connector.id] = provider.target_for_groups(sorted(groups.get(connector.id, ())))
            else:
                targets[connector.id] = None
        return targets

    async def _proxy_views(
        self,
        rows: Sequence[Proxy],
        connectors: Mapping[str, Connector],
        totals: Mapping[str, MetricDelta],
        now: datetime,
    ) -> list[ProxyView]:
        """The rows with their soft state: windows, soft health and quarantines read side by side."""
        ids = [proxy.id for proxy in rows]
        windows, health, quarantined = await asyncio.gather(
            _soft(self._state.windows(PROXY_WINDOWS, ids), {}, "proxy windows"),
            _soft(self._state.health(ids), {}, "soft health"),
            self.quarantined(),
        )
        views: list[ProxyView] = []
        for proxy in rows:
            until = quarantined.get(proxy.id)
            views.append(
                ProxyView(
                    proxy=proxy,
                    connector=connectors.get(proxy.connector_id),
                    counters=MetricDelta.summed(totals.get(proxy.id), windows.get(proxy.id)),
                    health=health.get(proxy.id),
                    quarantine_remaining_seconds=max(0.0, (until - now).total_seconds()) if until else 0.0,
                )
            )
        return views

    def _providers(
        self, connectors: Iterable[tuple[Connector, Credential | None]]
    ) -> dict[str, DescriptorProvider | None]:
        """Each connector's descriptor provider by connector ID, built from its credential; None where it has none.

        A config the provider cannot be built from (a ValueError) reads as no provider.
        """
        providers: dict[str, DescriptorProvider | None] = {}
        for connector, credential in connectors:
            try:
                providers[connector.id] = self._registry.descriptor_provider(connector, credential)
            except ValueError:
                providers[connector.id] = None
        return providers


async def _soft[T](read: Awaitable[T], unavailable: T, what: str) -> T:
    """``read`` from Redis, else ``unavailable`` (logged): without Redis a page shows less instead of failing."""
    try:
        return await read
    except (RedisError, OSError) as exc:
        logger.warning("Runtime state unavailable, shown without it", state=what, error=error_text(exc))
        return unavailable


def _sizes(counts: Iterable[ConnectorStatusCount]) -> Counter[str]:
    """Every connector's proxies, whatever their status, from the per-status counts."""
    sizes: Counter[str] = Counter()
    for row in counts:
        sizes[row.connector_id] += row.count
    return sizes
