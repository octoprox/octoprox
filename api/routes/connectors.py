# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector endpoints, thin over :class:`~api.services.connectors.ConnectorService` for writes and
:class:`~api.services.read_model.RuntimeView` for answers; another project's connector is not found.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from api.core import utc_now
from api.core.auth import RequireEditorDep
from api.db.repository import ConnectorRepository, MetricsRepository
from api.models.connector import (
    ConnectorCreate,
    ConnectorOptionsResponse,
    ConnectorResponse,
    ConnectorUpdate,
)
from api.routes.common import (
    connector_service_of,
    require_project,
    runtime_view_of,
    session_factory_of,
)
from api.routes.metrics import RANGE_CONFIG, MetricsHistoryResponse, MetricsSnapshot
from api.services.read_model import ConnectorRow

router = APIRouter(prefix="/projects/{project_id}/connectors")


class ConnectorListResponse(BaseModel):
    """Response for listing connectors."""
    total: int
    connectors: list[ConnectorResponse]


def _connector_to_response(view: ConnectorRow) -> ConnectorResponse:
    """The full API view of a connector: its definition, credential, pool, target and traffic."""
    connector = view.connector
    return ConnectorResponse(
        id=connector.id,
        name=connector.name,
        credential_id=connector.credential_id,
        credential_name=view.credential_name,
        credential_type=view.credential_type,
        project_id=connector.project_id,
        config=connector.config,
        routing_config=connector.routing_config,
        rate_limit_config=connector.rate_limit_config,
        traffic_config=connector.traffic_config,
        traffic_reset_at=connector.traffic_reset_at,
        traffic_usage=view.traffic_usage,
        enabled=connector.enabled,
        proxy_count=view.proxy_count,
        target=view.target,
        last_error=connector.last_error,
        last_error_at=connector.last_error_at,
        consecutive_errors=connector.consecutive_errors,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


async def _describe(request: Request, project_id: str, connector_id: str) -> ConnectorResponse:
    """A connector of the project as the API shows it; 404 when the project has no such connector."""
    view = await runtime_view_of(request).connector(project_id, connector_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return _connector_to_response(view)


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(request: Request, project_id: str) -> ConnectorListResponse:
    """List all connectors for a project, oldest first."""
    await require_project(request, project_id)
    views = await runtime_view_of(request).connectors(project_id)
    return ConnectorListResponse(total=len(views), connectors=[_connector_to_response(view) for view in views])


@router.post("", response_model=ConnectorResponse, status_code=201)
async def create_connector(
    request: Request,
    connector_data: ConnectorCreate,
    project_id: str,
    _guard: RequireEditorDep,
) -> ConnectorResponse:
    """Create a new connector on a credential of the project.

    A provider that syncs is asked for its first discovery, and a cloud
    connector for its first scale; the response does not wait for either.
    """
    connector = await connector_service_of(request).create(project_id, connector_data)
    return await _describe(request, project_id, connector.id)


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector(request: Request, project_id: str, connector_id: str) -> ConnectorResponse:
    """Get a specific connector of the project by ID."""
    return await _describe(request, project_id, connector_id)


@router.get("/{connector_id}/metrics/history", response_model=MetricsHistoryResponse)
async def get_connector_metrics_history(
    request: Request,
    project_id: str,
    connector_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> MetricsHistoryResponse:
    """Historical metrics snapshots for one connector, from its own history.

    Unlike per-proxy history these survive the connector's proxies being
    rotated or re-synced, so a period's traffic can be charted in full.
    """
    delta, limit, bucket_seconds = RANGE_CONFIG[range]
    since = utc_now() - delta
    async with session_factory_of(request)() as session:
        if await ConnectorRepository(session).get_in_project(project_id, connector_id) is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        repo = MetricsRepository(session)
        if bucket_seconds:
            rows = await repo.get_connector_metrics_history_aggregated(
                connector_id=connector_id, since=since, bucket_seconds=bucket_seconds
            )
        else:
            rows = await repo.get_connector_metrics_history(
                connector_id=connector_id, since=since, limit=limit, granularity=60
            )
    return MetricsHistoryResponse(snapshots=[MetricsSnapshot(**row) for row in reversed(rows)])


@router.post("/{connector_id}/traffic/reset", response_model=ConnectorResponse)
async def reset_connector_traffic(
    request: Request, project_id: str, connector_id: str, _guard: RequireEditorDep
) -> ConnectorResponse:
    """Start the connector's traffic usage over from now, for a vendor top-up or a plan change; history is untouched.

    The answer counts usage from now, but shows the block gateways still enforce, marked pending, until a worker stores
    the traffic decision the reset asks for.
    """
    await connector_service_of(request).reset_traffic(project_id, connector_id)
    return await _describe(request, project_id, connector_id)


@router.patch("/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    request: Request,
    project_id: str,
    connector_id: str,
    connector_data: ConnectorUpdate,
    _guard: RequireEditorDep,
) -> ConnectorResponse:
    """Update a connector; a refused edit leaves the stored connector as it was."""
    await connector_service_of(request).update(project_id, connector_id, connector_data)
    return await _describe(request, project_id, connector_id)


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(
    request: Request, project_id: str, connector_id: str, _guard: RequireEditorDep
) -> None:
    """Delete a connector and all its proxies, at once unless it is a cloud connector (AWS, GCP, Azure).

    A cloud connector is disabled and marked for deletion with its proxies TERMINATING, and the scale job terminates
    the instances, then deletes it.
    """
    await connector_service_of(request).remove(project_id, connector_id)


# Separate router for non-project-specific endpoints
options_router = APIRouter(prefix="/connector-options")


@options_router.get("", response_model=ConnectorOptionsResponse)
async def get_connector_options() -> ConnectorOptionsResponse:
    """Get available options for connector configuration (regions, instance types, etc.)."""
    return ConnectorOptionsResponse()
