# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log's History: one entity's entries a page at a time, those of the install's entities for admins only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.db.audit_repository import AuditRepository
from api.models.user import UserRole
from api.services.errors import ForbiddenError

if TYPE_CHECKING:
    from api.core.auth import CurrentUser
    from api.db.session import SessionFactory
    from api.models.audit import AuditEntityType, AuditHistory

# A History page when the caller does not say, and the most one may ask for.
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


class AuditService:
    """Reads an entity's History for the callers who may read it."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def history(
        self,
        entity_type: AuditEntityType,
        entity_id: str,
        limit: int,
        before: str | None,
        caller: CurrentUser,
    ) -> AuditHistory:
        """One entity's entries, newest first, ``limit`` at a time, each page read ``before`` the last entry shown;
        the install's types (users, providers, settings) are for admins only."""
        if entity_type.admin_only and caller.role != UserRole.ADMIN:
            raise ForbiddenError("Insufficient permissions")
        async with self._session_factory() as session:
            return await AuditRepository(session).history(entity_type, entity_id, limit, before)
