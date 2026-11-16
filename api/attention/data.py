# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Needs attention checks read, and the reads that load it: a fixed number of statements for one project, and
as many for every project at once, however many connectors they hold.

Postgres first, Redis after each session closes, as the read model reads; the routing preview is the router the
Countries page asks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from api.attention.checks import REPUTATION_RECENT, REPUTATION_SITES, WINDOW
from api.core import utc_now
from api.core.traffic_limiter import TrafficHistory
from api.db.repository import (
    ConnectorRepository,
    ConnectorStatusCount,
    FailureWindowRow,
    MetricsRepository,
    ProjectRepository,
)
from api.db.reputation_repository import ExitReputationRepository
from api.models.proxy import ProxyStatus
from api.reputation.split import ReputationSplit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory
    from api.models.connector import Connector, TrafficUsage
    from api.models.project import Project
    from api.providers.registry import ProviderRegistry
    from api.reputation.record import ConnectorSiteReputation
    from api.routing.router import Router
    from api.services.read_model import PoolCounts, Pools, RuntimeView


@dataclass(frozen=True)
class FailureCount:
    """One connector's (None: before a proxy) failures of one kind and vendor status in the window, and its newest row."""

    connector_id: str | None
    kind: str
    upstream_status: int | None
    count: int
    last_at: datetime

    @classmethod
    def of(cls, row: FailureWindowRow) -> FailureCount:
        return cls(row.connector_id, row.kind, row.upstream_status, row.count, row.last_at)


@dataclass(frozen=True)
class SiteReputation:
    """What the reputation lines read: the recent sites, their split for a request naming no place, and the coarse records."""

    sites: tuple[str, ...]
    split: ReputationSplit
    coarse: tuple[ConnectorSiteReputation, ...]


@dataclass(frozen=True)
class ProjectData:
    """Everything one project's checks read, loaded together in a fixed number of statements."""

    project: Project
    # Every one, pending deletion included, oldest first.
    connectors: tuple[Connector, ...]
    # Connector id -> its rows per status.
    statuses: Mapping[str, Counter[ProxyStatus]]
    # As the hero counts it.
    pool: PoolCounts
    # Enabled connectors whose stored traffic block is in its period.
    blocked: frozenset[str]
    # The last 15 minutes.
    failures: tuple[FailureCount, ...]
    project_requests: int
    connector_requests: Mapping[str, int]
    # The routing preview the Countries page uses.
    preview: Router
    registry: ProviderRegistry
    now: datetime
    # The project's stored mutes, by line key, read with the rest.
    mutes: Mapping[str, Any] = field(default_factory=dict)
    # One-project reads only.
    traffic: Mapping[str, TrafficUsage] = field(default_factory=dict)
    # One-project reads, when the project records exit reputation.
    reputation: SiteReputation | None = None


