# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Periodic compaction and retention of historical metrics.

Progressively reduces granularity of older metrics to keep the database
manageable while preserving aggregate correctness.  Also enforces
per-project retention limits.
"""

import asyncio
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.leadership import Lease
from api.db.redis import RedisClient
from api.db.repository import MetricsRepository, ProjectRepository

logger = structlog.get_logger()

# How often a standby instance polls Redis to see if the lease has freed
# up. Compaction's normal cadence is hourly, so 60s is plenty fast for
# failover while staying well below the next regular tick.
_LEASE_RETRY_SECONDS = 60.0

# Compaction tiers: (source_granularity, target_granularity, age_threshold)
# Data older than age_threshold is compacted from source to target.
COMPACTION_TIERS: list[tuple[int, int, timedelta]] = [
    (60, 3600, timedelta(hours=24)),       # raw → hourly after 24h
    (3600, 21600, timedelta(days=7)),      # hourly → 6-hourly after 7d
    (21600, 86400, timedelta(days=30)),    # 6-hourly → daily after 30d
]


class MetricsCompactor:
    """Periodically compacts old metrics and enforces retention limits.

    Compaction tiers (aligned with the frontend chart views):
    - Raw 1-minute data (granularity=60) kept for 24 hours
    - 1-hour buckets (granularity=3600) kept for 7 days
    - 6-hour buckets (granularity=21600) kept for 30 days
    - Daily buckets (granularity=86400) kept until retention limit

    Args:
        session_factory: Async session factory for database operations.
        interval: How often to run compaction, in seconds (default: 1 hour).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        instance_id: str,
        interval: int = 3600,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._instance_id = instance_id
        self._interval = interval
        self._running = False

    async def run(self) -> None:
        """Run the compaction loop while holding the global lease.

        Single-leader: writes back compacted rows to Postgres, and two
        compactors running simultaneously would race on the same source
        rows and double-count their aggregates.
        """
        self._running = True
        logger.info("Starting metrics compactor", interval=self._interval)
        lease = Lease(
            self._redis_client,
            name="metrics_compactor",
            owner_id=self._instance_id,
        )
        try:
            while self._running:
                try:
                    if not lease.is_held and not await lease.try_acquire():
                        await asyncio.sleep(_LEASE_RETRY_SECONDS)
                        continue
                    await asyncio.sleep(self._interval)
                    if lease.is_held and self._running:
                        await self._compact_and_retain()
                except asyncio.CancelledError:
                    logger.info("Metrics compactor stopped")
                    break
                except Exception as e:
                    logger.error("Metrics compaction error", error=str(e))
        finally:
            await lease.release()

    async def _compact_and_retain(self) -> None:
        """Run compaction tiers and retention for all projects."""
        now = utc_now()

        async with self._session_factory() as session:
            project_repo = ProjectRepository(session)
            projects = await project_repo.get_all()

        total_compacted = 0
        total_deleted = 0

        for project in projects:
            compacted = await self._compact_project(project.id, now)
            total_compacted += compacted

            deleted = await self._apply_retention(
                project.id, project.metrics_retention_days, now
            )
            total_deleted += deleted

        if total_compacted > 0 or total_deleted > 0:
            logger.info(
                "Metrics compaction complete",
                projects=len(projects),
                rows_compacted=total_compacted,
                rows_deleted=total_deleted,
            )

    async def _compact_project(self, project_id: str, now: datetime) -> int:
        """Run all compaction tiers for a single project.

        Returns total number of source rows compacted.
        """
        total = 0

        for source_gran, target_gran, age_threshold in COMPACTION_TIERS:
            cutoff = now - age_threshold

            # Compact project metrics
            async with self._session_factory() as session:
                repo = MetricsRepository(session)
                count = await repo.compact_project_metrics(
                    project_id=project_id,
                    older_than=cutoff,
                    source_granularity=source_gran,
                    target_granularity=target_gran,
                )
                await session.commit()
                total += count

            # Compact proxy metrics for all proxies in this project
            async with self._session_factory() as session:
                repo = MetricsRepository(session)
                proxy_ids = await repo.get_proxy_ids_for_project(project_id)

            for proxy_id in proxy_ids:
                async with self._session_factory() as session:
                    repo = MetricsRepository(session)
                    count = await repo.compact_proxy_metrics(
                        proxy_id=proxy_id,
                        older_than=cutoff,
                        source_granularity=source_gran,
                        target_granularity=target_gran,
                    )
                    await session.commit()
                    total += count

        return total

    async def _apply_retention(
        self, project_id: str, retention_days: int, now: datetime
    ) -> int:
        """Delete metrics older than the project's retention period.

        Compaction only changes granularity — it never removes the coarsest
        tier (daily).  This method enforces the hard retention limit by
        deleting *all* rows (any granularity) past the configured age.

        Returns total number of rows deleted.
        """
        if retention_days == 0:
            return 0

        cutoff = now - timedelta(days=retention_days)
        total = 0

        async with self._session_factory() as session:
            repo = MetricsRepository(session)
            total += await repo.delete_project_metrics_older_than(
                project_id, cutoff
            )
            total += await repo.delete_proxy_metrics_for_project_older_than(
                project_id, cutoff
            )
            await session.commit()

        return total

    def stop(self) -> None:
        """Stop the compactor."""
        self._running = False
