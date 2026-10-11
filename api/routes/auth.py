# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Authentication routes for Octoprox."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt  # type: ignore[import-untyped]
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.api_tokens import looks_like_token
from api.core.config import settings
from api.core.password import hash_password, verify_password
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.user import SetPasswordRequest, User

logger = structlog.get_logger()

router = APIRouter(prefix="/auth")

ALGORITHM = "HS256"


class LoginRequest(BaseModel):
    """Login request payload."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Login response with JWT token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthStatus(BaseModel):
    """Authentication status response."""

    authenticated: bool = False
    username: str | None = None
    role: str | None = None
    user_id: str | None = None
    theme_preference: str | None = None


def create_jwt(payload: dict[str, Any], secret: str, expiry_hours: int) -> str:
    """Create a JWT token."""
    to_encode = payload.copy()
    expire = datetime.now(UTC) + timedelta(hours=expiry_hours)
    to_encode.update({"exp": expire})
    encoded: str = jwt.encode(to_encode, secret, algorithm=ALGORITHM)
    return encoded


def issue_session_token(user: User) -> str:
    """The session JWT for a user who has just proved who they are, by whatever means.

    What the UI and password-login scripts carry; the role is baked in until
    it expires, unlike a personal access token (api.core.auth).
    """
    return create_jwt(
        payload={"sub": user.username, "user_id": user.id, "role": user.role.value},
        secret=settings.jwt_secret,
        expiry_hours=settings.jwt_expiry_hours,
    )


def verify_jwt(token: str, secret: str) -> dict[str, Any] | None:
    """Verify a JWT token and return the payload."""
    try:
        payload: dict[str, Any] = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.post("/login", response_model=LoginResponse)
async def login(login_req: LoginRequest, session: DbDep) -> LoginResponse:
    """Authenticate user against database and return JWT token."""
    repo = UserRepository(session)
    user = await repo.get_by_username(login_req.username)

    if not user or not user.password_hash or not verify_password(login_req.password, user.password_hash):
        logger.warning("Failed login attempt", username=login_req.username)
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    if not user.is_active:
        logger.warning("Inactive user login attempt", username=login_req.username)
        raise HTTPException(
            status_code=401,
            detail="Account is disabled",
        )

    token = issue_session_token(user)
    await repo.touch_last_login(user.id)

    logger.info("User logged in", username=user.username, role=user.role.value)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request, session: DbDep) -> AuthStatus:
    """Get current authentication status, for a session JWT or a personal access token alike."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return AuthStatus(authenticated=False)
    token = auth_header[7:]

    username: str | None
    role: str | None
    user_id: str | None
    if looks_like_token(token):
        # api.core.auth imports verify_jwt from this module, so the import goes this way lazily.
        from api.core.auth import user_from_api_token

        try:
            current = await user_from_api_token(session, token)
        except HTTPException:
            return AuthStatus(authenticated=False)
        username, role, user_id = current.username, current.role.value, current.id
    else:
        payload = verify_jwt(token, settings.jwt_secret)
        if not payload:
            return AuthStatus(authenticated=False)
        username, role, user_id = payload.get("sub"), payload.get("role"), payload.get("user_id")

    theme_preference: str | None = None
    if user_id:
        user = await UserRepository(session).get_by_id(user_id)
        if user:
            theme_preference = user.theme_preference
    return AuthStatus(
        authenticated=True,
        username=username,
        role=role,
        user_id=user_id,
        theme_preference=theme_preference,
    )


@router.post("/set-password", response_model=LoginResponse)
async def set_password(data: SetPasswordRequest, session: DbDep) -> LoginResponse:
    """Set password using an invite token (unauthenticated)."""
    repo = UserRepository(session)
    user = await repo.get_by_invite_token(data.token)

    if not user:
        raise HTTPException(status_code=404, detail="Invalid invite token")

    if user.invite_token_expires_at and user.invite_token_expires_at < utc_now():
        raise HTTPException(status_code=400, detail="Invite token has expired")

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Account is disabled")

    user.password_hash = hash_password(data.password)
    user.invite_token = None
    user.invite_token_expires_at = None
    await repo.update(user)
    await repo.touch_last_login(user.id)

    token = issue_session_token(user)

    logger.info("User set password via invite", username=user.username)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )
