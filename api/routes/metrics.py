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
    """Get current metrics for a project's proxy pool."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy = proxy_manager.get_healthy_proxies_for_project(project_id)
    current_strategy = proxy_manager._project_strategies.get(
        project_id, proxy_manager._strategy
    ).name

    # Calculate aggregate metrics
    total_requests = sum(p.request_count for p in proxies)
    total_successes = sum(p.success_count for p in proxies)
    total_failures = sum(p.failure_count for p in proxies)

    overall_success_rate = 0.0
    if total_requests > 0:
        overall_success_rate = (total_successes / total_requests) * 100

    avg_latency = 0.0
    latency_proxies = [p for p in proxies if p.avg_latency_ms > 0]
    if latency_proxies:
        avg_latency = sum(p.avg_latency_ms for p in latency_proxies) / len(latency_proxies)

    return MetricsResponse(
        pool=PoolMetrics(
            total_proxies=len(proxies),
            healthy_proxies=len(healthy),
            unhealthy_proxies=len(proxies) - len(healthy),
            total_requests=total_requests,
            total_successes=total_successes,
            total_failures=total_failures,
            overall_success_rate=round(overall_success_rate, 2),
            avg_latency_ms=round(avg_latency, 2),
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
    """Export metrics in Prometheus format for a project."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy = proxy_manager.get_healthy_proxies_for_project(project_id)

    total_requests = sum(p.request_count for p in proxies)
    total_successes = sum(p.success_count for p in proxies)
    total_failures = sum(p.failure_count for p in proxies)

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
        f"octoprox_requests_total{label_str} {total_requests}",
        "",
        "# HELP octoprox_requests_success_total Total successful requests",
        "# TYPE octoprox_requests_success_total counter",
        f"octoprox_requests_success_total{label_str} {total_successes}",
        "",
        "# HELP octoprox_requests_failure_total Total failed requests",
        "# TYPE octoprox_requests_failure_total counter",
        f"octoprox_requests_failure_total{label_str} {total_failures}",
    ]

    return "\n".join(lines)

