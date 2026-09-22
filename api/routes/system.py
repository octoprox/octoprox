# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Admin-only system statistics.

One endpoint returning inventory, storage and worker health for the whole
install. Admin-only because it exposes deployment shape - instance ids, lease
holders, database sizing - that nothing below that role has a use for.
"""

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.auth import RequireAdminDep
from api.core.config import settings
from api.core.system_stats import collect_system_stats
from api.db.repository import SystemMetricsRepository
from api.db.session import get_db
from api.models.system import SystemMetricsHistory, SystemMetricsPoint, SystemStats, TableGrowth

router = APIRouter(prefix="/system")

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/stats", response_model=SystemStats)
async def get_system_stats(request: Request, session: DbDep, _admin: RequireAdminDep) -> SystemStats:
    """Return inventory, cache, storage and worker statistics.

    Postgres-derived sections describe the whole install; ``runtime``,
    ``cache`` and ``workers.tasks`` describe only the instance that served
    the request, which in a cluster is whichever one the load balancer picked.
    The same three sections for every *other* instance arrive in
    ``workers.instances[].snapshot``, published by each instance on its own
    heartbeat - so this one endpoint covers the whole cluster without the
    caller needing a route to a specific instance.
    """
    state = request.app.state
    return await collect_system_stats(
        session,
        settings,
        proxy_manager=getattr(state, "proxy_manager", None),
        proxy_server=getattr(state, "proxy_server", None),
        cert_manager=getattr(state, "cert_manager", None),
        geo_runtime=getattr(state, "geo_runtime", None),
        redis_client=getattr(state, "redis_client", None),
        started_at=getattr(state, "started_at", None),
    )


# (window, bucket width). A null bucket returns raw snapshots; anything longer
# is averaged, because these are gauges - see SystemMetricsRepository.
HISTORY_RANGES: dict[str, tuple[timedelta, int | None]] = {
    "1h": (timedelta(hours=1), None),
    "24h": (timedelta(hours=24), None),
    "7d": (timedelta(days=7), 3600),
    "30d": (timedelta(days=30), 21600),
    "90d": (timedelta(days=90), 86400),
}


@router.get("/stats/history", response_model=SystemMetricsHistory)
async def get_system_stats_history(
    session: DbDep,
    _admin: RequireAdminDep,
    range: Literal["1h", "24h", "7d", "30d", "90d"] = Query("24h", alias="range"),
) -> SystemMetricsHistory:
    """Return the install-wide gauge history behind the System trend charts.

    Snapshots are written by the ``system_snapshotter`` worker on whichever
    instance holds its lease, so this series is the same from every instance -
    unlike the live ``/system/stats`` sections that describe one process.
    """
    window, bucket_seconds = HISTORY_RANGES[range]
    since = utc_now() - window

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
        interval_seconds=settings.system_metrics_interval,
        snapshots=[SystemMetricsPoint(**row) for row in rows],
        table_growth=growth,
    )
