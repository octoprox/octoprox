# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The metrics jobs: the flush of the live Redis windows into Postgres, and the hourly compaction with the retentions."""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.roles import JobKind
from api.core.stats import MetricDelta
from api.db.geo_repository import ObservationRepository
from api.db.redis import PROJECT_WINDOWS, PROXY_WINDOWS, TUNNEL_PEER_WINDOWS
from api.db.repository import MetricsRepository, ProjectRepository, ProxyRepository
from api.jobs.job import Job, JobBatch

if TYPE_CHECKING:
    from api.db.redis import MetricWindows, RedisClient
    from api.db.session import SessionFactory
    from api.geo.service import GeoService
    from api.models.project import Project

logger = structlog.get_logger()

# The compaction tiers, as (source granularity, target granularity, age): the
# rows of the source granularity older than the age fold into buckets of the
# target. They follow the chart views: minutes for a day, hours for a week,
# six hours for a month, then days until the project's retention.
COMPACTION_TIERS: tuple[tuple[int, int, timedelta], ...] = (
    (60, 3600, timedelta(hours=24)),
    (3600, 21600, timedelta(days=7)),
    (21600, 86400, timedelta(days=30)),
)

# How long a committed flush stays recorded: far longer than a delivery of its
# entry can come back.
FLUSH_RECORD_AGE = timedelta(days=1)


class MetricsFlushJob(Job):
    """Moves the live metrics windows into Postgres history and totals, as one flush named by its job entry's ID.

    The windows are taken into the flush's staging hash, committed with the ID in ``metric_flushes``, then cleared.
    """

    kind = JobKind.METRICS_FLUSH
    # One entry a run: its ID names the flush.
    batch_size = 1

    def __init__(self, session_factory: SessionFactory, redis_client: RedisClient) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    async def run(self, batch: JobBatch) -> None:
        """Take, commit and clear the flush; a delivery whose ID is recorded already only clears."""
        flush_id = batch.ids[0]
        if await self._recorded(flush_id):
            await self._redis.clear_metric_windows(flush_id)
            return
        started = time.monotonic()
        windows = await self._redis.take_metric_windows(flush_id)
        taken = time.monotonic()
        written = not windows.empty and await self._commit(flush_id, windows)
        committed = time.monotonic()
        await self._redis.clear_metric_windows(flush_id)
        if written:
            logger.info(
                "Metrics flush complete",
                flush_id=flush_id,
                proxy_count=len(windows.proxies),
                project_count=len(windows.projects),
                connector_count=len(windows.connectors),
                tunnel_peer_count=len(windows.tunnel_peers),
                host_count=len(windows.hosts),
                failure_count_rows=len(windows.failures),
                # A slow take is Redis or a busy event loop, a slow write Postgres.
                take_ms=round((taken - started) * 1000),
                write_ms=round((committed - taken) * 1000),
            )

    async def give_up(self, batch: JobBatch) -> None:
        """Settle a flush delivered too often: clear it once committed, else put its windows back for the next flush.

        Raises while Postgres cannot say which, so the entry stays pending.
        """
        flush_id = batch.ids[0]
        if await self._recorded(flush_id):
            await self._redis.clear_metric_windows(flush_id)
            logger.warning("Metrics flush given up after it committed; its staging hash is cleared", flush_id=flush_id)
            return
        await self._redis.put_back_metric_windows(flush_id)
        logger.warning("Metrics flush given up before it committed; its windows go to the next flush", flush_id=flush_id)

    async def _recorded(self, flush_id: str) -> bool:
        """Whether Postgres holds flush ``flush_id`` committed; raises while it cannot say."""
        async with self._session_factory() as session:
            return await MetricsRepository(session).flush_recorded(flush_id)

    async def _commit(self, flush_id: str, windows: MetricWindows) -> bool:
        """Write the windows' history rows and totals in one transaction that records the flush; False when it was recorded.

        A window whose entity is gone is dropped, and logged.
        """
        statuses = await self._statuses(windows.proxies)
        async with self._session_factory() as session:
            repo = MetricsRepository(session)
            if not await repo.record_flush(flush_id):
                logger.warning("Metrics flush committed by another delivery; this one writes nothing", flush_id=flush_id)
                return False

            # One lookup and one multi-row insert per table; the totals follow the rows written.
            dropped = await repo.save_proxy_metrics_snapshots(windows.proxies, statuses)
            if dropped:
                logger.warning("Dropping metrics of proxies that no longer exist", proxy_ids=dropped)
            await repo.upsert_totals(PROXY_WINDOWS, _kept(windows.proxies, dropped))

            dropped = await repo.save_project_metrics_snapshots(windows.projects)
            if dropped:
                logger.warning("Dropping metrics of projects that no longer exist", project_ids=dropped)
            await repo.upsert_totals(PROJECT_WINDOWS, _kept(windows.projects, dropped))

            dropped = await repo.save_connector_metrics_snapshots(windows.connectors)
            if dropped:
                logger.warning("Dropping metrics of connectors that no longer exist", connector_ids=dropped)

            dropped = await repo.save_tunnel_peer_metrics_snapshots(windows.tunnel_peers)
            if dropped:
                logger.warning("Dropping metrics of tunnel devices that no longer exist", peer_ids=dropped)
            await repo.upsert_totals(TUNNEL_PEER_WINDOWS, _kept(windows.tunnel_peers, dropped))

            dropped_hosts = await repo.save_host_metrics_snapshots(windows.hosts)
            if dropped_hosts:
                logger.warning(
                    "Dropping host metrics of connectors that no longer exist",
                    connector_ids=sorted({connector_id for _project, connector_id, _host in dropped_hosts}),
                    row_count=len(dropped_hosts),
                )

            failures = {key: count.failure_count for key, count in windows.failures.items()}
            dropped_failures = await repo.save_failure_metrics_snapshots(failures)
            if dropped_failures:
                logger.warning(
                    "Dropping failure counts of projects or connectors that no longer exist",
                    row_count=len(dropped_failures),
                )
            await session.commit()
        return True

    async def _statuses(self, proxy_ids: Iterable[str]) -> dict[str, str]:
        """Each proxy's status as its column holds it, for the status its history row records; gone proxies are absent."""
        ids = list(proxy_ids)
        if not ids:
            return {}
        async with self._session_factory() as session:
            placements = await ProxyRepository(session).placements(ids)
        return {proxy_id: placement.status.value for proxy_id, placement in placements.items()}