class ProjectDataLoader:
    """Loads what the checks read: one project's everything, or every project's for the checks that can find a problem.

    Args:
        preview: builds the routing preview of one project (``ApiRuntime.preview_router``).
        preview_all: builds one preview over every project (``ApiRuntime.preview_all``).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        runtime_view: RuntimeView,
        registry: ProviderRegistry,
        *,
        preview: Callable[[str], Awaitable[Router]],
        preview_all: Callable[[], Awaitable[Router]],
    ) -> None:
        self._session_factory = session_factory
        self._view = runtime_view
        self._registry = registry
        self._preview = preview
        self._preview_all = preview_all

    async def project(self, project_id: str) -> ProjectData | None:
        """One project's data, its traffic and exit reputation included; None when there is no such project."""
        now = utc_now()
        since = now - WINDOW
        preview = await self._preview(project_id)
        async with self._session_factory() as session:
            projects = ProjectRepository(session)
            project = await projects.get_by_id(project_id)
            if project is None:
                return None
            mutes = await projects.attention_mutes([project_id])
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            rows = await self._view.pool_rows(session, connectors, project_id)
            metrics = MetricsRepository(session)
            failures = await metrics.failure_window([project_id], since)
            requests = await metrics.project_requests_since([project_id], since)
            connector_requests = await metrics.connector_requests_since([c.id for c in connectors], since)
            limited = [c for c in connectors if c.parsed_traffic_config.limit_bytes is not None]
            history = await TrafficHistory.load(session, limited, now)
            reputation = await self._reputation(session, project, preview, now) if project.exit_reputation.records else None
        pools = await self._view.pools(rows)
        traffic = await self._view.usage(history)
        return _data(
            project,
            connectors,
            _statuses(rows.counts),
            pools,
            failures,
            requests.get(project_id, 0),
            connector_requests,
            preview,
            self._registry,
            now,
            mutes=mutes.get(project_id, {}),
            traffic=traffic,
            reputation=reputation,
        )

    async def projects(self) -> list[ProjectData]:
        """Every project's data that is not pending deletion, oldest first: what the problem checks read, no traffic or
        exit reputation."""
        now = utc_now()
        since = now - WINDOW
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
            mutes = await ProjectRepository(session).attention_mutes(None)
            connectors = await ConnectorRepository(session).get_all()
            rows = await self._view.pool_rows(session, connectors, None)
            live = [project.id for project in projects if not project.pending_deletion]
            metrics = MetricsRepository(session)
            failures = await metrics.failure_window(live, since)
            requests = await metrics.project_requests_since(live, since)
            connector_requests = await metrics.connector_requests_since([c.id for c in connectors], since)
        pools = await self._view.pools(rows)
        preview = await self._preview_all()
        by_project: dict[str, list[Connector]] = {}
        for connector in connectors:
            by_project.setdefault(connector.project_id, []).append(connector)
        failed: dict[str, list[FailureWindowRow]] = {}
        for row in failures:
            failed.setdefault(row.project_id, []).append(row)
        statuses = _statuses(rows.counts)
        return [
            _data(
                project,
                by_project.get(project.id, []),
                statuses,
                pools,
                failed.get(project.id, []),
                requests.get(project.id, 0),
                connector_requests,
                preview,
                self._registry,
                now,
                mutes=mutes.get(project.id, {}),
            )
            for project in sorted(projects, key=lambda p: (p.created_at, p.id))
            if not project.pending_deletion
        ]

    async def _reputation(self, session: AsyncSession, project: Project, preview: Router, now: datetime) -> SiteReputation:
        """The project's recently reported sites, their split for a request naming no place and the coarse records seen
        lately: four statements, two when no site was reported lately."""
        repository = ExitReputationRepository(session)
        recent = now - REPUTATION_RECENT
        sites = await repository.recent_sites(project.id, recent, limit=REPUTATION_SITES)
        split = await ReputationSplit.read(session, project, preview, sites=sites, place="", now=now)
        coarse = await repository.coarse_records(project.id, sites, recent) if sites else []
        return SiteReputation(sites=tuple(sites), split=split, coarse=tuple(coarse))


def _statuses(counts: Iterable[ConnectorStatusCount]) -> dict[str, Counter[ProxyStatus]]:
    """Each connector's rows per status."""
    statuses: dict[str, Counter[ProxyStatus]] = {}
    for row in counts:
        statuses.setdefault(row.connector_id, Counter())[row.status] += row.count
    return statuses


def _data(
    project: Project,
    connectors: Iterable[Connector],
    statuses: Mapping[str, Counter[ProxyStatus]],
    pools: Pools,
    failures: Iterable[FailureWindowRow],
    project_requests: int,
    connector_requests: Mapping[str, int],
    preview: Router,
    registry: ProviderRegistry,
    now: datetime,
    *,
    mutes: Mapping[str, Any],
    traffic: Mapping[str, TrafficUsage] | None = None,
    reputation: SiteReputation | None = None,
) -> ProjectData:
    """One project's data out of reads that may hold other projects' rows too."""
    held = tuple(connectors)
    ids = {connector.id for connector in held}
    enabled = {connector.id for connector in held if connector.enabled}
    return ProjectData(
        project=project,
        connectors=held,
        statuses={connector_id: statuses.get(connector_id, Counter()) for connector_id in ids},
        pool=pools.pool(ids),
        blocked=frozenset(enabled - pools.counted),
        failures=tuple(FailureCount.of(row) for row in failures),
        project_requests=project_requests,
        connector_requests={connector_id: connector_requests.get(connector_id, 0) for connector_id in ids},
        preview=preview,
        registry=registry,
        now=now,
        mutes=mutes,
        traffic=traffic or {},
        reputation=reputation,
    )
