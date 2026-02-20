# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy management endpoints."""

import re
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

from api.models.proxy import Proxy, ProxyCreate, ProxyProtocol, ProxyResponse, ProxyUpdate
from api.models.credential import CredentialType

router = APIRouter(prefix="/projects/{project_id}/proxies")

# Regex pattern to parse proxy URLs
# Supports: protocol://[username:password@]host:port
PROXY_URL_PATTERN = re.compile(
    r'^(?P<protocol>https?|socks[45])://'
    r'(?:(?P<username>[^:@]+):(?P<password>[^@]+)@)?'
    r'(?P<host>[^:]+):(?P<port>\d+)$',
    re.IGNORECASE
)


class ProxyListResponse(BaseModel):
    """Response for listing proxies."""
    total: int
    healthy: int
    proxies: list[ProxyResponse]


class StrategyRequest(BaseModel):
    """Request to change routing strategy."""
    strategy: str


class ProxyUploadError(BaseModel):
    """Error details for a failed proxy upload line."""
    line_number: int
    line: str
    error: str


class ProxyUploadResponse(BaseModel):
    """Response for proxy upload endpoint."""
    total_lines: int
    successful: int
    failed: int
    proxies: list[ProxyResponse]
    errors: list[ProxyUploadError]


def _parse_proxy_url(url: str) -> dict | None:
    """Parse a proxy URL and return its components."""
    url = url.strip()
    if not url:
        return None

    match = PROXY_URL_PATTERN.match(url)
    if not match:
        return None

    return {
        'protocol': match.group('protocol').lower(),
        'username': match.group('username'),
        'password': match.group('password'),
        'host': match.group('host'),
        'port': int(match.group('port')),
    }


def _proxy_to_response(
    proxy: Proxy,
    connector_name: str | None = None,
    connector_enabled: bool = True,
) -> ProxyResponse:
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
        connector_enabled=connector_enabled,
        status=proxy.status.value if hasattr(proxy.status, 'value') else proxy.status,
        request_count=proxy.request_count,
        success_count=proxy.success_count,
        failure_count=proxy.failure_count,
        success_rate=proxy.success_rate,
        avg_latency_ms=proxy.avg_latency_ms,
        bytes_sent=proxy.bytes_sent,
        bytes_received=proxy.bytes_received,
        tags=proxy.tags,
        created_at=proxy.created_at,
    )


@router.get("", response_model=ProxyListResponse)
async def list_proxies(request: Request, project_id: str) -> ProxyListResponse:
    """List all proxies for a project (including those from disabled connectors)."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get ALL proxies (including from disabled connectors) for display
    all_proxies = proxy_manager.get_all_proxies_for_project(project_id)
    # Get healthy proxies (only from enabled connectors) for the count
    healthy_proxies = proxy_manager.get_healthy_proxies_for_project(project_id)

    # Build responses with connector names and enabled status
    responses = []
    for p in all_proxies:
        connector = proxy_manager.get_connector(p.connector_id)
        connector_name = connector.name if connector else None
        connector_enabled = connector.enabled if connector else True
        responses.append(_proxy_to_response(p, connector_name, connector_enabled))

    return ProxyListResponse(
        total=len(all_proxies),
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

    return _proxy_to_response(proxy, connector.name, connector.enabled)


@router.post("/upload", response_model=ProxyUploadResponse, status_code=201)
async def upload_proxies(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    connector_id: str = Form(...),
) -> ProxyUploadResponse:
    """
    Upload proxies from a CSV file.

    The file should contain one proxy URL per line in the format:
    protocol://[username:password@]host:port

    Supported protocols: http, https, socks4, socks5

    Examples:
    - http://192.168.1.1:8080
    - socks5://user:pass@10.0.0.1:1080
    """
    proxy_manager = request.app.state.proxy_manager

    # Validate project exists
    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate that the connector exists
    connector = proxy_manager.get_connector(connector_id)
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
            detail="Proxies can only be uploaded to STATIC_PROXY_PROVIDER connectors"
        )

    # Read and parse the file
    content = await file.read()
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")

    lines = text.strip().split('\n')

    successful_proxies: list[ProxyResponse] = []
    errors: list[ProxyUploadError] = []

    for line_number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        parsed = _parse_proxy_url(line)
        if parsed is None:
            errors.append(ProxyUploadError(
                line_number=line_number,
                line=line,
                error="Invalid proxy URL format. Expected: protocol://[username:password@]host:port"
            ))
            continue

        try:
            protocol = ProxyProtocol(parsed['protocol'])
        except ValueError:
            errors.append(ProxyUploadError(
                line_number=line_number,
                line=line,
                error=f"Unsupported protocol: {parsed['protocol']}"
            ))
            continue

        proxy = Proxy(
            host=parsed['host'],
            port=parsed['port'],
            protocol=protocol,
            username=parsed['username'],
            password=parsed['password'],
            connector_id=connector_id,
        )

        await proxy_manager.add_proxy(proxy)
        connector.proxy_count += 1
        successful_proxies.append(_proxy_to_response(proxy, connector.name, connector.enabled))

    return ProxyUploadResponse(
        total_lines=len([l for l in lines if l.strip()]),
        successful=len(successful_proxies),
        failed=len(errors),
        proxies=successful_proxies,
        errors=errors,
    )


@router.get("/{proxy_id}", response_model=ProxyResponse)
async def get_proxy(request: Request, proxy_id: str) -> ProxyResponse:
    """Get a specific proxy by ID."""
    proxy_manager = request.app.state.proxy_manager
    proxy = proxy_manager.get_proxy(proxy_id)

    if proxy is None:
        raise HTTPException(status_code=404, detail="Proxy not found")

    connector = proxy_manager.get_connector(proxy.connector_id)
    connector_name = connector.name if connector else None
    connector_enabled = connector.enabled if connector else True
    return _proxy_to_response(proxy, connector_name, connector_enabled)


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
    connector_enabled = connector.enabled if connector else True
    return _proxy_to_response(proxy, connector_name, connector_enabled)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(request: Request, proxy_id: str) -> None:
    """Remove a proxy from the pool.

    If the proxy belongs to a cloud provider connector (AWS, GCP, Azure),
    the proxy will be marked as TERMINATING and the auto-scaler will handle
    the actual cloud instance termination.

    For non-cloud proxies, the proxy is removed immediately.
    """
    proxy_manager = request.app.state.proxy_manager

    if not await proxy_manager.delete_proxy_async(proxy_id):
        raise HTTPException(status_code=404, detail="Proxy not found")


@router.post("/strategy")
async def set_strategy(request: Request, strategy_req: StrategyRequest) -> dict[str, str]:
    """Change the routing strategy."""
    proxy_manager = request.app.state.proxy_manager

    try:
        proxy_manager.set_strategy(strategy_req.strategy)
        return {"status": "ok", "strategy": strategy_req.strategy}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
