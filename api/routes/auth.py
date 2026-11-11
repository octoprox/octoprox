# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Authentication routes for Octoprox."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NoReturn

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt  # type: ignore[import-untyped]
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.api_tokens import looks_like_token
from api.core.config import settings
from api.core.password import verify_password
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.user import SetPasswordRequest, User
from api.routes.common import audit_log_of

logger = structlog.get_logger()

router = APIRouter(prefix="/auth")

ALGORITHM = "HS256"

# What a refused password login answers, whichever check refused it.
INVALID_LOGIN = "Invalid username or password"


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


async def _refuse_login(
    request: Request,
    session: AsyncSession,
    login_req: LoginRequest,
    reason: str,
    user: User | None = None,
    *,
    detail: str = INVALID_LOGIN,
) -> NoReturn:
    """Answer a refused login with 401 and ``detail``, once the log and the audit log have heard ``reason``."""
    logger.warning("Failed login attempt", username=login_req.username, reason=reason)
    # The request's connection goes back to the pool first: the hook may open a session of its own.
    await session.rollback()
    await audit_log_of(request).sign_in_failed(
        request, "password", reason, user=user, username=None if user else login_req.username
    )
    raise HTTPException(status_code=401, detail=detail)


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, login_req: LoginRequest, session: DbDep) -> LoginResponse:
    """Authenticate user against database and return JWT token.

    An unknown user, a user without a password and a wrong password are refused alike; the audit log hears which.
    """
    repo = UserRepository(session)
    user = await repo.get_by_username(login_req.username)

    if user is None:
        await _refuse_login(request, session, login_req, "unknown user")
    if not user.password_hash:
        await _refuse_login(request, session, login_req, "no password set", user)
    if not verify_password(login_req.password, user.password_hash):
        await _refuse_login(request, session, login_req, "wrong password", user)
    if not user.is_active:
        await _refuse_login(request, session, login_req, "account disabled", user, detail="Account is disabled")

    token = issue_session_token(user)
    await repo.touch_last_login(user.id)
    # Committed before the hook, which may open a session of its own.
    await session.commit()
    await audit_log_of(request).signed_in(request, user, "password")

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
            current = await user_from_api_token(request, session, token)
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
async def set_password(request: Request, data: SetPasswordRequest, session: DbDep) -> LoginResponse:
    """Set password using an invite token (unauthenticated).

    The user is the actor of the change, and the audit log hears of the sign-in, or of why the link was refused.
    """
    audit_log = audit_log_of(request)
    repo = UserRepository(session)
    user = await repo.get_by_invite_token(data.token)

    # Each hook below runs once the request's connection is back in the pool: it may open a session of its own.
    if not user:
        await session.rollback()
        await audit_log.sign_in_failed(request, "invite", "unknown link")
        raise HTTPException(status_code=404, detail="Invalid invite token")

    if user.invite_token_expires_at and user.invite_token_expires_at < utc_now():
        await session.rollback()
        await audit_log.sign_in_failed(request, "invite", "expired link", user=user)
        raise HTTPException(status_code=400, detail="Invite token has expired")

    if not user.is_active:
        await session.rollback()
        await audit_log.sign_in_failed(request, "invite", "account disabled", user=user)
        raise HTTPException(status_code=400, detail="Account is disabled")

    before = user.model_copy()
    user.set_password(data.password)
    await repo.update(user)
    await repo.touch_last_login(user.id)
    actor = audit_log.actor(user.id, user.username, getattr(request.state, "request_id", None))
    await actor.updated(session, before, user, details={"via": "invite"})

    token = issue_session_token(user)
    await session.commit()
    await audit_log.signed_in(request, user, "invite")

    logger.info("User set password via invite", username=user.username)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )
