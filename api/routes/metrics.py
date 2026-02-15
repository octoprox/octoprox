"""Metrics endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/projects/{project_id}/metrics")


class PoolMetrics(BaseModel):
    """Proxy pool metrics."""
    total_proxies: int
    healthy_proxies: int
    unhealthy_proxies: int
    total_requests: int
    total_successes: int
    total_failures: int
    overall_success_rate: float
    avg_latency_ms: float
    total_bytes_sent: int
    total_bytes_received: int


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

    # Get project-level metrics directly from the Project model
    overall_success_rate = 0.0
    if project.request_count > 0:
        overall_success_rate = (project.success_count / project.request_count) * 100

    return MetricsResponse(
        pool=PoolMetrics(
            total_proxies=len(proxies),
            healthy_proxies=len(healthy),
            unhealthy_proxies=len(proxies) - len(healthy),
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

