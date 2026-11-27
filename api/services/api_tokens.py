# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Personal access tokens: a user's own under /me, anyone's for an admin, each issue and revoke told to its hook.

Issuing one needs a session: a token may not mint another (see :meth:`~api.core.auth.CurrentUser.require_session`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.api_tokens import generate_token
from api.db.auth_repository import ApiTokenRepository
from api.db.repository import UserRepository
from api.models.auth import ApiToken, IssuedToken
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.core.auth import CurrentUser
    from api.db.session import SessionFactory
    from api.models.auth import ApiTokenCreate

logger = structlog.get_logger()


class ApiTokenService:
    """Lists, issues and revokes users' personal access tokens; never keeps a secret."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def own(self, caller: CurrentUser) -> list[ApiToken]:
        """The caller's tokens; none for a session naming no user."""
        async with self._session_factory() as session:
            return await ApiTokenRepository(session).list_for_user(caller.id)

    async def of_user(self, user_id: str) -> list[ApiToken]:
        """A user's tokens."""
        async with self._session_factory() as session:
            if await UserRepository(session).get_by_id(user_id) is None:
                raise NotFoundError("User not found")
            return await ApiTokenRepository(session).list_for_user(user_id)

    async def issue(
        self, user_id: str, data: ApiTokenCreate, caller: CurrentUser, actor: Actor
    ) -> IssuedToken:
        """A new token for the user, with the secret that exists only in this answer; the caller needs a session."""
        caller.require_session("create tokens")
        async with self._session_factory() as session:
            if await UserRepository(session).get_by_id(user_id) is None:
                raise NotFoundError("User not found")
            secret, token_hash, prefix = generate_token()
            token = ApiToken(
                user_id=user_id,
                name=data.name,
                token_hash=token_hash,
                token_prefix=prefix,
                created_by=actor.name,
                expires_at=utc_now() + timedelta(days=data.expires_in_days)
                if data.expires_in_days
                else None,
            )
            await ApiTokenRepository(session).create(token)
            await actor.token_issued(session, token)
            await session.commit()
        logger.info(
            "API token created", user_id=user_id, token_id=token.id, name=token.name, by=actor.name
        )
        return IssuedToken(token=token, secret=secret)

    async def revoke(self, user_id: str, token_id: str, actor: Actor) -> None:
        """Revoke one of the user's tokens; it is refused from its next call on."""
        async with self._session_factory() as session:
            token = await ApiTokenRepository(session).delete(user_id, token_id)
            if token is None:
                raise NotFoundError("Token not found")
            await actor.token_revoked(session, token)
            await session.commit()
        logger.info("API token revoked", user_id=user_id, token_id=token_id, by=actor.name)
