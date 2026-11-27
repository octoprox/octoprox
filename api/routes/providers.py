# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider catalog and admin-managed provider descriptors, served by the api role.

The catalog reads and the writes apply the stored rows to this registry first, so any api sees what another just wrote.
"""

from __future__ import annotations

from collections.abc import Awaitable

from fastapi import APIRouter, HTTPException, Request, Response

from api.core.auth import RequireAdminDep, RequireEditorDep
from api.models.provider import (
    ProviderCreate,
    ProviderDetail,
    ProviderImportRequest,
    ProviderListResponse,
    ProviderOptionsRequest,
    ProviderOptionsResponse,
    ProviderTestRequest,
    ProviderTestResponse,
    ProviderUpdate,
    ProviderValidateRequest,
    ProviderValidateResponse,
)
from api.routes.common import actor_of, provider_call_service_of, provider_service_of
from api.services.providers import ProviderNotRegisteredError

router = APIRouter(prefix="/providers")


async def _registered(write: Awaitable[ProviderDetail]) -> ProviderDetail:
    """The written provider's entry; 500 when its descriptor was stored but this registry could not take it."""
    try:
        return await write
    except ProviderNotRegisteredError:
        raise HTTPException(status_code=500, detail="Provider was stored but could not be registered") from None


# --- catalog (all authenticated users) ------------------------------------------------


@router.get("", response_model=ProviderListResponse)
async def list_providers(request: Request) -> ProviderListResponse:
    """Catalog of every provider type with its form schemas, the stored descriptors as Postgres has them."""
    return await provider_service_of(request).every()


@router.get("/{provider_id}", response_model=ProviderDetail)
async def get_provider(request: Request, provider_id: str) -> ProviderDetail:
    """One provider type's catalog entry with its full descriptor, a stored one as Postgres has it."""
    return await provider_service_of(request).get(provider_id)


@router.post("/{provider_id}/options/{option_name}", response_model=ProviderOptionsResponse)
async def resolve_options(
    request: Request,
    provider_id: str,
    option_name: str,
    body: ProviderOptionsRequest,
    _guard: RequireEditorDep,
) -> ProviderOptionsResponse:
    """Resolve a descriptor's dynamic select options for a saved ``credential_id`` or an in-progress ``credential_config``.

    Admins may also pass an unsaved ``spec``, so the provider builder's test panel drives dynamic selects before the
    descriptor is saved; the static egress checks apply as on save.
    """
    return await provider_call_service_of(request).options(provider_id, option_name, body, _guard)


# --- admin: authoring -----------------------------------------------------------------


@router.post("/validate", response_model=ProviderValidateResponse)
async def validate_provider(
    request: Request, body: ProviderValidateRequest, _admin: RequireAdminDep
) -> ProviderValidateResponse:
    """Dry-run: parse a descriptor and report hosts, errors and warnings."""
    return provider_service_of(request).validate(body.spec)


@router.post("", response_model=ProviderDetail, status_code=201)
async def create_provider(request: Request, body: ProviderCreate, admin: RequireAdminDep) -> ProviderDetail:
    """Create a custom provider descriptor (admin)."""
    return await _registered(provider_service_of(request).create(body, actor_of(request, admin)))


@router.post("/import", response_model=ProviderDetail, status_code=201)
async def import_provider(request: Request, body: ProviderImportRequest, admin: RequireAdminDep) -> ProviderDetail:
    """Create a custom provider from a YAML document (admin)."""
    return await _registered(provider_service_of(request).import_yaml(body, actor_of(request, admin)))


@router.put("/{provider_id}", response_model=ProviderDetail)
async def update_provider(
    request: Request, provider_id: str, body: ProviderUpdate, admin: RequireAdminDep
) -> ProviderDetail:
    """Update a custom provider descriptor (admin)."""
    return await provider_service_of(request).update(provider_id, body, actor_of(request, admin))


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(request: Request, provider_id: str, admin: RequireAdminDep) -> None:
    """Delete a custom provider descriptor (admin). Refused while credentials use it."""
    await provider_service_of(request).delete(provider_id, actor_of(request, admin))


@router.get("/{provider_id}/export")
async def export_provider(request: Request, provider_id: str, _admin: RequireAdminDep) -> Response:
    """Download a descriptor as YAML (admin), as stored. Built-ins can be exported as a starting point."""
    return Response(
        content=await provider_service_of(request).export(provider_id),
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{provider_id}.yaml"'},
    )


@router.post("/{provider_id}/test", response_model=ProviderTestResponse)
async def test_provider(
    request: Request, provider_id: str, body: ProviderTestRequest, _admin: RequireAdminDep
) -> ProviderTestResponse:
    """Exercise a shipped or custom descriptor with throwaway config (admin), ``proxy_request`` fetching a URL through it.

    ``spec`` tests an unsaved draft, under the static egress checks, so a draft cannot probe private networks.
    """
    return await provider_call_service_of(request).exercise(provider_id, body)
