# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Metrics endpoints."""

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from api.core import utc_now
from api.core.stats import HOST_OVERFLOW, normalize_host
from api.db.repository import MetricsRepository
from api.models.proxy import ProxyStatus
from api.providers.sdk.strategies import is_dynamic_gateway
from api.routes.common import proxy_manager_of

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
    proxy_manager = proxy_manager_of(request)

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
    proxy_manager = proxy_manager_of(request)

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
    # Why the connector takes nothing, when it does: "disabled", "traffic_limit"
    # (blocked by its traffic limit) or "no_eligible_proxies".
    excluded_reason: str | None = None
    observed_requests: int
    # Share of the project's requests in the window that went through this connector, 0-100.
    observed_share: float | None = None
    # Bytes, both directions, the connector carried in the window, and what
    # they cost at its price per GB (None when the connector is unpriced).
    observed_bytes: int = 0
    cost: float | None = None
    currency: str | None = None


class TrafficSplitResponse(BaseModel):
    """How a project's traffic divides between its connectors, expected and observed."""
    strategy: str
    range: str
    total_weight: int
    observed_requests: int
    observed_bytes: int = 0
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
    proxy_manager = proxy_manager_of(request)

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
    proxy_manager = proxy_manager_of(request)

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

    # Per-connector traffic this period, against the limit and the price.
    connectors = sorted(proxy_manager.get_connectors_for_project(project_id), key=lambda c: c.name.lower())
    usage_lines: list[str] = []
    limit_lines: list[str] = []
    cost_lines: list[str] = []
    blocked_lines: list[str] = []
    for connector in connectors:
        usage = proxy_manager.traffic_usage(connector)
        labels = f'{{project="{project_id}",connector="{connector.id}",name="{_label_value(connector.name)}"}}'
        usage_lines.append(f"octoprox_connector_traffic_bytes{labels} {usage.total_bytes}")
        if usage.limit_bytes is not None:
            limit_lines.append(f"octoprox_connector_traffic_limit_bytes{labels} {usage.limit_bytes}")
        if usage.cost is not None:
            cost_lines.append(
                f'octoprox_connector_traffic_cost{{project="{project_id}",connector="{connector.id}",'
                f'name="{_label_value(connector.name)}",currency="{usage.currency}"}} {usage.cost}'
            )
        blocked_lines.append(f"octoprox_connector_traffic_blocked{labels} {1 if usage.blocked else 0}")
    if connectors:
        lines += [
            "",
            "# HELP octoprox_connector_traffic_bytes Bytes through the connector in its current traffic period",
            "# TYPE octoprox_connector_traffic_bytes gauge",
            *usage_lines,
        ]
        if limit_lines:
            lines += [
                "",
                "# HELP octoprox_connector_traffic_limit_bytes The connector's traffic limit for the period",
                "# TYPE octoprox_connector_traffic_limit_bytes gauge",
                *limit_lines,
            ]
        if cost_lines:
            lines += [
                "",
                "# HELP octoprox_connector_traffic_cost Spend in the current period at the connector's price per GB",
                "# TYPE octoprox_connector_traffic_cost gauge",
                *cost_lines,
            ]
        lines += [
            "",
            "# HELP octoprox_connector_traffic_blocked 1 while the connector takes no requests because of its traffic limit",
            "# TYPE octoprox_connector_traffic_blocked gauge",
            *blocked_lines,
        ]

    return "\n".join(lines)


def _label_value(value: str) -> str:
    """Escape a string for a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


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

    The observed share sums the flushed connector-level metrics in the
    window, which outlive the connector's proxies, so a rotation does not
    make the numbers disagree.
    """
    proxy_manager = proxy_manager_of(request)

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
    since = utc_now() - delta
    async with proxy_manager._session_factory() as session:
        totals = await MetricsRepository(session).get_connector_totals_since(
            {c.id: since for c in connectors}
        )
    observed_total = sum(t["request_count"] for t in totals.values())
    observed_bytes_total = sum(t["bytes_sent"] + t["bytes_received"] for t in totals.values())

    shares: list[ConnectorTrafficShare] = []
    for connector in sorted(connectors, key=lambda c: c.name.lower()):
        rows = proxy_manager.get_active_proxies_for_connector(connector.id)
        group = eligible_groups.get(connector.id)
        eligible = len(group.proxies) if group else 0
        if not connector.enabled:
            reason: str | None = "disabled"
        elif proxy_manager.is_traffic_blocked(connector.id):
            reason = "traffic_limit"
        elif group is None:
            reason = "no_eligible_proxies"
        else:
            reason = None
        expected = (group.weight / total_weight * 100) if group and total_weight else 0.0
        observed = totals.get(connector.id, {})
        requests = int(observed.get("request_count", 0))
        observed_bytes = int(observed.get("bytes_sent", 0)) + int(observed.get("bytes_received", 0))
        traffic_config = connector.parsed_traffic_config
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
            observed_requests=requests,
            observed_share=round(requests / observed_total * 100, 2) if observed_total else None,
            observed_bytes=observed_bytes,
            cost=traffic_config.cost_of(observed_bytes),
            currency=traffic_config.currency if traffic_config.price_per_gb is not None else None,
        ))

    return TrafficSplitResponse(
        strategy=proxy_manager.strategy_for_project(project_id).name,
        range=range,
        total_weight=total_weight,
        observed_requests=observed_total,
        observed_bytes=observed_bytes_total,
        connectors=shares,
    )


