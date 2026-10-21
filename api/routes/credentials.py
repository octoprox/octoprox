# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential management endpoints.

Thin over :class:`~api.services.credentials.CredentialService`, which
validates configs with the provider registry and runs the vendor-side check,
so this module is the same for every provider; a credential of another project
is not found.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.core.auth import RequireEditorDep
from api.db.repository import CredentialRepository
from api.models.credential import (
    Credential,
    CredentialCreate,
    CredentialDetailResponse,
    CredentialResponse,
    CredentialUpdate,
)
from api.routes.common import credential_service_of, require_project, session_factory_of

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


@router.get("", response_model=CredentialListResponse)
async def list_credentials(request: Request, project_id: str) -> CredentialListResponse:
    """List all credentials for a project, oldest first (ties by id, so every api process lists them alike)."""
    await require_project(request, project_id)
    async with session_factory_of(request)() as session:
        credentials = await CredentialRepository(session).get_by_project(project_id)
    credentials.sort(key=lambda credential: (credential.created_at, credential.id))
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
    """Create a new credential, checked with the vendor when its provider has a validation call."""
    credential = await credential_service_of(request).create(project_id, credential_data)
    return _credential_to_detail_response(credential)


@router.get("/{credential_id}", response_model=CredentialDetailResponse)
async def get_credential(request: Request, project_id: str, credential_id: str) -> CredentialDetailResponse:
    """Get a specific credential of the project by ID (includes config)."""
    async with session_factory_of(request)() as session:
        credential = await CredentialRepository(session).get_in_project(project_id, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return _credential_to_detail_response(credential)


@router.patch("/{credential_id}", response_model=CredentialDetailResponse)
async def update_credential(
    request: Request,
    project_id: str,
    credential_id: str,
    credential_data: CredentialUpdate,
    _guard: RequireEditorDep,
) -> CredentialDetailResponse:
    """Update a credential's name or config; a refused edit leaves the stored credential as it was."""
    credential = await credential_service_of(request).update(project_id, credential_id, credential_data)
    return _credential_to_detail_response(credential)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(
    request: Request, project_id: str, credential_id: str, _guard: RequireEditorDep
) -> None:
    """Delete a credential; refused while a connector uses it."""
    await credential_service_of(request).delete(project_id, credential_id)
