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
                            await self._flush_metrics(lease)
                except asyncio.CancelledError:
                    logger.info("Metrics flusher stopped")
                    break
                except Exception as e:
                    logger.error("Metrics flush error", error=str(e))
        finally:
            await lease.release()

    async def _flush_metrics(self, lease: Lease | None = None) -> None:
        """Flush all proxy, project, connector, tunnel device, host and failure metrics from Redis to Postgres.

        ``lease`` is confirmed with Redis just before the commit. A flush that
        outlived its lease is rolled back and leaves Redis as it found it: the
        new holder flushes the same counts, and two flushes committing them
        would write them twice. None (a direct call) skips the check.
        """
        # Get all metrics from Redis: independent walks, run side by side
        # rather than one after another.
        # Nested because gather's typing stops at six awaitables.
        redis = self._redis_client
        (
            (
                all_proxy_metrics,
                all_project_metrics,
                all_connector_metrics,
                all_tunnel_peer_metrics,
                all_host_metrics,
                all_failure_metrics,
            ),
            all_statuses,
        ) = await asyncio.gather(
            asyncio.gather(
                redis.get_all_proxy_metrics(),
                redis.get_all_project_metrics(),
                redis.get_all_connector_metrics(),
                redis.get_all_tunnel_peer_metrics(),
                redis.get_all_host_metrics(),
                redis.get_all_failure_metrics(),
            ),
            redis.get_all_proxy_statuses(),
        )

        if (
            not all_proxy_metrics
            and not all_project_metrics
            and not all_connector_metrics
            and not all_tunnel_peer_metrics
            and not all_host_metrics
            and not all_failure_metrics
        ):
            logger.debug("No metrics to flush")
            return

        logger.info(
            "Flushing metrics to Postgres",
            proxy_count=len(all_proxy_metrics),
            project_count=len(all_project_metrics),
            connector_count=len(all_connector_metrics),
            tunnel_peer_count=len(all_tunnel_peer_metrics),
            host_count=len(all_host_metrics),
            failure_count_rows=len(all_failure_metrics),
        )

        # Every hash read is drained once the transaction is committed, whether
        # its row was written or its entity was gone: draining inside the loop
        # would lose a window if a later row made the transaction roll back.
        async with self._session_factory() as session:
            repo = MetricsRepository(session)

            # One lookup and one multi-row insert per table, like the host
            # rows below: a statement per proxy stretches the flush.
            statuses: dict[str, str] = {}
            for proxy_id in all_proxy_metrics:
                status = all_statuses.get(proxy_id, {}).get("status", "unknown")
                statuses[proxy_id] = status.value if hasattr(status, "value") else status
            dropped_proxies = await repo.save_proxy_metrics_snapshots(all_proxy_metrics, statuses)
            if dropped_proxies:
                logger.warning("Dropping metrics of proxies that no longer exist", proxy_ids=dropped_proxies)

            dropped_projects = await repo.save_project_metrics_snapshots(all_project_metrics)
            if dropped_projects:
                logger.warning("Dropping metrics of projects that no longer exist", project_ids=dropped_projects)

            dropped_connectors = await repo.save_connector_metrics_snapshots(all_connector_metrics)
            if dropped_connectors:
                logger.warning(
                    "Dropping metrics of connectors that no longer exist", connector_ids=dropped_connectors
                )

            for peer_id, peer_metrics in all_tunnel_peer_metrics.items():
                kept = await repo.save_tunnel_peer_metrics_snapshot(peer_id, peer_metrics)
                if not kept:
                    logger.warning("Dropping metrics of a tunnel device that no longer exists", peer_id=peer_id)

            # One bulk insert for every host row of the window: there can be
            # thousands, and a statement each would stretch the flush.
            dropped_hosts = await repo.save_host_metrics_snapshots(all_host_metrics)
            if dropped_hosts:
                logger.warning(
                    "Dropping host metrics of connectors that no longer exist",
                    connector_ids=sorted({connector_id for _project, connector_id, _host in dropped_hosts}),
                    row_count=len(dropped_hosts),
                )

            dropped_failures = await repo.save_failure_metrics_snapshots(all_failure_metrics)
            if dropped_failures:
                logger.warning(
                    "Dropping failure counts of projects or connectors that no longer exist",
                    row_count=len(dropped_failures),
                )

            if lease is not None and not await lease.confirm():
                logger.warning("Metrics flush rolled back: the lease could not be confirmed before the commit")
                return
            await session.commit()

        # Subtracted, not deleted: what the instances added since the read
        # stays in the hashes for the next flush.
        await self._redis_client.drain_flushed_metrics(
            proxies=all_proxy_metrics,
            projects=all_project_metrics,
            connectors=all_connector_metrics,
            tunnel_peers=all_tunnel_peer_metrics,
            hosts=all_host_metrics,
            failures=all_failure_metrics,
        )

        logger.info(
            "Metrics flush complete",
            proxy_count=len(all_proxy_metrics),
            project_count=len(all_project_metrics),
            connector_count=len(all_connector_metrics),
            tunnel_peer_count=len(all_tunnel_peer_metrics),
            host_count=len(all_host_metrics),
            failure_count_rows=len(all_failure_metrics),
        )

    def stop(self) -> None:
        """Stop the flusher."""
        self._running = False