# ---------------------------------------------------------------------------
# Hosts: where a project's traffic goes, by destination host and connector.


class HostConnectorMetrics(BaseModel):
    """One connector's part of a host's traffic in the window."""
    connector_id: str
    # None when the connector was deleted after the rows were written (they
    # cascade with it, so this is a flush-window race, not a lasting state).
    name: str | None = None
    credential_type: str | None = None
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostMetrics(BaseModel):
    """A destination host's traffic in the window, in total and per connector."""
    host: str
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int
    # Share of the window's requests and bytes (both directions), 0-100.
    request_share: float
    bytes_share: float
    connectors: list[HostConnectorMetrics]


class HostsTotals(BaseModel):
    """The window's totals over every host that matched."""
    host_count: int
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostsConnector(BaseModel):
    """A connector of the project, for the filter and the legend."""
    connector_id: str
    name: str
    credential_type: str
    enabled: bool


class HostsResponse(BaseModel):
    """A project's requested hosts in the window, busiest first."""
    range: str
    since: datetime
    # The row hosts past the per-window cap are folded into, when present.
    overflow_host: str = HOST_OVERFLOW
    # Whether per-host counting is on at all (``metrics.hosts.enabled``).
    enabled: bool
    # How many hosts the response holds at most; ``totals.host_count`` says
    # how many matched. Search to reach the rest.
    limit: int
    totals: HostsTotals
    connectors: list[HostsConnector]
    hosts: list[HostMetrics]


class HostSeriesPoint(BaseModel):
    """One bucket of one host's series."""
    timestamp: datetime
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostSeries(BaseModel):
    """One host's traffic over time."""
    host: str
    points: list[HostSeriesPoint]


class HostsHistoryResponse(BaseModel):
    """The busiest hosts of the window over time, with the rest folded into one series."""
    range: str
    since: datetime
    bucket_seconds: int
    overflow_host: str = HOST_OVERFLOW
    series: list[HostSeries]


# Bucket width per range for the per-host chart. Every range is bucketed
# (there are several series), sized to a hundred-odd points each.
HOST_HISTORY_BUCKETS: dict[str, int] = {
    "1h": 60,
    "6h": 300,
    "24h": 900,
    "7d": 3600,
    "30d": 3600 * 6,
}

MAX_HOSTS_PER_PAGE = 500
MAX_HOST_SERIES = 12