class CompactionJob(Job):
    """Folds old metrics rows into coarser ones and applies every retention; each step is set-based and safe to repeat."""

    kind = JobKind.COMPACTION

    def __init__(self, session_factory: SessionFactory, geo_service: GeoService) -> None:
        self._session_factory = session_factory
        self._geo = geo_service

    async def run(self, batch: JobBatch) -> None:
        """Compact and retain each project's metrics, then drop gone entities' totals and old observations and records."""
        now = utc_now()
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
        compacted = retained = 0
        for project in projects:
            compacted += await self._compact(project.id, now)
            retained += await self._retain(project, now)

        settings = self._geo.settings
        async with self._session_factory() as session:
            metrics, observations = MetricsRepository(session), ObservationRepository(session)
            totals = await metrics.delete_orphan_totals()
            observed = await observations.delete_expired(
                now,
                settings.observation_retention_days,
                {p.id: p.observation_retention_days for p in projects if p.observation_retention_days is not None},
            )
            exits = (
                await observations.delete_exit_ips_last_seen_before(now - timedelta(days=settings.exit_ip_retention_days))
                if settings.exit_ip_retention_days > 0
                else 0
            )
            flushes = await metrics.delete_flushes_older_than(now - FLUSH_RECORD_AGE)
            await session.commit()

        if compacted or retained or totals or observed or exits or flushes:
            logger.info(
                "Metrics compaction complete",
                projects=len(projects),
                rows_compacted=compacted,
                rows_deleted=retained,
                totals_deleted=totals,
                observations_deleted=observed,
                exit_ips_deleted=exits,
                flushes_deleted=flushes,
            )

    async def _compact(self, project_id: str, now: datetime) -> int:
        """Fold the project's rows of every metrics table through the tiers in order, a transaction a tier; the rows folded."""
        folded = 0
        for source, target, age in COMPACTION_TIERS:
            async with self._session_factory() as session:
                repo = MetricsRepository(session)
                for compact in (
                    repo.compact_project_metrics,
                    repo.compact_proxy_metrics,
                    repo.compact_connector_metrics,
                    repo.compact_tunnel_peer_metrics,
                    repo.compact_host_metrics,
                    repo.compact_failure_metrics,
                ):
                    folded += await compact(project_id, now - age, source, target)
                await session.commit()
        return folded

    async def _retain(self, project: Project, now: datetime) -> int:
        """Delete the project's metrics rows of every granularity past its retention, none at 0 days; the rows deleted."""
        if project.metrics_retention_days == 0:
            return 0
        cutoff = now - timedelta(days=project.metrics_retention_days)
        deleted = 0
        async with self._session_factory() as session:
            repo = MetricsRepository(session)
            for delete in (
                repo.delete_project_metrics_older_than,
                repo.delete_proxy_metrics_for_project_older_than,
                repo.delete_connector_metrics_for_project_older_than,
                repo.delete_tunnel_peer_metrics_older_than,
                repo.delete_host_metrics_older_than,
                repo.delete_failure_metrics_older_than,
            ):
                deleted += await delete(project.id, cutoff)
            await session.commit()
        return deleted


def _kept[D: MetricDelta](windows: Mapping[str, D], dropped: Collection[str]) -> dict[str, D]:
    """The windows whose history row was written, those not ``dropped``."""
    gone = set(dropped)
    return {entity_id: delta for entity_id, delta in windows.items() if entity_id not in gone}
