# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Metrics endpoints."""

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from api.core import utc_now
from api.db.repository import MetricsRepository
from api.models.proxy import ProxyStatus
from api.providers.sdk.strategies import is_dynamic_gateway

router = APIRouter(prefix="/projects/{project_id}/metrics")


class PoolMetrics(BaseModel):
    """Proxy pool metrics."""
    total_proxies: int
    healthy_proxies: int
    unhealthy_proxies: int
    quarantined_proxies: int
    draining_proxies: int
    terminating_proxies: int
    total_requests: int
    total_successes: int
    total_failures: int
    overall_success_rate: float
    avg_latency_ms: float
    total_bytes_sent: int
    total_bytes_received: int


class ScalingMetrics(BaseModel):
    """Auto-scaling metrics."""
    demand_level: str
    requests_per_minute: float
    rate_per_proxy: float
    current_instances: int
    healthy_instances: int
    min_instances: int
    max_instances: int
    draining_instances: int
    terminating_instances: int


class StrategyMetrics(BaseModel):
    """Routing strategy metrics."""
    current_strategy: str
    available_strategies: list[str]


class MetricsResponse(BaseModel):
    """Combined metrics response."""
    pool: PoolMetrics
    strategy: StrategyMetrics


@router.get("", response_model=MetricsResponse)
async def get_metrics(request: Request, project_id: str) -> MetricsResponse:
    """Get current metrics for a project's proxy pool.

    Uses project-level metrics stored on the Project model which combines:
    - Historical totals from Postgres (loaded on startup)
    - Current window increments (updated in real-time)

    These metrics persist across proxy rotation.
    """
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy = proxy_manager.get_healthy_proxies_for_project(project_id)
    current_strategy = proxy_manager._project_strategies.get(
        project_id, proxy_manager._strategy
    ).name

    # Count draining and terminating proxies
    draining_count = sum(1 for p in proxies if p.status == ProxyStatus.DRAINING)
    terminating_count = sum(1 for p in proxies if p.status == ProxyStatus.TERMINATING)
    quarantined_count = proxy_manager.get_quarantined_count_for_project(project_id)

    # Get project-level metrics directly from the Project model
    overall_success_rate = 0.0
    if project.request_count > 0:
        overall_success_rate = (project.success_count / project.request_count) * 100

    return MetricsResponse(
        pool=PoolMetrics(
            total_proxies=len(proxies),
            healthy_proxies=len(healthy),
            unhealthy_proxies=len(proxies) - len(healthy) - draining_count - terminating_count,
            quarantined_proxies=quarantined_count,
            draining_proxies=draining_count,
            terminating_proxies=terminating_count,
            total_requests=project.request_count,
            total_successes=project.success_count,
            total_failures=project.failure_count,
            overall_success_rate=round(overall_success_rate, 2),
            avg_latency_ms=round(project.avg_latency_ms, 2),
            total_bytes_sent=project.bytes_sent,
            total_bytes_received=project.bytes_received,
        ),
        strategy=StrategyMetrics(
            current_strategy=current_strategy,
            available_strategies=[
                "round_robin",
                "least_used",
                "random",
                "sticky",
                "health_based",
            ],
        ),
    )


