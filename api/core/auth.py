# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Authentication middleware and dependencies for Octoprox."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal

import structlog
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.api_tokens import hash_token, looks_like_token
from api.core.config import settings
from api.db.auth_repository import ApiTokenRepository
from api.db.session import get_db
from api.models.user import UserRole
from api.routes.auth import verify_jwt
from api.routes.common import audit_log_of

# A token's last use is stamped at most this often, so a busy script does
# not turn every call into a write.
_LAST_USED_GRANULARITY = timedelta(minutes=1)

# Bearer token - auto_error=False so we can provide custom error messages
security = HTTPBearer(auto_error=False)

SecurityDep = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]


@dataclass
class CurrentUser:
    """Represents the currently authenticated user."""

    id: str
    username: str
    role: UserRole
    # How the caller proved who they are: a session JWT (the UI, or a
    # script that logged in with a password) or a personal access token.
    auth_method: Literal["jwt", "token"] = "jwt"
    # The personal access token the caller authenticated with; None for a session JWT.
    token_id: str | None = None
    token_name: str | None = None


DbDep = Annotated[AsyncSession, Depends(get_db)]


async def user_from_api_token(request: Request, session: AsyncSession, bearer: str) -> CurrentUser:
    """Resolve a personal access token to its user as the user is right now, read with the token in one statement.

    Nothing is cached, so revoking the token, disabling the user or changing their role applies on the next call.
    """
    tokens = ApiTokenRepository(session)
    found = await tokens.get_with_user(hash_token(bearer))
    if found is None or found[0].expired:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, user = found
    if not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="Account is disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = utc_now()
    first = False
    if token.last_used_at is None:
        first = await tokens.touch_first_use(token.id, now)
    elif now - token.last_used_at >= _LAST_USED_GRANULARITY:
        await tokens.touch_last_used(token.id, now, _LAST_USED_GRANULARITY)
    # Committed before the endpoint runs: the request's connection goes back
    # to the pool, a stamp holds the token's row this briefly, and a call
    # refused further on keeps its stamp. The first use's hook records in a
    # transaction of its own, so it comes after.
    await session.commit()
    if first:
        await audit_log_of(request).token_first_used(request, token, user)
    return CurrentUser(
        id=user.id,
        username=user.username,
        role=user.role,
        auth_method="token",
        token_id=token.id,
        token_name=token.name,
    )


async def get_current_user(
    request: Request,
    credentials: SecurityDep,
    session: DbDep,
) -> CurrentUser:
    """Get the current authenticated user from the bearer token.

    The bearer is either a session JWT or a personal access token (which
    starts with ``octo_``, see api.core.api_tokens).

    Returns:
        CurrentUser with id, username, and role.

    Raises:
        HTTPException: If token is invalid or missing.
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if looks_like_token(credentials.credentials):
        user = await user_from_api_token(request, session, credentials.credentials)
    else:
        payload = verify_jwt(credentials.credentials, settings.jwt_secret)
        if not payload:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = CurrentUser(
            id=payload.get("user_id", ""),
            username=payload.get("sub", ""),
            role=UserRole(payload.get("role", "viewer")),
        )
    # From here on every log line for this request names the actor. The
    # request middleware clears these at the end of the request.
    structlog.contextvars.bind_contextvars(user_id=user.id, username=user.username)
    return user


# Type alias for the current user dependency
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def require_auth(user: CurrentUserDep) -> CurrentUser:
    """Dependency that requires authentication. Use on router-level dependencies."""
    return user


def _require_role(*allowed_roles: UserRole):  # type: ignore[no-untyped-def]
    """Factory that creates a dependency requiring specific roles."""

    async def _check_role(user: CurrentUserDep) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _check_role


# Convenience dependencies
require_admin = _require_role(UserRole.ADMIN)
require_editor = _require_role(UserRole.ADMIN, UserRole.EDITOR)

# Typed dependency aliases for use in endpoint signatures
RequireAdminDep = Annotated[CurrentUser, Depends(require_admin)]
RequireEditorDep = Annotated[CurrentUser, Depends(require_editor)]
