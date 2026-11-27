# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project metrics endpoints on the api role: pool, counters, scaling, history, the traffic split and hosts.

Every number is read when asked for, the live parts from Redis: losing it shows the pool without quarantines or traffic
blocks, the counters without the window not flushed yet and demand as none, while losing Postgres fails every route.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from api.core.telemetry import CONTENT_TYPE
from api.models.metrics import (
    MAX_HOST_SERIES,
    MAX_HOSTS_PER_PAGE,
    HostsHistoryResponse,
    HostsResponse,
    MetricsHistoryResponse,
    MetricsRange,
    MetricsResponse,
    ScalingMetrics,
    TrafficSplitResponse,
)
from api.routes.common import (
    host_metrics_service_of,
    metrics_service_of,
    traffic_split_service_of,
)

router = APIRouter(prefix="/projects/{project_id}/metrics")


@router.get("", response_model=MetricsResponse)
async def get_metrics(request: Request, project_id: str) -> MetricsResponse:
    """Get current metrics for a project's proxy pool: the proxies of its enabled, unblocked connectors, by status.

    Healthy means healthy and not quarantined, and the counters are the project's totals plus the window the gateways
    counted since a worker's last metrics flush, so they persist across proxy rotation.
    """
    return await metrics_service_of(request).summary(project_id)


@router.get("/scaling", response_model=ScalingMetrics)
async def get_scaling_metrics(request: Request, project_id: str) -> ScalingMetrics:
    """Get auto-scaling metrics for a project: demand level, request rates and the cloud connectors' instance counts.

    Demand is what the gateways counted over the last few minutes, weighed against the project's healthy pool.
    """
    return ScalingMetrics.of(await metrics_service_of(request).scaling(project_id))


@router.get("/history", response_model=MetricsHistoryResponse)
async def get_metrics_history(
    request: Request,
    project_id: str,
    range: Annotated[MetricsRange, Query(alias="range")] = "24h",
) -> MetricsHistoryResponse:
    """Get historical metrics snapshots for a project, as the workers' metrics flushes wrote them."""
    return await metrics_service_of(request).history(project_id, range)


@router.get("/prometheus", response_class=Response)
async def prometheus_metrics(request: Request, project_id: str) -> Response:
    """Export metrics in Prometheus text format for a project (``text/plain``).

    The pool and the project's counters (which persist across proxy
    rotation) as ``/metrics`` shows them, and each connector's traffic this
    period against its limit and price, as the Connectors page shows it.
    """
    return Response(content=await metrics_service_of(request).exposition(project_id), media_type=CONTENT_TYPE)


@router.get("/traffic-split", response_model=TrafficSplitResponse)
async def get_traffic_split(
    request: Request,
    project_id: str,
    range: Annotated[MetricsRange, Query(alias="range")] = "1h",
    site: Annotated[
        str | None,
        Query(max_length=2048, description="A site (the exact host, or a URL) to weigh the connectors for under exit reputation"),
    ] = None,
    place: Annotated[
        str | None,
        Query(max_length=256, description="With a site: the requested place, a LocationTarget key such as US, US/NY or US/NY/new_york"),
    ] = None,
) -> TrafficSplitResponse:
    """Expected and observed share of traffic per connector, and with a site each connector's exit reputation there.

    The expected share is what an untargeted request would see now, from the gateway's own router over a compile of the
    project with the quarantines and the traffic blocks Redis holds, as gateways enforce them; the observed share sums
    the flushed connector metrics in the window. With a site, the effective share is a connector's share of the requests
    to the site from ``place`` among the connectors serving them, once each weight is multiplied by the connector's
    reputation there, read from the project's records in Postgres; it moves routing only where the project's exit
    reputation avoids.
    """
    return await traffic_split_service_of(request).split(project_id, range, site=site, place=place)


@router.get("/hosts", response_model=HostsResponse)
async def get_host_metrics(
    request: Request,
    project_id: str,
    range: Annotated[MetricsRange, Query(alias="range")] = "24h",
    connector_id: str | None = Query(None, description="Only traffic carried by this connector"),
    search: str | None = Query(None, max_length=255, description="Only hosts containing this text"),
    limit: int = Query(100, ge=1, le=MAX_HOSTS_PER_PAGE),
) -> HostsResponse:
    """The hosts a project requested in the window, busiest first, with totals and a per-connector split.

    Sums the host history the workers' metrics flushes write, so the last flush interval of traffic is not in it yet;
    ``totals.host_count`` is how many hosts matched, of which at most ``limit`` are returned.
    """
    return await host_metrics_service_of(request).of_project(
        project_id, range, connector_id=connector_id, search=search, limit=limit
    )


@router.get("/hosts/history", response_model=HostsHistoryResponse)
async def get_host_metrics_history(
    request: Request,
    project_id: str,
    range: Annotated[MetricsRange, Query(alias="range")] = "24h",
    connector_id: str | None = Query(None, description="Only traffic carried by this connector"),
    top: int = Query(8, ge=1, le=MAX_HOST_SERIES, description="How many hosts get a series of their own"),
    hosts: str | None = Query(
        None, max_length=4096,
        description="Comma-separated hosts to chart instead of the busiest ones",
    ),
) -> HostsHistoryResponse:
    """The busiest hosts of the window over time: ``top`` hosts (or the ``hosts`` named) each get a series, busiest first.

    Every other host is folded into one last series under ``overflow_host``, and each series carries every bucket of the
    window, with zeros where the host was quiet, so they stack without gaps.
    """
    return await host_metrics_service_of(request).history(
        project_id, range, connector_id=connector_id, top=top, hosts=hosts.split(",") if hosts else None
    )