@router.get("/scaling", response_model=ScalingMetrics)
async def get_scaling_metrics(request: Request, project_id: str) -> ScalingMetrics:
    """Get auto-scaling metrics for a project.

    Returns demand level, request rates, and instance counts for cloud provider
    connectors only (AWS, GCP, Azure) - these are the ones with auto-scaling.
    Static proxy providers are excluded.
    """
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get all connectors and filter to enabled cloud providers only
    all_connectors = proxy_manager.get_connectors_for_project(project_id)
    cloud_connector_ids: set[str] = set()
    min_instances = 0
    max_instances = 0

    for connector in all_connectors:
        if not connector.enabled:
            continue
        # Check if this is a cloud connector using typed config
        cloud_config = connector.cloud_config
        if not cloud_config:
            continue
        # This is an enabled cloud provider connector
        cloud_connector_ids.add(connector.id)
        min_instances += cloud_config.min_proxies
        max_instances += cloud_config.max_proxies

    # Get proxies only from cloud provider connectors
    all_proxies = proxy_manager.get_proxies_for_project(project_id)
    cloud_proxies = [p for p in all_proxies if p.connector_id in cloud_connector_ids]
    healthy_cloud_proxies = [p for p in cloud_proxies if p.status == ProxyStatus.HEALTHY]

    # Count draining and terminating (only cloud proxies)
    draining_count = sum(1 for p in cloud_proxies if p.status == ProxyStatus.DRAINING)
    terminating_count = sum(1 for p in cloud_proxies if p.status == ProxyStatus.TERMINATING)

    # Get demand info from the demand tracker
    demand_info = await proxy_manager.get_demand_info(project_id)

    # demand_info returns "demand_level" as a DemandLevel enum
    demand_level = demand_info.get("demand_level", "LOW")
    # Convert enum to string if needed
    if hasattr(demand_level, "value"):
        demand_level = demand_level.value.upper()
    else:
        demand_level = str(demand_level).upper()

    return ScalingMetrics(
        demand_level=demand_level,
        requests_per_minute=demand_info.get("requests_per_minute", 0.0),
        rate_per_proxy=demand_info.get("rate_per_proxy", 0.0),
        current_instances=len(cloud_proxies),
        healthy_instances=len(healthy_cloud_proxies),
        min_instances=min_instances,
        max_instances=max_instances,
        draining_instances=draining_count,
        terminating_instances=terminating_count,
    )


class MetricsSnapshot(BaseModel):
    """A single metrics snapshot."""
    timestamp: datetime
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class MetricsHistoryResponse(BaseModel):
    """Historical metrics response."""
    snapshots: list[MetricsSnapshot]


class ConnectorTrafficShare(BaseModel):
    """One connector's slice of a project's traffic under the current weights."""
    connector_id: str
    name: str
    credential_type: str
    enabled: bool
    weight: int
    dynamic: bool
    total_proxies: int
    eligible_proxies: int
    # Share of untargeted requests this connector takes right now, 0-100.
    # Zero when the connector is disabled or has no eligible proxy.
    expected_share: float
    # Why the connector takes nothing, when it does: "disabled" or "no_eligible_proxies".
    excluded_reason: str | None = None
    observed_requests: int
    # Share of the project's requests in the window that went through this connector, 0-100.
    observed_share: float | None = None


class TrafficSplitResponse(BaseModel):
    """How a project's traffic divides between its connectors, expected and observed."""
    strategy: str
    range: str
    total_weight: int
    observed_requests: int
    connectors: list[ConnectorTrafficShare]


# (delta, limit for raw queries, bucket_seconds for aggregated queries)
# Raw ranges return individual snapshots; aggregated ranges group into time buckets.
RANGE_CONFIG: dict[str, tuple[timedelta, int, int | None]] = {
    "1h":  (timedelta(hours=1),  60,   None),
    "6h":  (timedelta(hours=6),  360,  None),
    "24h": (timedelta(hours=24), 1440, None),
    "7d":  (timedelta(days=7),   0,    3600),      # 1-hour buckets → ~168 points
    "30d": (timedelta(days=30),  0,    3600 * 6),   # 6-hour buckets → ~120 points
}


