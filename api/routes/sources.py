"""Proxy source management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.source import ProxySource, SourceCreate, SourceResponse, SourceUpdate

router = APIRouter()


class SourceListResponse(BaseModel):
    """Response for listing sources."""
    total: int
    sources: list[SourceResponse]


def _source_to_response(source: ProxySource) -> SourceResponse:
    """Convert a ProxySource to SourceResponse."""
    return SourceResponse(
        id=source.id,
        name=source.name,
        type=source.type.value if hasattr(source.type, 'value') else source.type,
        enabled=source.enabled,
        proxy_count=source.proxy_count,
        last_refresh=source.last_refresh,
        refresh_interval_seconds=source.refresh_interval_seconds,
        created_at=source.created_at,
    )


@router.get("", response_model=SourceListResponse)
async def list_sources(request: Request) -> SourceListResponse:
    """List all proxy sources."""
    proxy_manager = request.app.state.proxy_manager
    sources = proxy_manager.sources
    
    return SourceListResponse(
        total=len(sources),
        sources=[_source_to_response(s) for s in sources],
    )


@router.post("", response_model=SourceResponse, status_code=201)
async def create_source(request: Request, source_data: SourceCreate) -> SourceResponse:
    """Add a new proxy source."""
    proxy_manager = request.app.state.proxy_manager
    
    source = ProxySource(
        name=source_data.name,
        type=source_data.type,
        enabled=source_data.enabled,
        config=source_data.config,
        refresh_interval_seconds=source_data.refresh_interval_seconds,
    )
    
    proxy_manager.add_source(source)
    return _source_to_response(source)


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(request: Request, source_id: str) -> SourceResponse:
    """Get a specific source by ID."""
    proxy_manager = request.app.state.proxy_manager
    source = proxy_manager.get_source(source_id)
    
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    
    return _source_to_response(source)


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(
    request: Request, source_id: str, source_data: SourceUpdate
) -> SourceResponse:
    """Update a source."""
    proxy_manager = request.app.state.proxy_manager
    source = proxy_manager.get_source(source_id)
    
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    
    # Update fields
    if source_data.name is not None:
        source.name = source_data.name
    if source_data.enabled is not None:
        source.enabled = source_data.enabled
    if source_data.config is not None:
        source.config = source_data.config
    if source_data.refresh_interval_seconds is not None:
        source.refresh_interval_seconds = source_data.refresh_interval_seconds
    
    return _source_to_response(source)


@router.delete("/{source_id}", status_code=204)
async def delete_source(request: Request, source_id: str) -> None:
    """Remove a proxy source."""
    proxy_manager = request.app.state.proxy_manager
    
    if not proxy_manager.remove_source(source_id):
        raise HTTPException(status_code=404, detail="Source not found")


@router.post("/{source_id}/refresh", response_model=SourceResponse)
async def refresh_source(request: Request, source_id: str) -> SourceResponse:
    """Manually trigger a refresh of a source."""
    proxy_manager = request.app.state.proxy_manager
    source = proxy_manager.get_source(source_id)
    
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    
    # TODO: Implement actual refresh logic based on source type
    # For now, just return the source
    return _source_to_response(source)

