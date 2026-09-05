# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector management endpoints."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from api.core.auth import RequireEditorDep
from api.core.event_bus import event_bus
from api.core.signals import provider_connector_sync_requested
from api.models.connector import (
    Connector,
    ConnectorCreate,
    ConnectorOptionsResponse,
    ConnectorResponse,
    ConnectorUpdate,
    validate_rate_limit_config,
    validate_routing_config,
)
from api.models.credential import Credential
from api.providers.registry import ProviderRegistry, UnknownProviderError, get_provider_registry
from api.providers.sdk.validation import ConfigValidationError

router = APIRouter(prefix="/projects/{project_id}/connectors")


class ConnectorListResponse(BaseModel):
    """Response for listing connectors."""
    total: int
    connectors: list[ConnectorResponse]


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
        enabled=connector.enabled,
        proxy_count=proxy_count,
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
        credential = proxy_manager.get_credential(connector.credential_id)
        credential_name = credential.name if credential else None
        credential_type = credential.type if credential else None
        proxy_count = len(proxy_manager.get_proxies_for_connector(connector.id))
        responses.append(_connector_to_response(connector, credential_name, credential_type, proxy_count))

    return ConnectorListResponse(
        total=len(connectors),
        connectors=responses,
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
        try:
            validated_routing_config = validate_routing_config(connector_data.routing_config)
        except (ValidationError, ValueError) as e:
            detail = str(e)
            if hasattr(e, 'errors'):
                messages = [err.get('msg', str(err)) for err in e.errors()]
                detail = "; ".join(messages)
            raise HTTPException(status_code=422, detail=detail) from None

    # Validate rate limit config if provided
    validated_rate_limit_config: dict[str, Any] = {}
    if connector_data.rate_limit_config:
        try:
            validated_rate_limit_config = validate_rate_limit_config(connector_data.rate_limit_config)
        except (ValidationError, ValueError) as e:
            detail = str(e)
            if hasattr(e, 'errors'):
                messages = [err.get('msg', str(err)) for err in e.errors()]
                detail = "; ".join(messages)
            raise HTTPException(status_code=422, detail=detail) from None

    connector = Connector(
        name=connector_data.name,
        credential_id=connector_data.credential_id,
        credential_type=credential.type,
        project_id=project_id,
        config=validated_config,
        routing_config=validated_routing_config,
        rate_limit_config=validated_rate_limit_config,
        enabled=connector_data.enabled,
    )

    await proxy_manager.add_connector(connector)

    # Trigger provider sync if this is a syncable provider connector (fire-and-forget)
    # Use create_task to avoid blocking the API response during IP discovery
    if registry.is_syncable(credential.type):
        asyncio.create_task(
            event_bus.publish(provider_connector_sync_requested, None, connector=connector)
        )

    # New connector has 0 proxies initially (provider syncer will add them)
    return _connector_to_response(connector, credential.name, credential.type, proxy_count=0)


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector(request: Request, connector_id: str) -> ConnectorResponse:
    """Get a specific connector by ID."""
    proxy_manager = request.app.state.proxy_manager
    connector = proxy_manager.get_connector(connector_id)

    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    credential = proxy_manager.get_credential(connector.credential_id)
    credential_name = credential.name if credential else None
    credential_type = credential.type if credential else None
    proxy_count = len(proxy_manager.get_proxies_for_connector(connector.id))
    return _connector_to_response(connector, credential_name, credential_type, proxy_count)


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

    # Update fields
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
    if connector_data.routing_config is not None:
        try:
            connector.routing_config = validate_routing_config(connector_data.routing_config)
        except (ValidationError, ValueError) as e:
            detail = str(e)
            if hasattr(e, 'errors'):
                messages = [err.get('msg', str(err)) for err in e.errors()]
                detail = "; ".join(messages)
            raise HTTPException(status_code=422, detail=detail) from None
    if connector_data.rate_limit_config is not None:
        try:
            connector.rate_limit_config = validate_rate_limit_config(connector_data.rate_limit_config)
        except (ValidationError, ValueError) as e:
            detail = str(e)
            if hasattr(e, 'errors'):
                messages = [err.get('msg', str(err)) for err in e.errors()]
                detail = "; ".join(messages)
            raise HTTPException(status_code=422, detail=detail) from None
    if connector_data.enabled is not None:
        connector.enabled = connector_data.enabled

    await proxy_manager.update_connector(connector)

    credential = proxy_manager.get_credential(connector.credential_id)

    # Trigger provider sync if this is a syncable provider connector (fire-and-forget)
    # Use create_task to avoid blocking the API response during IP discovery
    if credential and get_provider_registry().is_syncable(credential.type):
        asyncio.create_task(
            event_bus.publish(provider_connector_sync_requested, None, connector=connector)
        )

    credential_name = credential.name if credential else None
    credential_type = credential.type if credential else None
    proxy_count = len(proxy_manager.get_proxies_for_connector(connector.id))
    return _connector_to_response(connector, credential_name, credential_type, proxy_count)


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