@router.get("/history", response_model=MetricsHistoryResponse)
async def get_metrics_history(
    request: Request,
    project_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> MetricsHistoryResponse:
    """Get historical metrics snapshots for a project."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    delta, limit, bucket_seconds = RANGE_CONFIG[range]
    since = utc_now() - delta

    async with proxy_manager._session_factory() as session:
        repo = MetricsRepository(session)
        if bucket_seconds:
            rows = await repo.get_project_metrics_history_aggregated(
                project_id=project_id,
                since=since,
                bucket_seconds=bucket_seconds,
            )
        else:
            rows = await repo.get_project_metrics_history(
                project_id=project_id,
                since=since,
                limit=limit,
                granularity=60,
            )

    # Rows come back in descending order; reverse to chronological
    snapshots = [MetricsSnapshot(**row) for row in reversed(rows)]
    return MetricsHistoryResponse(snapshots=snapshots)


@router.get("/prometheus")
async def prometheus_metrics(request: Request, project_id: str) -> str:
    """Export metrics in Prometheus format for a project.

    Uses project-level metrics from the Project model which persist across proxy rotation.
    """
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy = proxy_manager.get_healthy_proxies_for_project(project_id)
    quarantined = proxy_manager.get_quarantined_count_for_project(project_id)

    label_str = f'{{project="{project_id}"}}'

    lines = [
        "# HELP octoprox_proxies_total Total number of proxies in the pool",
        "# TYPE octoprox_proxies_total gauge",
        f"octoprox_proxies_total{label_str} {len(proxies)}",
        "",
        "# HELP octoprox_proxies_healthy Number of healthy proxies",
        "# TYPE octoprox_proxies_healthy gauge",
        f"octoprox_proxies_healthy{label_str} {len(healthy)}",
        "",
        "# HELP octoprox_proxies_quarantined Number of quarantined (rate-limited) proxies",
        "# TYPE octoprox_proxies_quarantined gauge",
        f"octoprox_proxies_quarantined{label_str} {quarantined}",
        "",
        "# HELP octoprox_requests_total Total number of requests processed",
        "# TYPE octoprox_requests_total counter",
        f"octoprox_requests_total{label_str} {project.request_count}",
        "",
        "# HELP octoprox_requests_success_total Total successful requests",
        "# TYPE octoprox_requests_success_total counter",
        f"octoprox_requests_success_total{label_str} {project.success_count}",
        "",
        "# HELP octoprox_requests_failure_total Total failed requests",
        "# TYPE octoprox_requests_failure_total counter",
        f"octoprox_requests_failure_total{label_str} {project.failure_count}",
        "",
        "# HELP octoprox_bytes_sent_total Total bytes sent through proxies",
        "# TYPE octoprox_bytes_sent_total counter",
        f"octoprox_bytes_sent_total{label_str} {project.bytes_sent}",
        "",
        "# HELP octoprox_bytes_received_total Total bytes received through proxies",
        "# TYPE octoprox_bytes_received_total counter",
        f"octoprox_bytes_received_total{label_str} {project.bytes_received}",
    ]

    return "\n".join(lines)


@router.get("/traffic-split", response_model=TrafficSplitResponse)
async def get_traffic_split(
    request: Request,
    project_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("1h", alias="range"),
) -> TrafficSplitResponse:
    """Expected and observed share of traffic per connector.

    The expected share is what an untargeted request (no ``-cc-``, no
    domain restriction in play) would see right now: each enabled connector
    with at least one eligible proxy takes ``weight / sum of weights``.
    Requests that carry a country or hit a filtered domain narrow the set
    and the remaining connectors split the traffic by the same ratios.

    The observed share sums the flushed per-proxy request counts in the
    window per connector. Proxies removed since then take their history
    with them, so the two can disagree right after a rotation.
    """
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    connectors = proxy_manager.get_connectors_for_project(project_id)
    eligible_groups = {
        g.key: g for g in proxy_manager.group_by_connector(
            proxy_manager.get_routable_proxies_for_project(project_id)
        )
    }
    total_weight = sum(g.weight for g in eligible_groups.values())

    delta, _limit, _bucket = RANGE_CONFIG[range]
    async with proxy_manager._session_factory() as session:
        counts = await MetricsRepository(session).get_connector_request_counts_since(
            [c.id for c in connectors], utc_now() - delta
        )
    observed_total = sum(counts.values())

    shares: list[ConnectorTrafficShare] = []
    for connector in sorted(connectors, key=lambda c: c.name.lower()):
        rows = proxy_manager.get_active_proxies_for_connector(connector.id)
        group = eligible_groups.get(connector.id)
        eligible = len(group.proxies) if group else 0
        if not connector.enabled:
            reason: str | None = "disabled"
        elif group is None:
            reason = "no_eligible_proxies"
        else:
            reason = None
        expected = (group.weight / total_weight * 100) if group and total_weight else 0.0
        observed = counts.get(connector.id, 0)
        shares.append(ConnectorTrafficShare(
            connector_id=connector.id,
            name=connector.name,
            credential_type=connector.credential_type,
            enabled=connector.enabled,
            weight=connector.weight,
            dynamic=any(is_dynamic_gateway(p) for p in rows),
            total_proxies=len(rows),
            eligible_proxies=eligible,
            expected_share=round(expected, 2),
            excluded_reason=reason,
            observed_requests=observed,
            observed_share=round(observed / observed_total * 100, 2) if observed_total else None,
        ))

    return TrafficSplitResponse(
        strategy=proxy_manager.strategy_for_project(project_id).name,
        range=range,
        total_weight=total_weight,
        observed_requests=observed_total,
        connectors=shares,
    )
