# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector management endpoints."""

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

from api.core import utc_now
from api.core.auth import RequireEditorDep
from api.core.event_bus import event_bus
from api.core.signals import provider_connector_sync_requested
from api.db.repository import MetricsRepository
from api.models.connector import (
    Connector,
    ConnectorCreate,
    ConnectorOptionsResponse,
    ConnectorResponse,
    ConnectorUpdate,
    ProxyTarget,
    TrafficUsage,
    validate_rate_limit_config,
    validate_routing_config,
    validate_traffic_config,
)
from api.models.credential import Credential
from api.providers.registry import ProviderRegistry, UnknownProviderError, get_provider_registry
from api.providers.sdk.validation import ConfigValidationError
from api.routes.common import unique_name_violation
from api.routes.metrics import RANGE_CONFIG, MetricsHistoryResponse, MetricsSnapshot

router = APIRouter(prefix="/projects/{project_id}/connectors")


class ConnectorListResponse(BaseModel):
    """Response for listing connectors."""
    total: int
    connectors: list[ConnectorResponse]


def _validate_sub_config(
    validate: Callable[[dict[str, Any]], dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    """Run one of the connector's JSON config validators, mapping errors to 422."""
    try:
        return validate(config)
    except (ValidationError, ValueError) as e:
        detail = str(e)
        if hasattr(e, 'errors'):
            messages = [err.get('msg', str(err)) for err in e.errors()]
            detail = "; ".join(messages)
        raise HTTPException(status_code=422, detail=detail) from None


def _traffic_usage(proxy_manager: Any, connector: Connector) -> TrafficUsage | None:
    """The connector's traffic usage as this instance sees it."""
    try:
        usage: TrafficUsage = proxy_manager.traffic_usage(connector)
    except Exception:  # a mocked manager in tests, or a connector mid-removal
        return None
    return usage


def _validate_connector_config(
    registry: ProviderRegistry, credential: Credential, config: dict[str, Any]
) -> dict[str, Any]:
    """Validate a connector config for the credential's provider type."""
    try:
        return registry.validate_connector_config(credential.type, config, credential.config)
    except UnknownProviderError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except ConfigValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except ValidationError as e:
        # Extract just the error messages from Pydantic validation errors
        messages = [err.get('msg', str(err)) for err in e.errors()]
        raise HTTPException(status_code=422, detail="; ".join(messages)) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def _connector_to_response(
    connector: Connector,
    credential_name: str | None = None,
    credential_type: str | None = None,
    proxy_count: int = 0,
    target: ProxyTarget | None = None,
    traffic_usage: TrafficUsage | None = None,
) -> ConnectorResponse:
    """Convert a Connector to ConnectorResponse."""
    return ConnectorResponse(
        id=connector.id,
        name=connector.name,
        credential_id=connector.credential_id,
        credential_name=credential_name,
        credential_type=credential_type,
        project_id=connector.project_id,
        config=connector.config,
        routing_config=connector.routing_config,
        rate_limit_config=connector.rate_limit_config,
        traffic_config=connector.traffic_config,
        traffic_reset_at=connector.traffic_reset_at,
        traffic_usage=traffic_usage,
        enabled=connector.enabled,
        proxy_count=proxy_count,
        target=target,
        last_error=connector.last_error,
        last_error_at=connector.last_error_at,
        consecutive_errors=connector.consecutive_errors,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(request: Request, project_id: str) -> ConnectorListResponse:
    """List all connectors for a project."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    connectors = proxy_manager.get_connectors_for_project(project_id)
    responses = []
    for connector in connectors:
        responses.append(_describe(proxy_manager, connector))

    return ConnectorListResponse(
        total=len(connectors),
        connectors=responses,
    )


def _describe(proxy_manager: Any, connector: Connector) -> ConnectorResponse:
    """The full API view of a connector: credential, pool, target and traffic."""
    credential = proxy_manager.get_credential(connector.credential_id)
    credential_name = credential.name if credential else None
    credential_type = credential.type if credential else None
    proxy_count = len(proxy_manager.get_proxies_for_connector(connector.id))
    return _connector_to_response(
        connector, credential_name, credential_type, proxy_count,
        proxy_manager.get_connector_target(connector), _traffic_usage(proxy_manager, connector),
    )


@router.post("", response_model=ConnectorResponse, status_code=201)
async def create_connector(
    request: Request,
    connector_data: ConnectorCreate,
    project_id: str,
    _guard: RequireEditorDep,
) -> ConnectorResponse:
    """Create a new connector."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate that the credential exists and belongs to this project
    credential = proxy_manager.get_credential(connector_data.credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if credential.project_id != project_id:
        raise HTTPException(status_code=400, detail="Credential does not belong to this project")

    # Validate connector config based on credential type
    registry = get_provider_registry()
    validated_config = _validate_connector_config(registry, credential, connector_data.config)

    # Validate routing config if provided
    validated_routing_config: dict[str, Any] = {}
    if connector_data.routing_config:
        validated_routing_config = _validate_sub_config(validate_routing_config, connector_data.routing_config)

    # Validate rate limit config if provided
    validated_rate_limit_config: dict[str, Any] = {}
    if connector_data.rate_limit_config:
        validated_rate_limit_config = _validate_sub_config(validate_rate_limit_config, connector_data.rate_limit_config)

    # Validate traffic config if provided
    validated_traffic_config: dict[str, Any] = {}
    if connector_data.traffic_config:
        validated_traffic_config = _validate_sub_config(validate_traffic_config, connector_data.traffic_config)

    connector = Connector(
        name=connector_data.name,
        credential_id=connector_data.credential_id,
        credential_type=credential.type,
        project_id=project_id,
        config=validated_config,
        routing_config=validated_routing_config,
        rate_limit_config=validated_rate_limit_config,
        traffic_config=validated_traffic_config,
        enabled=connector_data.enabled,
    )

    try:
        await proxy_manager.add_connector(connector)
    except IntegrityError as exc:
        raise unique_name_violation(exc, "connector", connector.name) from None

    # Trigger provider sync if this is a syncable provider connector (fire-and-forget)
    # Use create_task to avoid blocking the API response during IP discovery
    if registry.is_syncable(credential.type):
        asyncio.create_task(
            event_bus.publish(provider_connector_sync_requested, None, connector=connector)
        )

    # New connector has 0 proxies initially (provider syncer will add them)
    return _connector_to_response(
        connector, credential.name, credential.type, proxy_count=0,
        target=proxy_manager.get_connector_target(connector),
        traffic_usage=_traffic_usage(proxy_manager, connector),
    )


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector(request: Request, connector_id: str) -> ConnectorResponse:
    """Get a specific connector by ID."""
    proxy_manager = request.app.state.proxy_manager
    connector = proxy_manager.get_connector(connector_id)

    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    return _describe(proxy_manager, connector)


@router.get("/{connector_id}/metrics/history", response_model=MetricsHistoryResponse)
async def get_connector_metrics_history(
    request: Request,
    connector_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> MetricsHistoryResponse:
    """Historical metrics snapshots for one connector, from its own history.

    Unlike per-proxy history these survive the connector's proxies being
    rotated or re-synced, so a period's traffic can be charted in full.
    """
    proxy_manager = request.app.state.proxy_manager
    connector = proxy_manager.get_connector(connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    delta, limit, bucket_seconds = RANGE_CONFIG[range]
    since = utc_now() - delta
    async with proxy_manager._session_factory() as session:
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
    request: Request, connector_id: str, _guard: RequireEditorDep
) -> ConnectorResponse:
    """Start the connector's traffic usage over from now.

    For a vendor top-up or a plan change mid-period. History is untouched:
    the current period simply counts from this moment, and a block raised
    by the limit is lifted.
    """
    proxy_manager = request.app.state.proxy_manager
    connector = await proxy_manager.reset_connector_traffic(connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return _describe(proxy_manager, connector)


@router.patch("/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    request: Request, connector_id: str, connector_data: ConnectorUpdate, _guard: RequireEditorDep
) -> ConnectorResponse:
    """Update a connector."""
    proxy_manager = request.app.state.proxy_manager
    connector = proxy_manager.get_connector(connector_id)

    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    # Get current credential for validation
    current_credential = proxy_manager.get_credential(connector.credential_id)

    # Update fields. ``connector`` is the cached object, so a rejected write
    # must put the old name back.
    previous_name = connector.name
    if connector_data.name is not None:
        connector.name = connector_data.name
    if connector_data.credential_id is not None:
        # Validate new credential
        credential = proxy_manager.get_credential(connector_data.credential_id)
        if credential is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        if credential.project_id != connector.project_id:
            raise HTTPException(status_code=400, detail="Credential does not belong to this project")
        connector.credential_id = connector_data.credential_id
        connector.credential_type = credential.type
        current_credential = credential
    if connector_data.config is not None:
        # Validate connector config based on credential type
        if current_credential:
            connector.config = _validate_connector_config(
                get_provider_registry(), current_credential, connector_data.config
            )
        else:
            connector.config = connector_data.config
    # Validate each JSON config before touching the cached object, so a
    # rejected write leaves the connector as it was.
    new_routing = new_rate_limit = new_traffic = None
    if connector_data.routing_config is not None:
        new_routing = _validate_sub_config(validate_routing_config, connector_data.routing_config)
    if connector_data.rate_limit_config is not None:
        new_rate_limit = _validate_sub_config(validate_rate_limit_config, connector_data.rate_limit_config)
    if connector_data.traffic_config is not None:
        new_traffic = _validate_sub_config(validate_traffic_config, connector_data.traffic_config)
    if new_routing is not None:
        connector.routing_config = new_routing
    if new_rate_limit is not None:
        connector.rate_limit_config = new_rate_limit
    if new_traffic is not None:
        connector.traffic_config = new_traffic
    if connector_data.enabled is not None:
        connector.enabled = connector_data.enabled

    try:
        await proxy_manager.update_connector(connector)
    except IntegrityError as exc:
        connector.name = previous_name
        raise unique_name_violation(exc, "connector", connector_data.name or "") from None

    credential = proxy_manager.get_credential(connector.credential_id)

    # Trigger provider sync if this is a syncable provider connector (fire-and-forget)
    # Use create_task to avoid blocking the API response during IP discovery
    if credential and get_provider_registry().is_syncable(credential.type):
        asyncio.create_task(
            event_bus.publish(provider_connector_sync_requested, None, connector=connector)
        )

    return _describe(proxy_manager, connector)


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(request: Request, connector_id: str, _guard: RequireEditorDep) -> None:
    """Delete a connector and all its proxies.

    For cloud connectors (AWS, GCP, Azure), all proxies will be marked as
    TERMINATING and the auto-scaler will handle the actual cloud instance
    termination. The connector is deleted after all proxies are terminated.

    For non-cloud connectors, the connector and proxies are deleted immediately.
    """
    proxy_manager = request.app.state.proxy_manager

    if not await proxy_manager.delete_connector_async(connector_id):
        raise HTTPException(status_code=404, detail="Connector not found")


# Separate router for non-project-specific endpoints
options_router = APIRouter(prefix="/connector-options")


@options_router.get("", response_model=ConnectorOptionsResponse)
async def get_connector_options() -> ConnectorOptionsResponse:
    """Get available options for connector configuration (regions, instance types, etc.)."""
    return ConnectorOptionsResponse()
