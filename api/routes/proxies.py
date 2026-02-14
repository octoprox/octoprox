"""Proxy management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.proxy import Proxy, ProxyCreate, ProxyResponse, ProxyUpdate
from api.models.credential import CredentialType

router = APIRouter(prefix="/projects/{project_id}/proxies")


class ProxyListResponse(BaseModel):
    """Response for listing proxies."""
    total: int
    healthy: int
    proxies: list[ProxyResponse]


class StrategyRequest(BaseModel):
    """Request to change routing strategy."""
    strategy: str


def _proxy_to_response(proxy: Proxy, connector_name: str | None = None) -> ProxyResponse:
    """Convert a Proxy to ProxyResponse."""
    return ProxyResponse(
        id=proxy.id,
        host=proxy.host,
        port=proxy.port,
        protocol=proxy.protocol.value if hasattr(proxy.protocol, 'value') else proxy.protocol,
        username=proxy.username,
        password=proxy.password,
        connector_id=proxy.connector_id,
        connector_name=connector_name,
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
async def list_proxies(request: Request, project_id: str) -> ProxyListResponse:
    """List all proxies for a project."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy_proxies = proxy_manager.get_healthy_proxies_for_project(project_id)

    # Build responses with connector names
    responses = []
    for p in proxies:
        connector = proxy_manager.get_connector(p.connector_id)
        connector_name = connector.name if connector else None
        responses.append(_proxy_to_response(p, connector_name))

    return ProxyListResponse(
        total=len(proxies),
        healthy=len(healthy_proxies),
        proxies=responses,
    )


@router.post("", response_model=ProxyResponse, status_code=201)
async def create_proxy(request: Request, proxy_data: ProxyCreate, project_id: str) -> ProxyResponse:
    """Add a new proxy to the pool. Only allowed for STATIC_PROXY_PROVIDER connectors."""
    proxy_manager = request.app.state.proxy_manager

    # Validate project exists
    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate that the connector exists
    connector = proxy_manager.get_connector(proxy_data.connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    # Validate connector belongs to this project
    if connector.project_id != project_id:
        raise HTTPException(status_code=400, detail="Connector does not belong to this project")

    # Validate that the connector is a STATIC_PROXY_PROVIDER type
    credential = proxy_manager.get_credential(connector.credential_id)
    if credential is None:
        raise HTTPException(status_code=400, detail="Connector's credential not found")

    credential_type = credential.type if isinstance(credential.type, str) else credential.type.value
    if credential_type != CredentialType.STATIC_PROXY_PROVIDER.value:
        raise HTTPException(
            status_code=400,
            detail="Proxies can only be manually added to STATIC_PROXY_PROVIDER connectors"
        )

    proxy = Proxy(
        host=proxy_data.host,
        port=proxy_data.port,
        protocol=proxy_data.protocol,
        username=proxy_data.username,
        password=proxy_data.password,
        connector_id=proxy_data.connector_id,
        tags=proxy_data.tags,
        metadata=proxy_data.metadata,
    )

    await proxy_manager.add_proxy(proxy)

    # Update connector proxy count
    connector.proxy_count += 1

    return _proxy_to_response(proxy, connector.name)


@router.get("/{proxy_id}", response_model=ProxyResponse)
async def get_proxy(request: Request, proxy_id: str) -> ProxyResponse:
    """Get a specific proxy by ID."""
    proxy_manager = request.app.state.proxy_manager
    proxy = proxy_manager.get_proxy(proxy_id)

    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")

    connector = proxy_manager.get_connector(proxy.connector_id)
    connector_name = connector.name if connector else None
    return _proxy_to_response(proxy, connector_name)


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

    # Persist the update
    await proxy_manager.update_proxy(proxy)

    connector = proxy_manager.get_connector(proxy.connector_id)
    connector_name = connector.name if connector else None
    return _proxy_to_response(proxy, connector_name)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(request: Request, proxy_id: str) -> None:
    """Remove a proxy from the pool."""
    proxy_manager = request.app.state.proxy_manager

    # Get proxy to update connector count before deletion
    proxy = proxy_manager.get_proxy(proxy_id)
    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")

    # Update connector proxy count
    connector = proxy_manager.get_connector(proxy.connector_id)
    if connector and connector.proxy_count > 0:
        connector.proxy_count -= 1

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
