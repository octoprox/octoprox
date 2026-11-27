# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The admin System view: the cluster and the stores as Postgres and Redis describe them, and their trend history."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from api.core import utc_now
from api.core.system_stats import SystemReader
from api.db.repository import SystemMetricsRepository
from api.models.system import SystemMetricsHistory, SystemMetricsPoint, TableGrowth

if TYPE_CHECKING:
    from api.core.system_stats import ClusterReader
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.models.system import SystemStats, SystemTopology
    from api.providers.registry import ProviderRegistry

# The windows the trend charts read, from the last hour to the last ninety days (the keys of HISTORY_RANGES).
HistoryRange = Literal["1h", "24h", "7d", "30d", "90d"]

# (window, bucket width). A null bucket returns raw snapshots; anything longer
# is averaged, because these are gauges - see SystemMetricsRepository.
HISTORY_RANGES: dict[str, tuple[timedelta, int | None]] = {
    "1h": (timedelta(hours=1), None),
    "24h": (timedelta(hours=24), None),
    "7d": (timedelta(days=7), 3600),
    "30d": (timedelta(days=30), 21600),
    "90d": (timedelta(days=90), 86400),
}


class SystemService:
    """Reads the cluster from the heartbeats, the stores from Postgres and Redis, and the snapshot job's gauge history.

    Args:
        metrics_interval: this process's ``system_metrics_interval``, which the controller's snapshot producer should
            match.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        provider_registry: ProviderRegistry,
        *,
        cluster: ClusterReader,
        metrics_interval: int,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._registry = provider_registry
        self._cluster = cluster
        self._metrics_interval = metrics_interval

    async def stats(self) -> SystemStats:
        """Every live process by role, the controller and feed, the work streams, and the stores."""
        async with self._session_factory() as session:
            return await SystemReader(session, self._redis_client, self._registry).read()

    async def topology(self) -> SystemTopology:
        """The cluster sections of :meth:`stats` alone, read from Redis only."""
        return await self._cluster.topology()

    async def history(self, range: HistoryRange) -> SystemMetricsHistory:
        """The install-wide gauge history of ``range``, oldest first, with each table's growth across it, biggest first."""
        window, bucket_seconds = HISTORY_RANGES[range]
        since = utc_now() - window

        async with self._session_factory() as session:
            repo = SystemMetricsRepository(session)
            if bucket_seconds:
                rows = await repo.get_history_aggregated(since, bucket_seconds)
            else:
                rows = await repo.get_history(since)

            first_sizes, last_sizes = await repo.get_table_sizes_at_edges(since)
        growth = [
            TableGrowth(
                name=name,
                first_bytes=int(first_sizes.get(name, 0)),
                last_bytes=int(last_bytes),
                delta_bytes=int(last_bytes) - int(first_sizes.get(name, 0)),
            )
            for name, last_bytes in last_sizes.items()
        ]
        growth.sort(key=lambda t: t.delta_bytes, reverse=True)

        return SystemMetricsHistory(
            range=range,
            bucket_seconds=bucket_seconds,
            interval_seconds=self._metrics_interval,
            snapshots=[SystemMetricsPoint(**row) for row in rows],
            table_growth=growth,
        )
