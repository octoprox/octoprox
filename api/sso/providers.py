# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The admin's sign-in provider settings: each provider's OAuth client, every save recorded with the secret masked."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.db.auth_repository import AuthProviderRepository
from api.models.auth import (
    MICROSOFT_MULTI_TENANT_ALIASES,
    PROVIDER_LABELS,
    AuthProviderConfig,
    AuthProviderKind,
)
from api.services.errors import WriteRejectedError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.models.auth import AuthProviderDoc

logger = structlog.get_logger()


class SsoProviderService:
    """Reads every provider's client, set up or not, and replaces one."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def every(self) -> list[AuthProviderConfig]:
        """Every provider Octoprox knows, in its order: as stored, or unconfigured while it has no row."""
        async with self._session_factory() as session:
            stored = await AuthProviderRepository(session).get_all()
        return [stored.get(kind) or AuthProviderConfig(kind=kind) for kind in AuthProviderKind]

    async def save(
        self, kind: AuthProviderKind, doc: AuthProviderDoc, actor: Actor
    ) -> AuthProviderConfig:
        """Replace a provider's client; a null secret keeps the stored one. Microsoft names one tenant, and an enabled
        provider needs its client (and Microsoft its tenant)."""
        async with self._session_factory() as session:
            repo = AuthProviderRepository(session)
            stored = await repo.get(kind)
            current = stored or AuthProviderConfig(kind=kind)
            secret = current.client_secret if doc.client_secret is None else doc.client_secret
            updated = AuthProviderConfig(
                kind=kind,
                enabled=doc.enabled,
                client_id=doc.client_id,
                client_secret=secret,
                tenant_id=doc.tenant_id if kind == AuthProviderKind.MICROSOFT else "",
                updated_by=actor.name,
            )
            if (
                kind == AuthProviderKind.MICROSOFT
                and updated.tenant_id.lower() in MICROSOFT_MULTI_TENANT_ALIASES
            ):
                raise WriteRejectedError(
                    "Name the tenant whose accounts may sign in; 'common', 'organizations' and 'consumers' would admit any Microsoft account"
                )
            if updated.enabled and not updated.configured:
                missing = (
                    "a tenant ID"
                    if updated.needs_tenant and not updated.tenant_id
                    else "a client ID and secret"
                )
                raise WriteRejectedError(
                    f"{PROVIDER_LABELS[kind]} sign-in needs {missing} before it can be enabled"
                )
            saved = await repo.save(updated)
            if stored is None:
                await actor.created(session, saved)
            else:
                await actor.updated(session, stored, saved)
            await session.commit()
        logger.info(
            "Sign-in provider saved", provider=kind.value, enabled=saved.enabled, by=actor.name
        )
        return saved
