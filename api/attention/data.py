# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Needs attention checks read, and the reads the alerts job loads it with: a fixed number of statements for
one project or for every project at once, however many connectors and proxies they hold.

Postgres first, Redis after each session closes, as the read model reads; the routing preview is the router the
Countries page asks. Redis is read strictly: a read that fails fails the run, never a check on missing data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from api.attention.checks import REPUTATION_RECENT, REPUTATION_SITES, WINDOW
from api.core.logging import error_text
from api.core.traffic_limiter import TrafficHistory
from api.db.repository import (
    ConnectorRepository,
    ConnectorStatusCount,
    FailureWindowRow,
    MetricsRepository,
    ProjectRepository,
    RequestTally,
)
from api.db.reputation_repository import ExitReputationRepository
from api.models.alerts import AlertSettings
from api.models.proxy import ProxyStatus
from api.reputation.split import ReputationSplit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory
    from api.feed.preview import RoutingPreview
    from api.models.connector import Connector, TrafficUsage
    from api.models.project import Project
    from api.providers.registry import ProviderRegistry
    from api.reputation.record import ConnectorSiteReputation
    from api.routing.router import Router
    from api.services.read_model import PoolCounts, Pools, RuntimeView

logger = structlog.get_logger()


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
    # Its alert cut-offs, the defaults where it sets none.
    thresholds: AlertSettings
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
    # The project's requests and successes in the last 15 minutes, as the Overview counts them.
    success: RequestTally
    connector_requests: Mapping[str, int]
    # The routing preview the Countries page uses.
    preview: Router
    registry: ProviderRegistry
    now: datetime
    # Every connector with a traffic limit.
    traffic: Mapping[str, TrafficUsage] = field(default_factory=dict)
    # When the project records exit reputation.
    reputation: SiteReputation | None = None

    @property
    def project_requests(self) -> int:
        """The project's flushed requests in the window."""
        return self.success.requests


@dataclass(frozen=True)
class Loaded:
    """The data of each project a load could build, oldest first, and the ids of those it could not."""

    datas: list[ProjectData]
    failed: list[str]


@dataclass(frozen=True)
class _Reputation:
    """What the exit reputation reads found for every recording project in scope, read together."""

    sites: Mapping[str, list[str]]
    avoided: Mapping[tuple[str, str], frozenset[str]]
    records: list[ConnectorSiteReputation]
    coarse: list[ConnectorSiteReputation]


