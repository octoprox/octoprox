# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sign-in through an identity provider, and the admin's provider settings.

The public router is what the login page and the browser's round trip
through the provider use; nothing on it takes a bearer token. The admin
router is where the OAuth clients are set up.

A completed sign-in matches the provider's verified email against the user
table; nobody self-registers. The first match links the provider account
to the user, and later sign-ins find the link, so a changed email at the
provider does not create a stranger.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.auth import RequireAdminDep
from api.core.config import settings
from api.db.auth_repository import AuthIdentityRepository, AuthProviderRepository
from api.db.redis import SSO_STATE_TTL_SECONDS
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.auth import (
    MICROSOFT_MULTI_TENANT_ALIASES,
    PROVIDER_LABELS,
    AuthIdentity,
    AuthProviderConfig,
    AuthProviderDoc,
    AuthProviderKind,
    AuthProviderListResponse,
    AuthProviderResponse,
    PublicSsoProvider,
    PublicSsoProvidersResponse,
    SsoExchangeRequest,
)
from api.models.user import User
from api.routes.auth import LoginResponse, issue_session_token
from api.routes.common import external_base_url, sso_flow_of
from api.sso import Identity, SsoError

logger = structlog.get_logger()

public_router = APIRouter(prefix="/auth/sso")
admin_router = APIRouter(prefix="/sso/providers")

DbDep = Annotated[AsyncSession, Depends(get_db)]

# Where the browser lands after the provider: the SPA route that redeems the
# code, or the login page with a reason.
COMPLETE_PATH = "/sso/complete"
ERROR_PARAM = "sso_error"
# The state of the login this browser started, so the callback can tell that
# the browser coming back is the one that left. Without it, anyone who
# completes the provider step with their own account could hand the callback
# URL to someone else and sign that person into the attacker's account
# (login CSRF). The Redis state alone cannot tell the two browsers apart.
STATE_COOKIE = "octoprox_sso_state"
STATE_COOKIE_PATH = "/api/v1/auth/sso"


def redirect_uri_for(request: Request, kind: AuthProviderKind) -> str:
    """The callback URL to register with the provider, built from the install's public URL."""
    return f"{external_base_url(request)}/api/v1/auth/sso/{kind.value}/callback"


def _to_login(request: Request, message: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{external_base_url(request)}/?{ERROR_PARAM}={quote(message)}", status_code=303
    )
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


def _is_https(request: Request) -> bool:
    return external_base_url(request).startswith("https://")


async def _enabled_provider(session: AsyncSession, kind: AuthProviderKind) -> AuthProviderConfig | None:
    """The provider's client if a login through it may start; None when it is off or incomplete."""
    config = await AuthProviderRepository(session).get(kind)
    if config is None or not config.enabled or not config.configured:
        return None
    return config


# --- public: the login page and the round trip ---------------------------------------


@public_router.get("/providers", response_model=PublicSsoProvidersResponse)
async def list_enabled_providers(session: DbDep) -> PublicSsoProvidersResponse:
    """The sign-in buttons to show. Public: the login page has no token yet."""
    enabled = await AuthProviderRepository(session).get_enabled()
    return PublicSsoProvidersResponse(
        providers=[
            PublicSsoProvider(kind=c.kind, label=PROVIDER_LABELS[c.kind]) for c in enabled if c.configured
        ]
    )


@public_router.get("/{kind}/start")
async def start_sign_in(request: Request, kind: AuthProviderKind, session: DbDep) -> RedirectResponse:
    """Send the browser to the provider, or back to the login page with a reason."""
    config = await _enabled_provider(session, kind)
    if config is None:
        # A top-level navigation: a JSON 404 would be a dead end for the person.
        return _to_login(request, f"{PROVIDER_LABELS[kind]} sign-in is not enabled")
    try:
        url, state = await sso_flow_of(request).start(config, redirect_uri_for(request, kind))
    except SsoError as exc:
        logger.warning("Sign-in could not start", provider=kind.value, reason=exc.detail)
        return _to_login(request, exc.message)
    response = RedirectResponse(url, status_code=302)
    # Lax: sent on the top-level navigation the provider redirects back with,
    # and on nothing a third-party page can trigger.
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=SSO_STATE_TTL_SECONDS,
        path=STATE_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
    return response


