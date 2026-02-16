"""Metrics endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.credential import CredentialType
from api.models.proxy import ProxyStatus

# Cloud provider credential types that support auto-scaling
CLOUD_PROVIDER_TYPES = {CredentialType.AWS, CredentialType.GCP, CredentialType.AZURE}

router = APIRouter(prefix="/projects/{project_id}/metrics")


class PoolMetrics(BaseModel):
    """Proxy pool metrics."""
    total_proxies: int
    healthy_proxies: int
    unhealthy_proxies: int
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

    # Get project-level metrics directly from the Project model
    overall_success_rate = 0.0
    if project.request_count > 0:
        overall_success_rate = (project.success_count / project.request_count) * 100

    return MetricsResponse(
        pool=PoolMetrics(
            total_proxies=len(proxies),
            healthy_proxies=len(healthy),
            unhealthy_proxies=len(proxies) - len(healthy) - draining_count - terminating_count,
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
        # Get credential to check type
        credential = proxy_manager.get_credential(connector.credential_id)
        if not credential or credential.type not in CLOUD_PROVIDER_TYPES:
            continue
        # This is an enabled cloud provider connector
        cloud_connector_ids.add(connector.id)
        config = connector.config or {}
        min_instances += config.get("min_proxies", 1)
        max_instances += config.get("max_proxies", 10)

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

