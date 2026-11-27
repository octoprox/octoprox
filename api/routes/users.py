# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""User management routes for Octoprox.

Each change to a user is recorded in the audit log; issuing and revoking a
token and unlinking a sign-in are told to its hooks.
"""

from fastapi import APIRouter, Request

from api.core.auth import CurrentUserDep, RequireAdminDep
from api.models.auth import (
    ApiTokenCreate,
    ApiTokenCreatedResponse,
    ApiTokenListResponse,
    AuthIdentityListResponse,
)
from api.models.user import (
    InviteResponse,
    ServiceAccountCreate,
    UserCreate,
    UserInviteCreate,
    UserListResponse,
    UserResponse,
    UserSelfUpdate,
    UserUpdate,
)
from api.routes.common import actor_of, api_token_service_of, external_base_url, user_service_of

router = APIRouter(prefix="/users")


# --- Self endpoints (any authenticated user) ---
# These must be declared BEFORE /{user_id} to avoid path conflicts.


@router.get("/me", response_model=UserResponse)
async def get_self(request: Request, user: CurrentUserDep) -> UserResponse:
    """Get the current user's profile."""
    return UserResponse.of(await user_service_of(request).get(user.id))


@router.patch("/me", response_model=UserResponse)
async def update_self(request: Request, data: UserSelfUpdate, user: CurrentUserDep) -> UserResponse:
    """Update the current user's own profile (email, password, theme).

    Setting a password needs a session (see _require_session). If the
    account has a password, changing it also asks for the current one, so
    a session left open on a shared machine cannot quietly take the
    account. If the account has none, there is nothing to ask for: the
    person is adding a password as a second way in beside the provider,
    and the session is the proof. A service account sets neither a
    password nor an email.
    """
    return UserResponse.of(await user_service_of(request).update_own(data, user, actor_of(request, user)))


# --- Personal access tokens and linked sign-ins ---
# A user manages their own under /me; an admin manages anyone's under
# /{user_id}, which is also how a service account gets its tokens.


@router.get("/me/tokens", response_model=ApiTokenListResponse)
async def list_my_tokens(request: Request, user: CurrentUserDep) -> ApiTokenListResponse:
    """The current user's personal access tokens (never the secrets)."""
    return ApiTokenListResponse.of(await api_token_service_of(request).own(user))


@router.post("/me/tokens", response_model=ApiTokenCreatedResponse, status_code=201)
async def create_my_token(request: Request, data: ApiTokenCreate, user: CurrentUserDep) -> ApiTokenCreatedResponse:
    """Mint a token for the current user. The secret is in this response and nowhere else."""
    issued = await api_token_service_of(request).issue(user.id, data, user, actor_of(request, user))
    return ApiTokenCreatedResponse.of(issued)


@router.delete("/me/tokens/{token_id}", status_code=204)
async def delete_my_token(request: Request, token_id: str, user: CurrentUserDep) -> None:
    """Revoke one of the current user's tokens. Takes effect on the next call made with it."""
    await api_token_service_of(request).revoke(user.id, token_id, actor_of(request, user))


@router.get("/me/identities", response_model=AuthIdentityListResponse)
async def list_my_identities(request: Request, user: CurrentUserDep) -> AuthIdentityListResponse:
    """The provider accounts the current user can sign in with."""
    return AuthIdentityListResponse.of(await user_service_of(request).own_identities(user))


@router.delete("/me/identities/{identity_id}", status_code=204)
async def delete_my_identity(request: Request, identity_id: str, user: CurrentUserDep) -> None:
    """Unlink a provider account. Signing in with it again relinks it if the email still matches."""
    await user_service_of(request).unlink(user.id, identity_id, actor_of(request, user))


@router.get("/{user_id}/tokens", response_model=ApiTokenListResponse)
async def list_user_tokens(request: Request, user_id: str, _admin: RequireAdminDep) -> ApiTokenListResponse:
    """A user's tokens (admin only)."""
    return ApiTokenListResponse.of(await api_token_service_of(request).of_user(user_id))


