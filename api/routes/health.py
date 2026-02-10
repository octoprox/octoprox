"""Health check endpoints."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    proxy_count: int
    healthy_proxy_count: int


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Check the health of the service."""
    proxy_manager = request.app.state.proxy_manager
    
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        proxy_count=len(proxy_manager.proxies),
        healthy_proxy_count=len(proxy_manager.healthy_proxies),
    )


@router.get("/ready")
async def readiness_check() -> dict[str, str]:
    """Kubernetes readiness probe."""
    return {"status": "ready"}


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    """Kubernetes liveness probe."""
    return {"status": "alive"}

