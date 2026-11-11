# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""User management routes for Octoprox.

Each change to a user is recorded in the audit log; issuing and revoking a
token and unlinking a sign-in are told to its hooks.
"""

import secrets
from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.api_tokens import generate_token
from api.core.audit import Actor
from api.core.auth import CurrentUser, CurrentUserDep, RequireAdminDep
from api.core.config import settings
from api.core.password import hash_password, verify_password
from api.db.auth_repository import ApiTokenRepository, AuthIdentityRepository
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.audit import AuditAction
from api.models.auth import (
    PROVIDER_LABELS,
    ApiToken,
    ApiTokenCreate,
    ApiTokenCreatedResponse,
    ApiTokenListResponse,
    ApiTokenResponse,
    AuthIdentity,
    AuthIdentityListResponse,
    AuthIdentityResponse,
)
from api.models.user import (
    InviteResponse,
    User,
    UserCreate,
    UserInviteCreate,
    UserResponse,
    UserSelfUpdate,
    UserUpdate,
)
from api.routes.common import actor_of, external_base_url

logger = structlog.get_logger()

router = APIRouter(prefix="/users")

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _require_session(user: CurrentUser, action: str) -> None:
    """Refuse an API-token caller a change to how a human gets in.

    A personal access token proves only possession of the token. It may do
    what the user's role allows, but not set a password, mint a token or
    issue a set-password link: a token leaked from a CI log must not be
    able to create a way in that outlives its own revocation. A session
    JWT (UI, or a script that logged in with a password) proves the person.
    """
    if user.auth_method == "token":
        raise HTTPException(status_code=403, detail=f"Sign in to the UI or with a password to {action}")


class UserListResponse(BaseModel):
    """Response for listing users."""

    total: int
    users: list[UserResponse]


def _to_response(user: User, providers: list[str] | None = None) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        has_password=user.password_hash is not None,
        sign_in_providers=providers or [],
        theme_preference=user.theme_preference,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# --- Self endpoints (any authenticated user) ---
# These must be declared BEFORE /{user_id} to avoid path conflicts.


@router.get("/me", response_model=UserResponse)
async def get_self(session: DbDep, user: CurrentUserDep) -> UserResponse:
    """Get the current user's profile."""
    repo = UserRepository(session)
    db_user = await repo.get_by_id(user.id)

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    return _to_response(db_user, await _providers_of(session, db_user.id))


