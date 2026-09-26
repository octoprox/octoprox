# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Periodic flusher for Redis metrics to Postgres."""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.workers import LeaseName, WorkerName
from api.db.redis import RedisClient
from api.db.repository import MetricsRepository

logger = structlog.get_logger()

# How often a standby instance polls Redis to see if the lease has freed
# up. Set well above the lease TTL (5s) so non-leaders don't hammer Redis
# but still take over within ~30s of a leader dying.
_LEASE_RETRY_SECONDS = 30.0


class MetricsFlusher:
    """Periodically flushes metrics from Redis to Postgres for historical storage.

    Singleton across Octoprox instances: only the leaseholder runs the
    flush. Two flushers writing the same Redis-aggregated counts to
    Postgres would double-count, so this is leader-elected.

    Args:
        session_factory: Async session factory for database operations.
        redis_client: Redis client for operational data.
        settings: Application settings.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._settings = settings
        self._interval = settings.metrics_flush_interval
        self._running = False

    async def run(self) -> None:
        """Run the metrics flush loop while holding the global lease."""
        self._running = True
        logger.info("Starting metrics flusher", interval=self._interval)
        job_stats.declare_interval(WorkerName.METRICS_FLUSHER, self._interval)
        lease = Lease(
            self._redis_client,
            name=LeaseName.METRICS_FLUSHER,
            owner_id=self._settings.instance_id,
        )
        try:
            while self._running:
                try:
                    if not lease.is_held and not await lease.try_acquire():
                        await asyncio.sleep(_LEASE_RETRY_SECONDS)
                        continue
                    await asyncio.sleep(self._interval)
                    if lease.is_held and self._running:
                        with job_stats.track(WorkerName.METRICS_FLUSHER):
                            await self._flush_metrics()
                except asyncio.CancelledError:
                    logger.info("Metrics flusher stopped")
                    break
                except Exception as e:
                    logger.error("Metrics flush error", error=str(e))
        finally:
            await lease.release()

    async def _flush_metrics(self) -> None:
        """Flush all proxy and project metrics from Redis to Postgres."""
        # Get all metrics from Redis
        all_proxy_metrics = await self._redis_client.get_all_proxy_metrics()
        all_project_metrics = await self._redis_client.get_all_project_metrics()
        all_connector_metrics = await self._redis_client.get_all_connector_metrics()
        all_statuses = await self._redis_client.get_all_proxy_statuses()

        if not all_proxy_metrics and not all_project_metrics and not all_connector_metrics:
            logger.debug("No metrics to flush")
            return

        logger.info(
            "Flushing metrics to Postgres",
            proxy_count=len(all_proxy_metrics),
            project_count=len(all_project_metrics),
            connector_count=len(all_connector_metrics),
        )

        # Every hash is cleared once the transaction is committed, whether its
        # row was written or its entity was gone. Clearing inside the loop
        # would lose a window if a later row made the transaction roll back.
        flushed_proxies: list[str] = []
        flushed_projects: list[str] = []
        flushed_connectors: list[str] = []
        async with self._session_factory() as session:
            repo = MetricsRepository(session)

            for proxy_id, metrics in all_proxy_metrics.items():
                status_data = all_statuses.get(proxy_id, {})
                status = status_data.get("status", "unknown")
                if hasattr(status, "value"):
                    status = status.value
                kept = await repo.save_metrics_snapshot(
                    proxy_id=proxy_id,
                    request_count=metrics.request_count,
                    success_count=metrics.success_count,
                    failure_count=metrics.failure_count,
                    avg_latency_ms=metrics.avg_latency_ms,
                    bytes_sent=metrics.bytes_sent,
                    bytes_received=metrics.bytes_received,
                    status=status,
                )
                if not kept:
                    logger.warning("Dropping metrics of a proxy that no longer exists", proxy_id=proxy_id)
                flushed_proxies.append(proxy_id)

            for project_id, metrics in all_project_metrics.items():
                kept = await repo.save_project_metrics_snapshot(
                    project_id=project_id,
                    request_count=metrics.request_count,
                    success_count=metrics.success_count,
                    failure_count=metrics.failure_count,
                    avg_latency_ms=metrics.avg_latency_ms,
                    bytes_sent=metrics.bytes_sent,
                    bytes_received=metrics.bytes_received,
                )
                if not kept:
                    logger.warning("Dropping metrics of a project that no longer exists", project_id=project_id)
                flushed_projects.append(project_id)

            for connector_id, metrics in all_connector_metrics.items():
                kept = await repo.save_connector_metrics_snapshot(
                    connector_id=connector_id,
                    request_count=metrics.request_count,
                    success_count=metrics.success_count,
                    failure_count=metrics.failure_count,
                    avg_latency_ms=metrics.avg_latency_ms,
                    bytes_sent=metrics.bytes_sent,
                    bytes_received=metrics.bytes_received,
                )
                if not kept:
                    logger.warning("Dropping metrics of a connector that no longer exists", connector_id=connector_id)
                flushed_connectors.append(connector_id)

            await session.commit()

        for proxy_id in flushed_proxies:
            await self._redis_client.reset_proxy_metrics(proxy_id)
        for project_id in flushed_projects:
            await self._redis_client.reset_project_metrics(project_id)
        for connector_id in flushed_connectors:
            await self._redis_client.reset_connector_metrics(connector_id)

        logger.info(
            "Metrics flush complete",
            proxy_count=len(all_proxy_metrics),
            project_count=len(all_project_metrics),
            connector_count=len(all_connector_metrics),
        )

    def stop(self) -> None:
        """Stop the flusher."""
        self._running = False

