# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Admin-only system statistics: the cluster and the stores as Postgres and Redis describe them, and their trend history.

Admin-only because they expose deployment shape (instance ids, the lease holder, database sizing) that no other role
has a use for.
"""

from typing import Literal

from fastapi import APIRouter, Query, Request

from api.core.auth import RequireAdminDep
from api.models.system import SystemMetricsHistory, SystemStats, SystemTopology
from api.routes.common import system_service_of

router = APIRouter(prefix="/system")


@router.get("/stats", response_model=SystemStats)
async def get_system_stats(request: Request, _admin: RequireAdminDep) -> SystemStats:
    """Return every live process by role, the controller and feed, the work streams, and the stores.

    The processes' sections come from their heartbeats in Redis, so this one
    endpoint covers the whole cluster without a route to any process; the
    rest is read from Postgres and Redis directly.
    """
    return await system_service_of(request).stats()


@router.get("/topology", response_model=SystemTopology)
async def get_system_topology(request: Request, _admin: RequireAdminDep) -> SystemTopology:
    """Return the cluster sections of ``/stats`` alone: every live process, the controller, the feed and the work streams.

    Read from Redis only, without the keyspace scan and the table sizes, so the
    topology view can poll it on the heartbeats' cadence.
    """
    return await system_service_of(request).topology()


@router.get("/stats/history", response_model=SystemMetricsHistory)
async def get_system_stats_history(
    request: Request,
    _admin: RequireAdminDep,
    range: Literal["1h", "24h", "7d", "30d", "90d"] = Query("24h", alias="range"),
) -> SystemMetricsHistory:
    """Return the install-wide gauge history behind the System trend charts, as the snapshot job records it.

    ``interval_seconds`` is this process's ``system_metrics_interval``, which
    the controller's snapshot producer should match.
    """
    return await system_service_of(request).history(range)
