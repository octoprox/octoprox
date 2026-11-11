# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log over HTTP: an entity's history, and how long entries are kept.

A project's entities' history is open to everyone signed in, who may read the entities too; the install's is for
admins.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request

from api.core.auth import CurrentUserDep, DbDep, RequireAdminDep
from api.db.audit_repository import AuditRepository, SystemSettingsRepository
from api.models.audit import AuditEntityType, AuditHistory, AuditRetention, AuditRetentionUpdate
from api.models.user import UserRole
from api.routes.common import actor_of

logger = structlog.get_logger()

router = APIRouter(prefix="/audit")

# A History page when the caller does not say, and the most one may ask for.
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


@router.get("/retention", response_model=AuditRetention)
async def get_retention(session: DbDep, _admin: RequireAdminDep) -> AuditRetention:
    """How many days audit entries are kept (admin); 0 keeps them forever."""
    return (await SystemSettingsRepository(session).get()).audit_retention()


@router.put("/retention", response_model=AuditRetention)
async def put_retention(
    request: Request, session: DbDep, body: AuditRetentionUpdate, admin: RequireAdminDep
) -> AuditRetention:
    """Set how many days audit entries are kept (admin), itself recorded; the hourly compaction applies it."""
    repository = SystemSettingsRepository(session)
    before = await repository.get()
    after = await repository.set_audit_retention(body.days)
    # Only the column this save wrote: the row it returns can carry another request's license save.
    changed = before.model_copy(update={"audit_retention_days": after.audit_retention_days})
    await actor_of(request, admin).updated(session, before, changed)
    await session.commit()
    logger.info("Audit retention changed", days=body.days, admin=admin.username)
    return after.audit_retention()


@router.get("/{entity_type}/{entity_id}", response_model=AuditHistory)
async def entity_history(
    session: DbDep,
    user: CurrentUserDep,
    entity_type: AuditEntityType,
    entity_id: str,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = DEFAULT_HISTORY_LIMIT,
    before: Annotated[str | None, Query(description="The id of the oldest entry already shown")] = None,
) -> AuditHistory:
    """One entity's entries, newest first, ``limit`` at a time, each next page read ``before`` the last entry shown.

    The install's types (users, providers, settings) are for admins only.
    """
    if entity_type.admin_only and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return await AuditRepository(session).history(entity_type, entity_id, limit, before)