@router.patch("/me", response_model=UserResponse)
async def update_self(
    request: Request, session: DbDep, data: UserSelfUpdate, user: CurrentUserDep
) -> UserResponse:
    """Update the current user's own profile (email, password, theme).

    Setting a password needs a session (see _require_session). If the
    account has a password, changing it also asks for the current one, so
    a session left open on a shared machine cannot quietly take the
    account. If the account has none, there is nothing to ask for: the
    person is adding a password as a second way in beside the provider,
    and the session is the proof.
    """
    repo = UserRepository(session)
    db_user = await repo.get_by_id(user.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    before = db_user.model_copy()

    if data.password is not None:
        _require_session(user, "set a password")
        if db_user.password_hash:
            if not data.current_password:
                raise HTTPException(
                    status_code=400, detail="Current password is required to change password"
                )
            if not verify_password(data.current_password, db_user.password_hash):
                raise HTTPException(status_code=400, detail="Current password is incorrect")
        db_user.set_password(data.password)

    if data.email is not None:
        if data.email and data.email != db_user.email:
            existing_email = await repo.get_by_email(data.email)
            if existing_email:
                raise HTTPException(
                    status_code=400, detail=f"Email '{data.email}' already exists"
                )
        db_user.email = data.email

    if data.theme_preference is not None:
        db_user.theme_preference = data.theme_preference

    await repo.update(db_user)
    await actor_of(request, user).updated(session, before, db_user)

    return _to_response(db_user, await _providers_of(session, db_user.id))


# --- Personal access tokens and linked sign-ins ---
# A user manages their own under /me; an admin manages anyone's under
# /{user_id}, which is also how a user that only exists to hold tokens (a
# service account: no password, no email) gets its first one.


def _token_response(token: ApiToken) -> ApiTokenResponse:
    return ApiTokenResponse(
        id=token.id,
        name=token.name,
        token_prefix=token.token_prefix,
        created_by=token.created_by,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        created_at=token.created_at,
        expired=token.expired,
    )


def _identity_response(identity: AuthIdentity) -> AuthIdentityResponse:
    return AuthIdentityResponse(
        id=identity.id,
        provider=identity.provider,
        label=PROVIDER_LABELS[identity.provider],
        email=identity.email,
        created_at=identity.created_at,
        last_login_at=identity.last_login_at,
    )


async def _list_tokens(session: AsyncSession, user_id: str) -> ApiTokenListResponse:
    tokens = await ApiTokenRepository(session).list_for_user(user_id)
    return ApiTokenListResponse(tokens=[_token_response(t) for t in tokens])


async def _create_token(
    session: AsyncSession, user_id: str, data: ApiTokenCreate, actor: Actor
) -> ApiTokenCreatedResponse:
    secret, token_hash, prefix = generate_token()
    token = ApiToken(
        user_id=user_id,
        name=data.name,
        token_hash=token_hash,
        token_prefix=prefix,
        created_by=actor.name,
        expires_at=utc_now() + timedelta(days=data.expires_in_days) if data.expires_in_days else None,
    )
    await ApiTokenRepository(session).create(token)
    await actor.token_issued(session, token)
    logger.info("API token created", user_id=user_id, token_id=token.id, name=token.name, by=actor.name)
    return ApiTokenCreatedResponse(token=secret, api_token=_token_response(token))


async def _delete_token(session: AsyncSession, user_id: str, token_id: str, actor: Actor) -> None:
    token = await ApiTokenRepository(session).delete(user_id, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")
    await actor.token_revoked(session, token)
    logger.info("API token revoked", user_id=user_id, token_id=token_id, by=actor.name)


async def _list_identities(session: AsyncSession, user_id: str) -> AuthIdentityListResponse:
    identities = await AuthIdentityRepository(session).list_for_user(user_id)
    return AuthIdentityListResponse(identities=[_identity_response(i) for i in identities])


async def _delete_identity(session: AsyncSession, user_id: str, identity_id: str, actor: Actor) -> None:
    identity = await AuthIdentityRepository(session).delete(user_id, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Linked sign-in not found")
    await actor.identity_unlinked(session, identity)
    logger.info("Sign-in provider unlinked", user_id=user_id, identity_id=identity_id, by=actor.name)


@router.get("/me/tokens", response_model=ApiTokenListResponse)
async def list_my_tokens(session: DbDep, user: CurrentUserDep) -> ApiTokenListResponse:
    """The current user's personal access tokens (never the secrets)."""
    return await _list_tokens(session, user.id)


@router.post("/me/tokens", response_model=ApiTokenCreatedResponse, status_code=201)
async def create_my_token(
    request: Request, session: DbDep, data: ApiTokenCreate, user: CurrentUserDep
) -> ApiTokenCreatedResponse:
    """Mint a token for the current user. The secret is in this response and nowhere else."""
    _require_session(user, "create tokens")
    if await UserRepository(session).get_by_id(user.id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await _create_token(session, user.id, data, actor_of(request, user))


@router.delete("/me/tokens/{token_id}", status_code=204)
async def delete_my_token(request: Request, session: DbDep, token_id: str, user: CurrentUserDep) -> None:
    """Revoke one of the current user's tokens. Takes effect on the next call made with it."""
    await _delete_token(session, user.id, token_id, actor_of(request, user))


@router.get("/me/identities", response_model=AuthIdentityListResponse)
async def list_my_identities(session: DbDep, user: CurrentUserDep) -> AuthIdentityListResponse:
    """The provider accounts the current user can sign in with."""
    return await _list_identities(session, user.id)


@router.delete("/me/identities/{identity_id}", status_code=204)
async def delete_my_identity(request: Request, session: DbDep, identity_id: str, user: CurrentUserDep) -> None:
    """Unlink a provider account. Signing in with it again relinks it if the email still matches."""
    await _delete_identity(session, user.id, identity_id, actor_of(request, user))


@router.get("/{user_id}/tokens", response_model=ApiTokenListResponse)
async def list_user_tokens(session: DbDep, user_id: str, _admin: RequireAdminDep) -> ApiTokenListResponse:
    """A user's tokens (admin only)."""
    await _require_user(session, user_id)
    return await _list_tokens(session, user_id)


@router.post("/{user_id}/tokens", response_model=ApiTokenCreatedResponse, status_code=201)
async def create_user_token(
    request: Request, session: DbDep, user_id: str, data: ApiTokenCreate, admin: RequireAdminDep
) -> ApiTokenCreatedResponse:
    """Mint a token on a user's behalf (admin only); how a service account gets one."""
    _require_session(admin, "create tokens")
    await _require_user(session, user_id)
    return await _create_token(session, user_id, data, actor_of(request, admin))


@router.delete("/{user_id}/tokens/{token_id}", status_code=204)
async def delete_user_token(
    request: Request, session: DbDep, user_id: str, token_id: str, admin: RequireAdminDep
) -> None:
    """Revoke a user's token (admin only)."""
    await _delete_token(session, user_id, token_id, actor_of(request, admin))


@router.get("/{user_id}/identities", response_model=AuthIdentityListResponse)
async def list_user_identities(session: DbDep, user_id: str, _admin: RequireAdminDep) -> AuthIdentityListResponse:
    """A user's linked provider accounts (admin only)."""
    await _require_user(session, user_id)
    return await _list_identities(session, user_id)


@router.delete("/{user_id}/identities/{identity_id}", status_code=204)
async def delete_user_identity(
    request: Request, session: DbDep, user_id: str, identity_id: str, admin: RequireAdminDep
) -> None:
    """Unlink a user's provider account (admin only)."""
    await _delete_identity(session, user_id, identity_id, actor_of(request, admin))


async def _providers_of(session: AsyncSession, user_id: str) -> list[str]:
    return [i.provider.value for i in await AuthIdentityRepository(session).list_for_user(user_id)]


async def _require_user(session: AsyncSession, user_id: str) -> None:
    if await UserRepository(session).get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")


# --- Admin-only endpoints ---


@router.get("", response_model=UserListResponse)
async def list_users(session: DbDep, _admin: RequireAdminDep) -> UserListResponse:
    """List all users (admin only)."""
    repo = UserRepository(session)
    users = await repo.get_all()
    providers = await AuthIdentityRepository(session).providers_by_user()

    return UserListResponse(total=len(users), users=[_to_response(u, providers.get(u.id)) for u in users])


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    request: Request, session: DbDep, data: UserCreate, admin: RequireAdminDep
) -> UserResponse:
    """Create a new user (admin only)."""
    repo = UserRepository(session)
    existing = await repo.get_by_username(data.username)
    if existing:
        raise HTTPException(
            status_code=400, detail=f"Username '{data.username}' already exists"
        )
    if data.email:
        existing_email = await repo.get_by_email(data.email)
        if existing_email:
            raise HTTPException(
                status_code=400, detail=f"Email '{data.email}' already exists"
            )

    user = User(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    await repo.create(user)
    await actor_of(request, admin).created(session, user)

    logger.info("User created", username=user.username, role=user.role.value)
    return _to_response(user)


def _build_invite_url(request: Request, token: str) -> str:
    """Build the invite URL from the install's public URL (or the request's base URL)."""
    return f"{external_base_url(request)}/set-password/{token}"


@router.post("/invite", response_model=InviteResponse, status_code=201)
async def invite_user(
    request: Request, session: DbDep, data: UserInviteCreate, admin: RequireAdminDep
) -> InviteResponse:
    """Create a new user with an invite link (admin only)."""
    repo = UserRepository(session)
    existing = await repo.get_by_username(data.username)
    if existing:
        raise HTTPException(
            status_code=400, detail=f"Username '{data.username}' already exists"
        )
    if data.email:
        existing_email = await repo.get_by_email(data.email)
        if existing_email:
            raise HTTPException(
                status_code=400, detail=f"Email '{data.email}' already exists"
            )

    token = secrets.token_urlsafe(48)
    expires_at = utc_now() + timedelta(hours=settings.invite_token_expiry_hours)

    user = User(
        username=data.username,
        email=data.email,
        password_hash=None,
        role=data.role,
        invite_token=token,
        invite_token_expires_at=expires_at,
    )
    await repo.create(user)
    await actor_of(request, admin).created(session, user, action=AuditAction.INVITED)

    logger.info("User invited", username=user.username, role=user.role.value)
    return InviteResponse(
        user=_to_response(user),
        invite_url=_build_invite_url(request, token),
    )


@router.post("/{user_id}/reinvite", response_model=InviteResponse)
async def reinvite_user(
    request: Request, session: DbDep, user_id: str, admin: RequireAdminDep
) -> InviteResponse:
    """Regenerate an invite link for a user who hasn't set their password (admin only)."""
    _require_session(admin, "send a set-password link")
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.password_hash is not None:
        raise HTTPException(
            status_code=400, detail="User already has a password set"
        )

    before = user.model_copy()
    token = secrets.token_urlsafe(48)
    user.invite_token = token
    user.invite_token_expires_at = utc_now() + timedelta(hours=settings.invite_token_expiry_hours)
    await repo.update(user)
    await actor_of(request, admin).updated(session, before, user, action=AuditAction.INVITE_REISSUED)

    logger.info("User reinvited", user_id=user_id, username=user.username)
    return InviteResponse(
        user=_to_response(user),
        invite_url=_build_invite_url(request, token),
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    session: DbDep, user_id: str, _admin: RequireAdminDep
) -> UserResponse:
    """Get a user by ID (admin only)."""
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _to_response(user, await _providers_of(session, user.id))


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    request: Request, session: DbDep, user_id: str, data: UserUpdate, admin: RequireAdminDep
) -> UserResponse:
    """Update a user (admin only). Setting a password needs a session, as for /me."""
    if data.password is not None:
        _require_session(admin, "set a password")
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    before = user.model_copy()

    if data.username is not None:
        if data.username != user.username:
            existing = await repo.get_by_username(data.username)
            if existing:
                raise HTTPException(
                    status_code=400, detail=f"Username '{data.username}' already exists"
                )
        user.username = data.username
    if data.email is not None:
        if data.email and data.email != user.email:
            existing_email = await repo.get_by_email(data.email)
            if existing_email:
                raise HTTPException(
                    status_code=400, detail=f"Email '{data.email}' already exists"
                )
        user.email = data.email
    if data.password is not None:
        user.set_password(data.password)
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.theme_preference is not None:
        user.theme_preference = data.theme_preference

    await repo.update(user)
    await actor_of(request, admin).updated(session, before, user)

    logger.info("User updated", user_id=user_id)
    return _to_response(user, await _providers_of(session, user.id))


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    request: Request, session: DbDep, user_id: str, admin: RequireAdminDep
) -> None:
    """Delete a user (admin only). Cannot delete yourself."""
    if admin.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if user is None or not await repo.delete(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await actor_of(request, admin).deleted(session, user)

    logger.info("User deleted", user_id=user_id)
