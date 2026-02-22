# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from api.models.credential import (
    Credential,
    CredentialCreate,
    CredentialDetailResponse,
    CredentialResponse,
    CredentialType,
    CredentialUpdate,
    validate_credential_config,
)

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
        type=credential.type.value if hasattr(credential.type, 'value') else credential.type,
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
        type=credential.type.value if hasattr(credential.type, 'value') else credential.type,
        project_id=credential.project_id,
        config=credential.config,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


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
) -> CredentialDetailResponse:
    """Create a new credential."""
    proxy_manager = request.app.state.proxy_manager

    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate BrightData token if applicable
    if credential_data.type == CredentialType.BRIGHTDATA:
        from api.models.credential import BrightDataCredentialConfig
        from api.routes.brightdata import validate_token

        # Get token from config dict (before full validation)
        token = credential_data.config.get("token")
        if not token or not token.strip():
            raise HTTPException(status_code=400, detail="BrightData token is required")

        # Validate token with API and get customer ID
        validation = await validate_token(token)

        if not validation.valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid BrightData token. Status: {validation.status or 'unknown'}"
            )

        # Store customer ID in config
        credential_data.config["customer_id"] = validation.customer_id

        # Now validate the complete config with the model
        try:
            validated = BrightDataCredentialConfig(**credential_data.config)
            credential_data.config = validated.model_dump(exclude_none=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid BrightData config: {str(e)}") from e

    credential = Credential(
        name=credential_data.name,
        type=credential_data.type,
        project_id=project_id,
        config=credential_data.config,
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
    request: Request, credential_id: str, credential_data: CredentialUpdate
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
        # Validate config based on credential type
        credential_type_enum = credential.type if isinstance(credential.type, CredentialType) else CredentialType(credential.type)

        # Handle BrightData specially - need to validate token with API if it changed
        if credential_type_enum == CredentialType.BRIGHTDATA:
            from api.models.credential import BrightDataCredentialConfig
            from api.routes.brightdata import validate_token

            new_token = credential_data.config.get("token")
            old_token = credential.config.get("token") if credential.config else None

            if new_token and new_token != old_token:
                # Token changed - need to re-validate with API
                if not new_token.strip():
                    raise HTTPException(status_code=400, detail="BrightData token is required")

                validation = await validate_token(new_token)
                if not validation.valid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid BrightData token. Status: {validation.status or 'unknown'}"
                    )
                credential_data.config["customer_id"] = validation.customer_id
            else:
                # Token not changed - preserve existing customer_id
                if credential.config and "customer_id" in credential.config:
                    credential_data.config["customer_id"] = credential.config["customer_id"]

            # Now validate the complete config
            try:
                validated = BrightDataCredentialConfig(**credential_data.config)
                credential.config = validated.model_dump(exclude_none=True)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid BrightData config: {str(e)}") from e
        else:
            try:
                validated_config = validate_credential_config(credential_type_enum, credential_data.config)
                credential.config = validated_config
            except ValidationError as e:
                # Extract just the error messages from Pydantic validation errors
                messages = [err.get('msg', str(err)) for err in e.errors()]
                raise HTTPException(status_code=422, detail="; ".join(messages)) from None
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None

    await proxy_manager.update_credential(credential)
    return _credential_to_detail_response(credential)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(request: Request, credential_id: str) -> None:
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

