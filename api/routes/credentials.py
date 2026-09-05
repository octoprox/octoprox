# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential management endpoints.

Config validation and vendor-side credential checks are delegated to the
provider registry, so this module is the same for code providers and for
descriptor providers, including ones added through the admin UI.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from api.core.auth import RequireEditorDep
from api.models.credential import (
    Credential,
    CredentialCreate,
    CredentialDetailResponse,
    CredentialResponse,
    CredentialUpdate,
)
from api.providers.registry import (
    ProviderRegistry,
    ProviderType,
    UnknownProviderError,
    get_provider_registry,
)
from api.providers.sdk.discovery import CredentialValidator
from api.providers.sdk.validation import ConfigValidationError

router = APIRouter(prefix="/projects/{project_id}/credentials")


class CredentialListResponse(BaseModel):
    """Response for listing credentials."""
    total: int
    credentials: list[CredentialResponse]


def _credential_to_response(credential: Credential) -> CredentialResponse:
    """Convert a Credential to CredentialResponse."""
    config = credential.config or {}
    return CredentialResponse(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        project_id=credential.project_id,
        has_username=bool(config.get("username")),
        has_password=bool(config.get("password")),
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def _credential_to_detail_response(credential: Credential) -> CredentialDetailResponse:
    """Convert a Credential to CredentialDetailResponse (includes config)."""
    return CredentialDetailResponse(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        project_id=credential.project_id,
        config=credential.config,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def _provider_type(registry: ProviderRegistry, type_id: str) -> ProviderType:
    try:
        return registry.require(type_id)
    except UnknownProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _validate_config(ptype: ProviderType, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return ptype.credential_validator(config)
    except ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ValidationError as exc:
        messages = [err.get("msg", str(err)) for err in exc.errors()]
        raise HTTPException(status_code=422, detail="; ".join(messages)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _verify_with_vendor(
    registry: ProviderRegistry, ptype: ProviderType, config: dict[str, Any]
) -> dict[str, Any]:
    """Run the descriptor's validation call (if any); returns config with captured values."""
    if ptype.descriptor is None or ptype.descriptor.validation is None:
        return config
    validator = CredentialValidator(ptype.descriptor, registry.runtime)
    outcome = await validator.validate(config)
    if not outcome.ok:
        raise HTTPException(status_code=400, detail=outcome.message)
    merged: dict[str, Any] = outcome.result or config
    return merged


@router.get("", response_model=CredentialListResponse)
async def list_credentials(request: Request, project_id: str) -> CredentialListResponse:
    """List all credentials for a project."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    credentials = proxy_manager.get_credentials_for_project(project_id)
    return CredentialListResponse(
        total=len(credentials),
        credentials=[_credential_to_response(c) for c in credentials],
    )


@router.post("", response_model=CredentialDetailResponse, status_code=201)
async def create_credential(
    request: Request,
    credential_data: CredentialCreate,
    project_id: str,
    _guard: RequireEditorDep,
) -> CredentialDetailResponse:
    """Create a new credential."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    registry = get_provider_registry()
    ptype = _provider_type(registry, credential_data.type)
    config = _validate_config(ptype, credential_data.config)
    config = await _verify_with_vendor(registry, ptype, config)

    credential = Credential(
        name=credential_data.name,
        type=ptype.id,
        project_id=project_id,
        config=config,
    )

    await proxy_manager.add_credential(credential)
    return _credential_to_detail_response(credential)


@router.get("/{credential_id}", response_model=CredentialDetailResponse)
async def get_credential(request: Request, credential_id: str) -> CredentialDetailResponse:
    """Get a specific credential by ID (includes config)."""
    proxy_manager = request.app.state.proxy_manager
    credential = proxy_manager.get_credential(credential_id)

    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")

    return _credential_to_detail_response(credential)


@router.patch("/{credential_id}", response_model=CredentialDetailResponse)
async def update_credential(
    request: Request, credential_id: str, credential_data: CredentialUpdate, _guard: RequireEditorDep
) -> CredentialDetailResponse:
    """Update a credential."""
    proxy_manager = request.app.state.proxy_manager
    credential = proxy_manager.get_credential(credential_id)

    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Update fields
    if credential_data.name is not None:
        credential.name = credential_data.name
    if credential_data.config is not None:
        registry = get_provider_registry()
        ptype = _provider_type(registry, credential.type)
        # Values captured by a previous vendor validation (e.g. a customer id)
        # are not user fields; carry them over unless the new config sets them.
        incoming = dict(credential_data.config)
        if ptype.descriptor is not None and ptype.descriptor.validation is not None:
            for key in ptype.descriptor.validation.capture:
                if key not in incoming and key in (credential.config or {}):
                    incoming[key] = credential.config[key]
        config = _validate_config(ptype, incoming)
        if _secret_material_changed(ptype, credential.config or {}, config):
            config = await _verify_with_vendor(registry, ptype, config)
        credential.config = config

    await proxy_manager.update_credential(credential)
    return _credential_to_detail_response(credential)


def _secret_material_changed(ptype: ProviderType, old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Only re-validate with the vendor when a credential field actually changed."""
    keys = {f.key for f in ptype.credential_fields}
    return any(old.get(key) != new.get(key) for key in keys)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(request: Request, credential_id: str, _guard: RequireEditorDep) -> None:
    """Delete a credential."""
    proxy_manager = request.app.state.proxy_manager

    credential = proxy_manager.get_credential(credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Check if any connectors are using this credential
    connectors = proxy_manager.get_connectors_for_credential(credential_id)
    if connectors:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete credential: {len(connectors)} connector(s) are using it"
        )

    await proxy_manager.remove_credential(credential_id)
