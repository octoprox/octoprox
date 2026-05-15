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
from api.core.config import settings
from api.core.password import hash_password, verify_password
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.user import SetPasswordRequest

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

    token = create_jwt(
        payload={
            "sub": user.username,
            "user_id": user.id,
            "role": user.role.value,
        },
        secret=settings.jwt_secret,
        expiry_hours=settings.jwt_expiry_hours,
    )

    logger.info("User logged in", username=user.username, role=user.role.value)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request, session: DbDep) -> AuthStatus:
    """Get current authentication status."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = verify_jwt(token, settings.jwt_secret)
        if payload:
            user_id = payload.get("user_id")
            theme_preference: str | None = None
            if user_id:
                user = await UserRepository(session).get_by_id(user_id)
                if user:
                    theme_preference = user.theme_preference
            return AuthStatus(
                authenticated=True,
                username=payload.get("sub"),
                role=payload.get("role"),
                user_id=user_id,
                theme_preference=theme_preference,
            )

    return AuthStatus(authenticated=False)


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

    token = create_jwt(
        payload={
            "sub": user.username,
            "user_id": user.id,
            "role": user.role.value,
        },
        secret=settings.jwt_secret,
        expiry_hours=settings.jwt_expiry_hours,
    )

    logger.info("User set password via invite", username=user.username)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )
