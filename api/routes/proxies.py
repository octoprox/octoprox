"""Proxy management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.proxy import Proxy, ProxyCreate, ProxyResponse, ProxyUpdate

router = APIRouter()


class ProxyListResponse(BaseModel):
    """Response for listing proxies."""
    total: int
    healthy: int
    proxies: list[ProxyResponse]


class StrategyRequest(BaseModel):
    """Request to change routing strategy."""
    strategy: str


def _proxy_to_response(proxy: Proxy) -> ProxyResponse:
    """Convert a Proxy to ProxyResponse."""
    return ProxyResponse(
        id=proxy.id,
        host=proxy.host,
        port=proxy.port,
        protocol=proxy.protocol.value if hasattr(proxy.protocol, 'value') else proxy.protocol,
        status=proxy.status.value if hasattr(proxy.status, 'value') else proxy.status,
        request_count=proxy.request_count,
        success_count=proxy.success_count,
        failure_count=proxy.failure_count,
        success_rate=proxy.success_rate,
        avg_latency_ms=proxy.avg_latency_ms,
        tags=proxy.tags,
        created_at=proxy.created_at,
    )


@router.get("", response_model=ProxyListResponse)
async def list_proxies(request: Request) -> ProxyListResponse:
    """List all proxies in the pool."""
    proxy_manager = request.app.state.proxy_manager
    proxies = proxy_manager.proxies
    
    return ProxyListResponse(
        total=len(proxies),
        healthy=len(proxy_manager.healthy_proxies),
        proxies=[_proxy_to_response(p) for p in proxies],
    )


@router.post("", response_model=ProxyResponse, status_code=201)
async def create_proxy(request: Request, proxy_data: ProxyCreate) -> ProxyResponse:
    """Add a new proxy to the pool."""
    proxy_manager = request.app.state.proxy_manager

    # Validate that the source exists
    source = proxy_manager.get_source(proxy_data.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")

    proxy = Proxy(
        host=proxy_data.host,
        port=proxy_data.port,
        protocol=proxy_data.protocol,
        username=proxy_data.username,
        password=proxy_data.password,
        source_id=proxy_data.source_id,
        tags=proxy_data.tags,
        metadata=proxy_data.metadata,
    )

    await proxy_manager.add_proxy(proxy)

    # Update source proxy count
    source.proxy_count += 1

    return _proxy_to_response(proxy)


@router.get("/{proxy_id}", response_model=ProxyResponse)
async def get_proxy(request: Request, proxy_id: str) -> ProxyResponse:
    """Get a specific proxy by ID."""
    proxy_manager = request.app.state.proxy_manager
    proxy = proxy_manager.get_proxy(proxy_id)
    
    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    return _proxy_to_response(proxy)


@router.patch("/{proxy_id}", response_model=ProxyResponse)
async def update_proxy(
    request: Request, proxy_id: str, proxy_data: ProxyUpdate
) -> ProxyResponse:
    """Update a proxy."""
    proxy_manager = request.app.state.proxy_manager
    proxy = proxy_manager.get_proxy(proxy_id)
    
    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    # Update fields
    if proxy_data.host is not None:
        proxy.host = proxy_data.host
    if proxy_data.port is not None:
        proxy.port = proxy_data.port
    if proxy_data.protocol is not None:
        proxy.protocol = proxy_data.protocol
    if proxy_data.username is not None:
        proxy.username = proxy_data.username
    if proxy_data.password is not None:
        proxy.password = proxy_data.password
    if proxy_data.tags is not None:
        proxy.tags = proxy_data.tags
    if proxy_data.metadata is not None:
        proxy.metadata = proxy_data.metadata
    
    return _proxy_to_response(proxy)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(request: Request, proxy_id: str) -> None:
    """Remove a proxy from the pool."""
    proxy_manager = request.app.state.proxy_manager

    # Get proxy to update source count before deletion
    proxy = proxy_manager.get_proxy(proxy_id)
    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")

    # Update source proxy count
    source = proxy_manager.get_source(proxy.source_id)
    if source and source.proxy_count > 0:
        source.proxy_count -= 1

    await proxy_manager.remove_proxy(proxy_id)


@router.post("/strategy")
async def set_strategy(request: Request, strategy_req: StrategyRequest) -> dict[str, str]:
    """Change the routing strategy."""
    proxy_manager = request.app.state.proxy_manager
    
    try:
        proxy_manager.set_strategy(strategy_req.strategy)
        return {"status": "ok", "strategy": strategy_req.strategy}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/select/next")
async def select_next_proxy(
    request: Request, session_id: str | None = None
) -> ProxyResponse:
    """Select the next proxy using the current routing strategy."""
    proxy_manager = request.app.state.proxy_manager
    proxy = proxy_manager.select_proxy(session_id)
    
    if proxy is None:
        raise HTTPException(status_code=503, detail="No healthy proxies available")
    
    return _proxy_to_response(proxy)

