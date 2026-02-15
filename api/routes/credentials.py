"""Credential management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pydantic import ValidationError

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
        try:
            validated_config = validate_credential_config(credential_type_enum, credential_data.config)
            credential.config = validated_config
        except ValidationError as e:
            # Extract just the error messages from Pydantic validation errors
            messages = [err.get('msg', str(err)) for err in e.errors()]
            raise HTTPException(status_code=422, detail="; ".join(messages))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

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

