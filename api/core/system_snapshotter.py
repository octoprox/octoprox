# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Periodic install-wide gauge snapshots, for the admin trend charts.

Singleton across instances: the snapshot describes the install, not the
process, so N instances writing it would store N copies of the same reading
and make every average N-times heavier at no extra information. Leader
election reuses the same Redis lease as the other singleton workers.

Cadence is derived from the data rather than from this loop's own clock: each
tick asks Postgres when the newest snapshot was written and skips if one is
not yet due. That keeps the series evenly spaced across restarts and lease
handovers, which would otherwise each insert an off-cadence extra row.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.system_stats import collect_database, collect_inventory, collect_redis
from api.core.workers import LeaseName, WorkerName
from api.db.redis import RedisClient
from api.db.repository import SystemMetricsRepository

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()

# How often a standby polls to see if the lease has freed up. Snapshots are
# minutes apart, so a minute of failover lag costs at most one point.
_LEASE_RETRY_SECONDS = 60.0

# A tick within this fraction of the interval counts as "already taken", so
# clock jitter does not double up rows.
_DUE_TOLERANCE = 0.9


class SystemSnapshotter:
    """Writes one ``system_metrics`` row per interval and applies retention.

    Args:
        session_factory: Async session factory for database operations.
        redis_client: Connected Redis client, for the lease and for the
            memory/key gauges.
        proxy_manager: Source of live pool status, which is in-memory state
            rather than anything Postgres can answer.
        settings: Application settings.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        proxy_manager: ProxyManager,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._proxy_manager = proxy_manager
        self._settings = settings
        self._running = False

    # Config is read on use rather than captured in __init__, so constructing a
    # ProxyManager never depends on settings being resolvable.

    @property
    def _interval(self) -> int:
        """Seconds between snapshots, floored so a misconfiguration cannot
        hammer Postgres with size queries."""
        return max(60, self._settings.system_metrics_interval)

    @property
    def _retention_days(self) -> int:
        return int(self._settings.system_metrics_retention_days)

    @property
    def enabled(self) -> bool:
        """False when the operator has set the interval to 0."""
        return self._settings.system_metrics_interval > 0

    async def run(self) -> None:
        """Take snapshots while holding the global lease."""
        self._running = True
        logger.info(
            "Starting system snapshotter",
            interval=self._interval,
            retention_days=self._retention_days,
        )
        job_stats.declare_interval(WorkerName.SYSTEM_SNAPSHOTTER, self._interval)
        lease = Lease(
            self._redis_client,
            name=LeaseName.SYSTEM_SNAPSHOTTER,
            owner_id=self._settings.instance_id,
        )
        try:
            while self._running:
                try:
                    if not lease.is_held and not await lease.try_acquire():
                        await asyncio.sleep(_LEASE_RETRY_SECONDS)
                        continue
                    # Snapshot first, then sleep: a fresh install gets its
                    # first point immediately instead of after one interval.
                    if lease.is_held:
                        with job_stats.track(WorkerName.SYSTEM_SNAPSHOTTER):
                            await self._snapshot_if_due()
                    await asyncio.sleep(self._interval)
                except asyncio.CancelledError:
                    logger.info("System snapshotter stopped")
                    break
                except Exception as e:
                    logger.error("System snapshot error", error=str(e))
                    await asyncio.sleep(self._interval)
        finally:
            await lease.release()

    async def _snapshot_if_due(self) -> None:
        """Write a snapshot unless a recent one already covers this tick."""
        async with self._session_factory() as session:
            repo = SystemMetricsRepository(session)
            latest = await repo.get_latest_timestamp()

        if latest is not None:
            age = (utc_now() - latest).total_seconds()
            if age < self._interval * _DUE_TOLERANCE:
                logger.debug("System snapshot not due yet", age_seconds=round(age))
                return

        await self.take_snapshot()

    async def take_snapshot(self) -> None:
        """Collect the gauges and store one row, then apply retention."""
        async with self._session_factory() as session:
            inventory = await collect_inventory(session, self._proxy_manager)
            database = await collect_database(session)

        # Memory and key count only - no keyspace walk on this path.
        redis_stats = await collect_redis(self._redis_client, scan_keyspace=False)

        statuses = inventory.proxies_by_status
        async with self._session_factory() as session:
            repo = SystemMetricsRepository(session)
            await repo.save_snapshot(
                database_size_bytes=database.size_bytes,
                redis_memory_bytes=redis_stats.used_memory_bytes,
                redis_keys=redis_stats.total_keys,
                projects=inventory.projects,
                credentials=inventory.credentials,
                connectors=inventory.connectors,
                connectors_enabled=inventory.connectors_enabled,
                users=inventory.users,
                proxies_total=inventory.proxies,
                proxies_healthy=statuses.get("healthy", 0),
                proxies_unhealthy=statuses.get("unhealthy", 0),
                table_sizes={t.name: t.total_bytes for t in database.tables},
                proxy_status_counts=dict(statuses),
            )
            await session.commit()

        logger.debug(
            "System snapshot stored",
            database_size_bytes=database.size_bytes,
            redis_keys=redis_stats.total_keys,
            proxies=inventory.proxies,
        )

        await self._apply_retention()

    async def _apply_retention(self) -> None:
        """Delete snapshots past the retention window (0 disables)."""
        if self._retention_days <= 0:
            return
        cutoff = utc_now() - timedelta(days=self._retention_days)
        async with self._session_factory() as session:
            repo = SystemMetricsRepository(session)
            deleted = await repo.delete_older_than(cutoff)
            await session.commit()
        if deleted:
            logger.info("Pruned system metrics", rows_deleted=deleted)

    def stop(self) -> None:
        """Signal the snapshotter to stop."""
        self._running = False