@public_router.get("/{kind}/callback")
async def finish_sign_in(
    request: Request,
    kind: AuthProviderKind,
    session: DbDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """The provider's answer. Ends in the SPA with a one-time code, or on the login page with a reason."""
    label = PROVIDER_LABELS[kind]
    if error:
        logger.warning("Sign-in refused by provider", provider=kind.value, error=error, description=error_description)
        message = "You cancelled the sign-in" if error == "access_denied" else f"{label} refused the sign-in"
        return _to_login(request, message)
    if not code or not state:
        return _to_login(request, f"{label} did not complete the sign-in")

    config = await _enabled_provider(session, kind)
    if config is None:
        return _to_login(request, f"{label} sign-in is not enabled")

    try:
        identity = await sso_flow_of(request).complete(config, redirect_uri_for(request, kind), state, code)
        if request.cookies.get(STATE_COOKIE) != state:
            # The state was genuine (and is now spent), but this browser did not start it.
            raise SsoError(
                "This sign-in attempt did not start in this browser. Please try again.",
                "sso state cookie missing or for another login",
            )
        user = await _resolve_user(session, identity)
    except SsoError as exc:
        logger.warning("Sign-in failed", provider=kind.value, reason=exc.detail)
        return _to_login(request, exc.message)
    except Exception:
        # A top-level navigation: the person needs the login page, not a JSON 500.
        logger.exception("Sign-in failed unexpectedly", provider=kind.value)
        return _to_login(request, f"{label} sign-in could not be completed. Please try again.")

    token = issue_session_token(user)
    await UserRepository(session).touch_last_login(user.id)
    login_code = await sso_flow_of(request).issue_login_code(token)
    logger.info("User signed in", username=user.username, role=user.role.value, provider=kind.value)
    response = RedirectResponse(f"{external_base_url(request)}{COMPLETE_PATH}?code={login_code}", status_code=303)
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


async def _resolve_user(session: AsyncSession, identity: Identity) -> User:
    """The user this identity signs in as, linking on first sight. Raises SsoError otherwise."""
    identities = AuthIdentityRepository(session)
    users = UserRepository(session)

    linked = await identities.get_by_subject(identity.provider, identity.subject)
    if linked is not None:
        user = await users.get_by_id(linked.user_id)
        if user is None:
            raise SsoError("Your account no longer exists", f"identity {linked.id} points at a missing user")
        if not user.is_active:
            raise SsoError("Your account is disabled", f"inactive user {user.username} via {identity.provider.value}")
        await identities.touch_last_login(linked.id, identity.email)
        return user

    # Both sides are lowercase: the identity by api.sso.identity, the user
    # table by every write path and migration 038.
    user = await users.get_by_email(identity.email)
    if user is None:
        raise SsoError(
            f"No Octoprox account has the email {identity.email}. Ask an admin to add you.",
            f"{identity.provider.value} {identity.subject} ({identity.email}) matches no user",
        )
    if not user.is_active:
        raise SsoError("Your account is disabled", f"inactive user {user.username} via {identity.provider.value}")
    other = await identities.get_for_user(user.id, identity.provider)
    if other is not None:
        raise SsoError(
            f"Your Octoprox account is already linked to a different {identity.label} account",
            f"user {user.username} has {identity.provider.value} subject {other.subject}, got {identity.subject}",
        )
    await identities.create(
        AuthIdentity(
            user_id=user.id,
            provider=identity.provider,
            subject=identity.subject,
            email=identity.email,
            last_login_at=None,
        )
    )
    logger.info("Sign-in provider linked", username=user.username, provider=identity.provider.value)
    return user


@public_router.post("/exchange", response_model=LoginResponse)
async def exchange_login_code(request: Request, data: SsoExchangeRequest) -> LoginResponse:
    """Trade the one-time code from the callback for the bearer token it parked."""
    token = await sso_flow_of(request).redeem_login_code(data.code)
    if token is None:
        raise HTTPException(status_code=400, detail="This sign-in code has expired or was already used")
    return LoginResponse(access_token=token, expires_in=settings.jwt_expiry_hours * 3600)


# --- admin: the OAuth clients -------------------------------------------------------


def _to_response(request: Request, config: AuthProviderConfig) -> AuthProviderResponse:
    return AuthProviderResponse(
        kind=config.kind,
        label=PROVIDER_LABELS[config.kind],
        enabled=config.enabled,
        client_id=config.client_id,
        has_client_secret=bool(config.client_secret),
        tenant_id=config.tenant_id,
        redirect_uri=redirect_uri_for(request, config.kind),
        configured=config.configured,
        updated_by=config.updated_by,
        updated_at=config.updated_at,
    )


@admin_router.get("", response_model=AuthProviderListResponse)
async def list_providers(request: Request, session: DbDep, _admin: RequireAdminDep) -> AuthProviderListResponse:
    """Every provider Octoprox knows, set up or not, with the redirect URI to register."""
    stored = await AuthProviderRepository(session).get_all()
    return AuthProviderListResponse(
        providers=[_to_response(request, stored.get(kind) or AuthProviderConfig(kind=kind)) for kind in AuthProviderKind]
    )


@admin_router.put("/{kind}", response_model=AuthProviderResponse)
async def save_provider(
    request: Request, kind: AuthProviderKind, doc: AuthProviderDoc, session: DbDep, admin: RequireAdminDep
) -> AuthProviderResponse:
    """Replace a provider's client. A null secret keeps the stored one."""
    repo = AuthProviderRepository(session)
    current = await repo.get(kind) or AuthProviderConfig(kind=kind)
    secret = current.client_secret if doc.client_secret is None else doc.client_secret
    updated = AuthProviderConfig(
        kind=kind,
        enabled=doc.enabled,
        client_id=doc.client_id,
        client_secret=secret,
        tenant_id=doc.tenant_id if kind == AuthProviderKind.MICROSOFT else "",
        updated_by=admin.username,
    )
    if kind == AuthProviderKind.MICROSOFT and updated.tenant_id.lower() in MICROSOFT_MULTI_TENANT_ALIASES:
        raise HTTPException(
            status_code=400,
            detail="Name the tenant whose accounts may sign in; 'common', 'organizations' and 'consumers' would admit any Microsoft account",
        )
    if updated.enabled and not updated.configured:
        missing = "a tenant ID" if updated.needs_tenant and not updated.tenant_id else "a client ID and secret"
        raise HTTPException(status_code=400, detail=f"{PROVIDER_LABELS[kind]} sign-in needs {missing} before it can be enabled")
    saved = await repo.save(updated)
    logger.info("Sign-in provider saved", provider=kind.value, enabled=saved.enabled, by=admin.username)
    return _to_response(request, saved)
