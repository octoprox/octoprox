"""Periodic flusher for Redis metrics to Postgres."""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.db.redis import RedisClient
from api.db.repository import MetricsRepository

logger = structlog.get_logger()


class MetricsFlusher:
    """Periodically flushes metrics from Redis to Postgres for historical storage.

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
        self._interval = settings.metrics_flush_interval
        self._running = False

    async def run(self) -> None:
        """Run the metrics flush loop."""
        self._running = True
        logger.info("Starting metrics flusher", interval=self._interval)

        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._flush_metrics()
            except asyncio.CancelledError:
                logger.info("Metrics flusher stopped")
                break
            except Exception as e:
                logger.error("Metrics flush error", error=str(e))

    async def _flush_metrics(self) -> None:
        """Flush all proxy metrics from Redis to Postgres."""
        # Get all metrics from Redis
        all_metrics = await self._redis_client.get_all_proxy_metrics()
        all_statuses = await self._redis_client.get_all_proxy_statuses()

        if not all_metrics:
            logger.debug("No metrics to flush")
            return

        logger.info("Flushing metrics to Postgres", proxy_count=len(all_metrics))

        async with self._session_factory() as session:
            repo = MetricsRepository(session)

            for proxy_id, metrics in all_metrics.items():
                status_data = all_statuses.get(proxy_id, {})
                status = status_data.get("status", "unknown")
                if hasattr(status, "value"):
                    status = status.value

                await repo.save_metrics_snapshot(
                    proxy_id=proxy_id,
                    request_count=metrics["request_count"],
                    success_count=metrics["success_count"],
                    failure_count=metrics["failure_count"],
                    avg_latency_ms=metrics["avg_latency_ms"],
                    bytes_sent=metrics.get("bytes_sent", 0),
                    bytes_received=metrics.get("bytes_received", 0),
                    status=status,
                )

                # Reset Redis metrics after successful flush
                await self._redis_client.reset_proxy_metrics(proxy_id)

            await session.commit()

        logger.info("Metrics flush complete", flushed_count=len(all_metrics))

    def stop(self) -> None:
        """Stop the flusher."""
        self._running = False

