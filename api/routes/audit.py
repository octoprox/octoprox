# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log over HTTP: an entity's history, and how long entries are kept.

A project's entities' history is open to everyone signed in, who may read the entities too; the install's is for
admins.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from api.core.auth import CurrentUserDep, RequireAdminDep
from api.models.audit import AuditEntityType, AuditHistory, AuditRetention, AuditRetentionUpdate
from api.routes.common import actor_of, audit_service_of, system_settings_service_of
from api.services.audit import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT

router = APIRouter(prefix="/audit")


@router.get("/retention", response_model=AuditRetention)
async def get_retention(request: Request, _admin: RequireAdminDep) -> AuditRetention:
    """How many days audit entries are kept (admin); 0 keeps them forever."""
    return (await system_settings_service_of(request).get()).audit_retention()


@router.put("/retention", response_model=AuditRetention)
async def put_retention(request: Request, body: AuditRetentionUpdate, admin: RequireAdminDep) -> AuditRetention:
    """Set how many days audit entries are kept (admin), itself recorded; the hourly compaction applies it."""
    saved = await system_settings_service_of(request).set_audit_retention(body.days, actor_of(request, admin))
    return saved.audit_retention()


@router.get("/{entity_type}/{entity_id}", response_model=AuditHistory)
async def entity_history(
    request: Request,
    user: CurrentUserDep,
    entity_type: AuditEntityType,
    entity_id: str,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
    before: Annotated[str | None, Query(description="The id of the oldest entry already shown")] = None,
) -> AuditHistory:
    """One entity's entries, newest first, ``limit`` at a time, each next page read ``before`` the last entry shown.

    The install's types (users, providers, settings) are for admins only.
    """
    return await audit_service_of(request).history(entity_type, entity_id, limit, before, user)
