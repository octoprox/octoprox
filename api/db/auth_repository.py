# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Persistence for sign-in providers, linked identities and API tokens."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import ApiTokenModel, AuthIdentityModel, AuthProviderModel
from api.models.auth import ApiToken, AuthIdentity, AuthProviderConfig, AuthProviderKind


class AuthProviderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> dict[AuthProviderKind, AuthProviderConfig]:
        """Every stored provider by kind. Kinds never saved are absent."""
        result = await self._session.execute(select(AuthProviderModel))
        return {AuthProviderKind(m.kind): self._to_domain(m) for m in result.scalars().all()}

    async def get(self, kind: AuthProviderKind) -> AuthProviderConfig | None:
        model = await self._session.get(AuthProviderModel, kind.value)
        return self._to_domain(model) if model else None

    async def get_enabled(self) -> list[AuthProviderConfig]:
        result = await self._session.execute(
            select(AuthProviderModel).where(AuthProviderModel.enabled.is_(True)).order_by(AuthProviderModel.kind)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def save(self, config: AuthProviderConfig) -> AuthProviderConfig:
        model = await self._session.get(AuthProviderModel, config.kind.value)
        if model is None:
            model = AuthProviderModel(kind=config.kind.value)
            self._session.add(model)
        model.enabled = config.enabled
        model.client_id = config.client_id
        model.client_secret = config.client_secret
        model.tenant_id = config.tenant_id
        model.updated_by = config.updated_by
        model.updated_at = utc_now()
        await self._session.flush()
        return self._to_domain(model)

    @staticmethod
    def _to_domain(model: AuthProviderModel) -> AuthProviderConfig:
        return AuthProviderConfig(
            kind=AuthProviderKind(model.kind),
            enabled=model.enabled,
            client_id=model.client_id,
            client_secret=model.client_secret,
            tenant_id=model.tenant_id,
            updated_by=model.updated_by,
            updated_at=model.updated_at,
        )


class AuthIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_subject(self, provider: AuthProviderKind, subject: str) -> AuthIdentity | None:
        result = await self._session.execute(
            select(AuthIdentityModel).where(
                AuthIdentityModel.provider == provider.value, AuthIdentityModel.subject == subject
            )
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_for_user(self, user_id: str, provider: AuthProviderKind) -> AuthIdentity | None:
        result = await self._session.execute(
            select(AuthIdentityModel).where(
                AuthIdentityModel.user_id == user_id, AuthIdentityModel.provider == provider.value
            )
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def list_for_user(self, user_id: str) -> list[AuthIdentity]:
        result = await self._session.execute(
            select(AuthIdentityModel)
            .where(AuthIdentityModel.user_id == user_id)
            .order_by(AuthIdentityModel.created_at)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def providers_by_user(self) -> dict[str, list[str]]:
        """Provider kinds linked to each user, for the user list in one query."""
        result = await self._session.execute(
            select(AuthIdentityModel.user_id, AuthIdentityModel.provider).order_by(AuthIdentityModel.created_at)
        )
        by_user: dict[str, list[str]] = {}
        for user_id, provider in result.all():
            by_user.setdefault(user_id, []).append(provider)
        return by_user

    async def create(self, identity: AuthIdentity) -> AuthIdentity:
        self._session.add(
            AuthIdentityModel(
                id=identity.id,
                user_id=identity.user_id,
                provider=identity.provider.value,
                subject=identity.subject,
                email=identity.email,
                created_at=identity.created_at,
                last_login_at=identity.last_login_at,
            )
        )
        await self._session.flush()
        return identity

    async def touch_last_login(self, identity_id: str, email: str) -> None:
        """Stamp the login and refresh the display email to what the provider now reports."""
        await self._session.execute(
            update(AuthIdentityModel)
            .where(AuthIdentityModel.id == identity_id)
            .values(last_login_at=utc_now(), email=email)
        )

    async def delete(self, user_id: str, identity_id: str) -> bool:
        result = await self._session.execute(
            delete(AuthIdentityModel).where(
                AuthIdentityModel.id == identity_id, AuthIdentityModel.user_id == user_id
            )
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    @staticmethod
    def _to_domain(model: AuthIdentityModel) -> AuthIdentity:
        return AuthIdentity(
            id=model.id,
            user_id=model.user_id,
            provider=AuthProviderKind(model.provider),
            subject=model.subject,
            email=model.email,
            created_at=model.created_at,
            last_login_at=model.last_login_at,
        )


class ApiTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        result = await self._session.execute(
            select(ApiTokenModel).where(ApiTokenModel.token_hash == token_hash)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def list_for_user(self, user_id: str) -> list[ApiToken]:
        result = await self._session.execute(
            select(ApiTokenModel).where(ApiTokenModel.user_id == user_id).order_by(ApiTokenModel.created_at)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def create(self, token: ApiToken) -> ApiToken:
        self._session.add(
            ApiTokenModel(
                id=token.id,
                user_id=token.user_id,
                name=token.name,
                token_hash=token.token_hash,
                token_prefix=token.token_prefix,
                created_by=token.created_by,
                expires_at=token.expires_at,
                last_used_at=token.last_used_at,
                created_at=token.created_at,
            )
        )
        await self._session.flush()
        return token

    async def touch_last_used(self, token_id: str, when: datetime) -> None:
        await self._session.execute(
            update(ApiTokenModel).where(ApiTokenModel.id == token_id).values(last_used_at=when)
        )

    async def delete(self, user_id: str, token_id: str) -> bool:
        result = await self._session.execute(
            delete(ApiTokenModel).where(ApiTokenModel.id == token_id, ApiTokenModel.user_id == user_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    @staticmethod
    def _to_domain(model: ApiTokenModel) -> ApiToken:
        return ApiToken(
            id=model.id,
            user_id=model.user_id,
            name=model.name,
            token_hash=model.token_hash,
            token_prefix=model.token_prefix,
            created_by=model.created_by,
            expires_at=model.expires_at,
            last_used_at=model.last_used_at,
            created_at=model.created_at,
        )