@router.get("/hosts", response_model=HostsResponse)
async def get_host_metrics(
    request: Request,
    project_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
    connector_id: str | None = Query(None, description="Only traffic carried by this connector"),
    search: str | None = Query(None, max_length=255, description="Only hosts containing this text"),
    limit: int = Query(100, ge=1, le=MAX_HOSTS_PER_PAGE),
) -> HostsResponse:
    """The hosts a project requested in the window, with totals and a per-connector split.

    Sums the flushed host history (``host_metrics``), which the leader
    writes every flush interval, so the last minute or so of traffic is
    not in the numbers yet. A request is the CONNECT tunnel or the plain
    HTTP exchange; see the metrics docs for what that means under
    keep-alive. Hosts are returned busiest first, at most ``limit`` of
    them; ``totals.host_count`` is how many matched in all.
    """
    proxy_manager = proxy_manager_of(request)

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    connectors = {c.id: c for c in proxy_manager.get_connectors_for_project(project_id)}
    if connector_id is not None and connector_id not in connectors:
        raise HTTPException(status_code=404, detail="Connector not found in this project")

    delta, _limit, _bucket = RANGE_CONFIG[range]
    since = utc_now() - delta
    search = search.strip() if search else None
    async with proxy_manager._session_factory() as session:
        repo = MetricsRepository(session)
        summary = await repo.get_host_summary(project_id, since, connector_id=connector_id, search=search or None)
        rows = await repo.get_host_totals(
            project_id, since, connector_id=connector_id, search=search or None, limit=limit
        )
        breakdown = await repo.get_host_connector_breakdown(
            project_id, since, [r["host"] for r in rows], connector_id=connector_id
        )

    total_requests = summary["request_count"]
    total_bytes = summary["bytes_sent"] + summary["bytes_received"]
    hosts: list[HostMetrics] = []
    for row in rows:
        host_bytes = row["bytes_sent"] + row["bytes_received"]
        per_connector = []
        for part in breakdown.get(row["host"], []):
            connector = connectors.get(part["connector_id"])
            per_connector.append(HostConnectorMetrics(
                connector_id=part["connector_id"],
                name=connector.name if connector else None,
                credential_type=connector.credential_type if connector else None,
                request_count=part["request_count"],
                success_count=part["success_count"],
                failure_count=part["failure_count"],
                avg_latency_ms=round(part["avg_latency_ms"], 2),
                bytes_sent=part["bytes_sent"],
                bytes_received=part["bytes_received"],
            ))
        hosts.append(HostMetrics(
            host=row["host"],
            request_count=row["request_count"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            avg_latency_ms=round(row["avg_latency_ms"], 2),
            bytes_sent=row["bytes_sent"],
            bytes_received=row["bytes_received"],
            request_share=round(row["request_count"] / total_requests * 100, 2) if total_requests else 0.0,
            bytes_share=round(host_bytes / total_bytes * 100, 2) if total_bytes else 0.0,
            connectors=per_connector,
        ))

    return HostsResponse(
        range=range,
        since=since,
        enabled=proxy_manager._settings.host_metrics_enabled,
        limit=limit,
        totals=HostsTotals(
            host_count=summary["host_count"],
            request_count=summary["request_count"],
            success_count=summary["success_count"],
            failure_count=summary["failure_count"],
            avg_latency_ms=round(summary["avg_latency_ms"], 2),
            bytes_sent=summary["bytes_sent"],
            bytes_received=summary["bytes_received"],
        ),
        connectors=[
            HostsConnector(
                connector_id=c.id, name=c.name, credential_type=c.credential_type, enabled=c.enabled
            )
            for c in sorted(connectors.values(), key=lambda c: c.name.lower())
        ],
        hosts=hosts,
    )


@router.get("/hosts/history", response_model=HostsHistoryResponse)
async def get_host_metrics_history(
    request: Request,
    project_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
    connector_id: str | None = Query(None, description="Only traffic carried by this connector"),
    top: int = Query(8, ge=1, le=MAX_HOST_SERIES, description="How many hosts get a series of their own"),
    hosts: str | None = Query(
        None, max_length=4096,
        description="Comma-separated hosts to chart instead of the busiest ones",
    ),
) -> HostsHistoryResponse:
    """The busiest hosts of the window over time, every other host folded into one series.

    ``top`` hosts by requests (or the ``hosts`` named) get a series each, in
    fixed buckets sized to the range; the remainder is one series under
    ``overflow_host``. Series are ordered busiest first, the remainder last,
    and each carries every bucket of the window with zeros where the host
    was quiet, so they stack without gaps.
    """
    proxy_manager = proxy_manager_of(request)

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if connector_id is not None and all(
        c.id != connector_id for c in proxy_manager.get_connectors_for_project(project_id)
    ):
        raise HTTPException(status_code=404, detail="Connector not found in this project")

    delta, _limit, _bucket = RANGE_CONFIG[range]
    bucket_seconds = HOST_HISTORY_BUCKETS[range]
    since = utc_now() - delta
    async with proxy_manager._session_factory() as session:
        repo = MetricsRepository(session)
        if hosts:
            # The stored form (normalize_host), each host once, in the order given.
            named = (normalize_host(part) for part in hosts.split(","))
            chosen = list(dict.fromkeys(h for h in named if h is not None))[:MAX_HOST_SERIES]
        else:
            chosen = [
                r["host"] for r in await repo.get_host_totals(project_id, since, connector_id=connector_id, limit=top)
            ]
        rows = await repo.get_host_history_aggregated(
            project_id, since, bucket_seconds, chosen, connector_id=connector_id
        )

    # Every series gets every bucket, so the chart stacks cleanly.
    timestamps = sorted({row["timestamp"] for row in rows})
    by_series: dict[str, dict[datetime, dict[str, Any]]] = {host: {} for host in chosen}
    for row in rows:
        by_series.setdefault(row["host"], {})[row["timestamp"]] = row
    order = [*chosen, *(h for h in by_series if h not in chosen)]
    series: list[HostSeries] = []
    for host in order:
        points = by_series.get(host, {})
        if not points and host not in chosen:
            continue
        series.append(HostSeries(
            host=host,
            points=[
                HostSeriesPoint(
                    timestamp=ts,
                    request_count=p.get("request_count", 0),
                    success_count=p.get("success_count", 0),
                    failure_count=p.get("failure_count", 0),
                    avg_latency_ms=round(float(p.get("avg_latency_ms", 0.0)), 2),
                    bytes_sent=p.get("bytes_sent", 0),
                    bytes_received=p.get("bytes_received", 0),
                )
                for ts in timestamps
                for p in [points.get(ts, {})]
            ],
        ))

    return HostsHistoryResponse(range=range, since=since, bucket_seconds=bucket_seconds, series=series)
