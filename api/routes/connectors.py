# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector endpoints, thin over :class:`~api.services.connectors.ConnectorService` for writes,
:class:`~api.services.read_model.RuntimeView` for answers and :class:`~api.services.metrics.MetricsService` for the
history; another project's connector is not found.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from api.core.auth import RequireEditorDep
from api.models.connector import (
    ConnectorCreate,
    ConnectorListResponse,
    ConnectorOptionsResponse,
    ConnectorResponse,
    ConnectorUpdate,
)
from api.models.metrics import MetricsHistoryResponse
from api.routes.common import (
    actor_of,
    connector_service_of,
    metrics_service_of,
    require_project,
    runtime_view_of,
    settings_of,
)

router = APIRouter(prefix="/projects/{project_id}/connectors")


async def _describe(request: Request, project_id: str, connector_id: str) -> ConnectorResponse:
    """A connector of the project as the API shows it; 404 when the project has no such connector."""
    view = await runtime_view_of(request).connector(project_id, connector_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return ConnectorResponse.of(view, settings_of(request).health_check_interval)


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(request: Request, project_id: str) -> ConnectorListResponse:
    """List all connectors for a project, oldest first."""
    await require_project(request, project_id)
    views = await runtime_view_of(request).connectors(project_id)
    return ConnectorListResponse.of(views, settings_of(request).health_check_interval)


@router.post("", response_model=ConnectorResponse, status_code=201)
async def create_connector(
    request: Request,
    connector_data: ConnectorCreate,
    project_id: str,
    user: RequireEditorDep,
) -> ConnectorResponse:
    """Create a new connector on a credential of the project.

    A provider that syncs is asked for its first discovery, and a cloud
    connector for its first scale; the response does not wait for either.
    """
    connector = await connector_service_of(request).create(project_id, connector_data, actor_of(request, user))
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
    return await metrics_service_of(request).connector_history(project_id, connector_id, range)


@router.post("/{connector_id}/traffic/reset", response_model=ConnectorResponse)
async def reset_connector_traffic(
    request: Request, project_id: str, connector_id: str, user: RequireEditorDep
) -> ConnectorResponse:
    """Start the connector's traffic usage over from now, for a vendor top-up or a plan change; history is untouched.

    The answer counts usage from now, but shows the block gateways still enforce, marked pending, until a worker stores
    the traffic decision the reset asks for.
    """
    await connector_service_of(request).reset_traffic(project_id, connector_id, actor_of(request, user))
    return await _describe(request, project_id, connector_id)


@router.patch("/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    request: Request,
    project_id: str,
    connector_id: str,
    connector_data: ConnectorUpdate,
    user: RequireEditorDep,
) -> ConnectorResponse:
    """Update a connector; a refused edit leaves the stored connector as it was."""
    await connector_service_of(request).update(project_id, connector_id, connector_data, actor_of(request, user))
    return await _describe(request, project_id, connector_id)


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(
    request: Request, project_id: str, connector_id: str, user: RequireEditorDep
) -> None:
    """Delete a connector and all its proxies, at once unless it is a cloud connector (AWS, GCP, Azure).

    A cloud connector is disabled and marked for deletion with its proxies TERMINATING, and the scale job terminates
    the instances, then deletes it.
    """
    await connector_service_of(request).remove(project_id, connector_id, actor_of(request, user))


# Separate router for non-project-specific endpoints
options_router = APIRouter(prefix="/connector-options")


@options_router.get("", response_model=ConnectorOptionsResponse)
async def get_connector_options() -> ConnectorOptionsResponse:
    """Get available options for connector configuration (regions, instance types, etc.)."""
    return ConnectorOptionsResponse()
