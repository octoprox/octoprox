# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential management endpoints.

Thin over :class:`~api.services.credentials.CredentialService`, which
validates configs with the provider registry and runs the vendor-side check,
so this module is the same for every provider; a credential of another project
is not found.
"""

from fastapi import APIRouter, Request

from api.core.auth import RequireEditorDep
from api.models.credential import (
    CredentialCreate,
    CredentialDetailResponse,
    CredentialListResponse,
    CredentialUpdate,
)
from api.routes.common import actor_of, credential_service_of

router = APIRouter(prefix="/projects/{project_id}/credentials")


@router.get("", response_model=CredentialListResponse)
async def list_credentials(request: Request, project_id: str) -> CredentialListResponse:
    """List all credentials for a project, oldest first (ties by id, so every api process lists them alike)."""
    return CredentialListResponse.of(await credential_service_of(request).of_project(project_id))


@router.post("", response_model=CredentialDetailResponse, status_code=201)
async def create_credential(
    request: Request,
    credential_data: CredentialCreate,
    project_id: str,
    user: RequireEditorDep,
) -> CredentialDetailResponse:
    """Create a new credential, checked with the vendor when its provider has a validation call."""
    credential = await credential_service_of(request).create(project_id, credential_data, actor_of(request, user))
    return CredentialDetailResponse.of(credential)


@router.get("/{credential_id}", response_model=CredentialDetailResponse)
async def get_credential(request: Request, project_id: str, credential_id: str) -> CredentialDetailResponse:
    """Get a specific credential of the project by ID (includes config)."""
    return CredentialDetailResponse.of(await credential_service_of(request).get(project_id, credential_id))


@router.patch("/{credential_id}", response_model=CredentialDetailResponse)
async def update_credential(
    request: Request,
    project_id: str,
    credential_id: str,
    credential_data: CredentialUpdate,
    user: RequireEditorDep,
) -> CredentialDetailResponse:
    """Update a credential's name or config; a refused edit leaves the stored credential as it was."""
    credential = await credential_service_of(request).update(
        project_id, credential_id, credential_data, actor_of(request, user)
    )
    return CredentialDetailResponse.of(credential)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(
    request: Request, project_id: str, credential_id: str, user: RequireEditorDep
) -> None:
    """Delete a credential; refused while a connector uses it."""
    await credential_service_of(request).delete(project_id, credential_id, actor_of(request, user))