@router.post("/{user_id}/tokens", response_model=ApiTokenCreatedResponse, status_code=201)
async def create_user_token(
    request: Request, user_id: str, data: ApiTokenCreate, admin: RequireAdminDep
) -> ApiTokenCreatedResponse:
    """Mint a token on a user's behalf (admin only); how a service account gets one."""
    issued = await api_token_service_of(request).issue(user_id, data, admin, actor_of(request, admin))
    return ApiTokenCreatedResponse.of(issued)


@router.delete("/{user_id}/tokens/{token_id}", status_code=204)
async def delete_user_token(request: Request, user_id: str, token_id: str, admin: RequireAdminDep) -> None:
    """Revoke a user's token (admin only)."""
    await api_token_service_of(request).revoke(user_id, token_id, actor_of(request, admin))


@router.get("/{user_id}/identities", response_model=AuthIdentityListResponse)
async def list_user_identities(request: Request, user_id: str, _admin: RequireAdminDep) -> AuthIdentityListResponse:
    """A user's linked provider accounts (admin only)."""
    return AuthIdentityListResponse.of(await user_service_of(request).identities_of(user_id))


@router.delete("/{user_id}/identities/{identity_id}", status_code=204)
async def delete_user_identity(request: Request, user_id: str, identity_id: str, admin: RequireAdminDep) -> None:
    """Unlink a user's provider account (admin only)."""
    await user_service_of(request).unlink(user_id, identity_id, actor_of(request, admin))


# --- Admin-only endpoints ---


@router.get("", response_model=UserListResponse)
async def list_users(request: Request, _admin: RequireAdminDep) -> UserListResponse:
    """List all users, people and service accounts alike (admin only)."""
    return UserListResponse.of(await user_service_of(request).every())


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(request: Request, data: UserCreate, admin: RequireAdminDep) -> UserResponse:
    """Create a new user (admin only)."""
    return UserResponse.of(await user_service_of(request).create(data, actor_of(request, admin)))


@router.post("/invite", response_model=InviteResponse, status_code=201)
async def invite_user(request: Request, data: UserInviteCreate, admin: RequireAdminDep) -> InviteResponse:
    """Create a new user with an invite link (admin only)."""
    invitation = await user_service_of(request).invite(data, external_base_url(request), actor_of(request, admin))
    return InviteResponse.of(invitation)


@router.post("/service-accounts", response_model=UserResponse, status_code=201)
async def create_service_account(request: Request, data: ServiceAccountCreate, admin: RequireAdminDep) -> UserResponse:
    """Create a service account (admin only): a name and a role, with no email, password or invite link.

    It authenticates with the tokens an admin issues it, which needs a session; the account alone is no way in, so
    an admin's token may create one.
    """
    return UserResponse.of(await user_service_of(request).create_service_account(data, actor_of(request, admin)))


@router.post("/{user_id}/reinvite", response_model=InviteResponse)
async def reinvite_user(request: Request, user_id: str, admin: RequireAdminDep) -> InviteResponse:
    """Regenerate an invite link for a user who hasn't set their password (admin only)."""
    invitation = await user_service_of(request).reinvite(
        user_id, external_base_url(request), admin, actor_of(request, admin)
    )
    return InviteResponse.of(invitation)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(request: Request, user_id: str, _admin: RequireAdminDep) -> UserResponse:
    """Get a user by ID (admin only)."""
    return UserResponse.of(await user_service_of(request).get(user_id))


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(request: Request, user_id: str, data: UserUpdate, admin: RequireAdminDep) -> UserResponse:
    """Update a user (admin only). Setting a password needs a session, as for /me; a service account takes neither a
    password nor an email, and no user changes kind."""
    return UserResponse.of(await user_service_of(request).update(user_id, data, admin, actor_of(request, admin)))


@router.delete("/{user_id}", status_code=204)
async def delete_user(request: Request, user_id: str, admin: RequireAdminDep) -> None:
    """Delete a user (admin only). Cannot delete yourself."""
    await user_service_of(request).delete(user_id, admin, actor_of(request, admin))
