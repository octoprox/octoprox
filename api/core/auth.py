# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Authentication middleware and dependencies for Octoprox."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.core.config import settings
from api.models.user import UserRole
from api.routes.auth import verify_jwt

# Bearer token - auto_error=False so we can provide custom error messages
security = HTTPBearer(auto_error=False)

SecurityDep = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]


@dataclass
class CurrentUser:
    """Represents the currently authenticated user."""

    id: str
    username: str
    role: UserRole


async def get_current_user(
    request: Request,
    credentials: SecurityDep,
) -> CurrentUser:
    """Get the current authenticated user from JWT token.

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

    payload = verify_jwt(credentials.credentials, settings.jwt_secret)
    if not payload:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(
        id=payload.get("user_id", ""),
        username=payload.get("sub", ""),
        role=UserRole(payload.get("role", "viewer")),
    )


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
