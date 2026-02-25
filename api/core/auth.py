# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Authentication middleware and dependencies for Octoprox."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.core.config import settings
from api.routes.auth import verify_jwt

# Optional bearer token - doesn't fail if no token provided
security = HTTPBearer(auto_error=False)

# Type alias for the security dependency
SecurityDep = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]


async def get_current_user(
    request: Request,
    credentials: SecurityDep,
) -> str | None:
    """Get the current authenticated user from JWT token.

    Returns:
        Username if authenticated, None if auth is disabled

    Raises:
        HTTPException: If auth is enabled but token is invalid/missing
    """
    # If auth is disabled, allow all requests
    if not settings.auth_enabled:
        return None

    # Auth is enabled - require valid token
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

    return payload.get("sub")


# Type alias for the current user dependency
CurrentUserDep = Annotated[str | None, Depends(get_current_user)]


async def require_auth(
    user: CurrentUserDep,
) -> str | None:
    """Dependency that requires authentication when enabled.

    Use this as a dependency on routes that should be protected.
    """
    return user

