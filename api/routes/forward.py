"""Proxy forwarding endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

from api.core.forwarder import ForwardingError, ProxyForwarder

router = APIRouter()


class ForwardRequest(BaseModel):
    """Request to forward through a proxy."""
    method: str = "GET"
    url: HttpUrl
    headers: dict[str, str] | None = None
    body: str | None = None


class ForwardResponse(BaseModel):
    """Response from forwarded request."""
    status_code: int
    headers: dict[str, str]
    body: str
    proxy_id: str | None = None


@router.post("", response_model=ForwardResponse)
async def forward_request(
    request: Request,
    forward_req: ForwardRequest,
    session_id: str | None = None,
    retry: bool = True,
) -> ForwardResponse:
    """Forward a request through a managed proxy.
    
    Args:
        forward_req: The request to forward
        session_id: Optional session ID for sticky routing
        retry: Whether to retry on failure (default: True)
    """
    proxy_manager = request.app.state.proxy_manager
    forwarder = ProxyForwarder(proxy_manager)
    
    content = forward_req.body.encode() if forward_req.body else None
    
    try:
        if retry:
            response = await forwarder.forward_with_retry(
                method=forward_req.method,
                url=str(forward_req.url),
                headers=forward_req.headers,
                content=content,
                session_id=session_id,
            )
        else:
            response = await forwarder.forward(
                method=forward_req.method,
                url=str(forward_req.url),
                headers=forward_req.headers,
                content=content,
                session_id=session_id,
            )
        
        # Convert headers to dict
        response_headers = dict(response.headers)
        
        return ForwardResponse(
            status_code=response.status_code,
            headers=response_headers,
            body=response.text,
        )
        
    except ForwardingError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_passthrough(
    request: Request,
    path: str,
    target_host: str,
    session_id: str | None = None,
) -> Response:
    """Transparent proxy passthrough endpoint.
    
    This endpoint acts as a transparent proxy, forwarding requests to the target host
    through a managed proxy.
    
    Args:
        path: The path to request on the target host
        target_host: The target host (e.g., "https://api.example.com")
        session_id: Optional session ID for sticky routing
    """
    proxy_manager = request.app.state.proxy_manager
    forwarder = ProxyForwarder(proxy_manager)
    
    # Build target URL
    target_url = f"{target_host.rstrip('/')}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"
    
    # Get request body
    body = await request.body()
    
    # Filter and forward headers
    forward_headers = {}
    skip_headers = {"host", "content-length", "transfer-encoding"}
    for key, value in request.headers.items():
        if key.lower() not in skip_headers:
            forward_headers[key] = value
    
    try:
        response = await forwarder.forward_with_retry(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=body if body else None,
            session_id=session_id,
        )
        
        # Filter response headers
        response_headers = {}
        skip_response_headers = {"content-encoding", "transfer-encoding", "content-length"}
        for key, value in response.headers.items():
            if key.lower() not in skip_response_headers:
                response_headers[key] = value
        
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type"),
        )
        
    except ForwardingError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