class ProjectDataLoader:
    """Loads what the checks read for one project, or for every project at once, in a fixed number of statements however
    many projects, connectors or proxies there are."""

    def __init__(
        self,
        session_factory: SessionFactory,
        runtime_view: RuntimeView,
        registry: ProviderRegistry,
        preview: RoutingPreview,
    ) -> None:
        self._session_factory = session_factory
        self._view = runtime_view
        self._registry = registry
        self._preview = preview

    async def load(self, project_id: str | None, now: datetime) -> Loaded:
        """Every project's data (None), or the one project's (empty when it is gone), oldest project first, and the ids
        of projects whose data could not be built; a project pending deletion is included, its report empty, so its
        open alerts resolve.

        Raises when Postgres or Redis cannot be read.
        """
        since = now - WINDOW
        preview = await (self._preview.project(project_id) if project_id is not None else self._preview.every_project())
        async with self._session_factory() as session:
            projects_repository = ProjectRepository(session)
            if project_id is None:
                projects = await projects_repository.get_all()
            else:
                found = await projects_repository.get_by_id(project_id)
                projects = [found] if found is not None else []
            if not projects:
                return Loaded(datas=[], failed=[])
            ids = [project.id for project in projects]
            thresholds = await projects_repository.alert_settings(ids)
            connectors_repository = ConnectorRepository(session)
            connectors = await (
                connectors_repository.get_by_project(project_id)
                if project_id is not None
                else connectors_repository.get_all()
            )
            rows = await self._view.pool_rows(session, connectors, project_id)
            metrics = MetricsRepository(session)
            failures = await metrics.failure_window(ids, since)
            success = await metrics.project_window(ids, since)
            connector_requests = await metrics.connector_requests_since([c.id for c in connectors], since)
            limited = [c for c in connectors if c.parsed_traffic_config.limit_bytes is not None]
            history = await TrafficHistory.load(session, limited, now)
            recording = [project.id for project in projects if project.exit_reputation.records]
            reputation = await self._reputation(session, recording, now)
        pools = await self._view.pools(rows)
        traffic = await self._view.usage(history)

        by_project: dict[str, list[Connector]] = {}
        for connector in connectors:
            by_project.setdefault(connector.project_id, []).append(connector)
        failed_rows: dict[str, list[FailureWindowRow]] = {}
        for row in failures:
            failed_rows.setdefault(row.project_id, []).append(row)
        statuses = _statuses(rows.counts)
        datas: list[ProjectData] = []
        failed: list[str] = []
        for project in sorted(projects, key=lambda p: (p.created_at, p.id)):
            try:
                datas.append(
                    _data(
                        project,
                        thresholds.get(project.id) or AlertSettings(),
                        by_project.get(project.id, []),
                        statuses,
                        pools,
                        failed_rows.get(project.id, []),
                        success.get(project.id, RequestTally()),
                        connector_requests,
                        traffic,
                        preview,
                        self._registry,
                        now,
                        reputation=self._split(project, preview, reputation, now),
                    )
                )
            except Exception as exc:
                logger.error("Alert checks skipped for a project", project_id=project.id, error=error_text(exc))
                failed.append(project.id)
        return Loaded(datas=datas, failed=failed)

    @staticmethod
    async def _reputation(session: AsyncSession, project_ids: Collection[str], now: datetime) -> _Reputation:
        """The recently reported sites of every recording project, the exit IPs they avoid, and the connector records the
        reputation lines read: four statements, none while no project in scope records."""
        if not project_ids:
            return _Reputation(sites={}, avoided={}, records=[], coarse=[])
        repository = ExitReputationRepository(session)
        recent = now - REPUTATION_RECENT
        sites = await repository.recent_sites_of(project_ids, recent, limit=REPUTATION_SITES)
        pairs = [(project_id, site) for project_id, listed in sites.items() for site in listed]
        return _Reputation(
            sites=sites,
            avoided=await repository.avoided_ips_of(pairs, now),
            # A request naming no place: the records kept at no place.
            records=await repository.connector_records_of(pairs, [""]),
            coarse=await repository.coarse_records_of(pairs, recent),
        )

    @staticmethod
    def _split(project: Project, preview: Router, read: _Reputation, now: datetime) -> SiteReputation | None:
        """The project's reputation lines' data out of the reads of every recording project; None when it records none."""
        if not project.exit_reputation.records:
            return None
        sites = read.sites.get(project.id, [])
        split = ReputationSplit(
            project,
            preview,
            avoided={site: ips for (owner, site), ips in read.avoided.items() if owner == project.id},
            records=[record for record in read.records if record.project_id == project.id],
            now=now,
        )
        coarse = tuple(record for record in read.coarse if record.project_id == project.id)
        return SiteReputation(sites=tuple(sites), split=split, coarse=coarse)


def _statuses(counts: Iterable[ConnectorStatusCount]) -> dict[str, Counter[ProxyStatus]]:
    """Each connector's rows per status."""
    statuses: dict[str, Counter[ProxyStatus]] = {}
    for row in counts:
        statuses.setdefault(row.connector_id, Counter())[row.status] += row.count
    return statuses


def _data(
    project: Project,
    thresholds: AlertSettings,
    connectors: Iterable[Connector],
    statuses: Mapping[str, Counter[ProxyStatus]],
    pools: Pools,
    failures: Iterable[FailureWindowRow],
    success: RequestTally,
    connector_requests: Mapping[str, int],
    traffic: Mapping[str, TrafficUsage],
    preview: Router,
    registry: ProviderRegistry,
    now: datetime,
    *,
    reputation: SiteReputation | None,
) -> ProjectData:
    """One project's data out of reads that may hold other projects' rows too."""
    held = tuple(connectors)
    ids = {connector.id for connector in held}
    enabled = {connector.id for connector in held if connector.enabled}
    return ProjectData(
        project=project,
        thresholds=thresholds,
        connectors=held,
        statuses={connector_id: statuses.get(connector_id, Counter()) for connector_id in ids},
        pool=pools.pool(ids),
        blocked=frozenset(enabled - pools.counted),
        failures=tuple(FailureCount.of(row) for row in failures),
        success=success,
        connector_requests={connector_id: connector_requests.get(connector_id, 0) for connector_id in ids},
        preview=preview,
        registry=registry,
        now=now,
        traffic={connector_id: usage for connector_id, usage in traffic.items() if connector_id in ids},
        reputation=reputation,
    )
