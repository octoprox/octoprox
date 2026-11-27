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

import re
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
from api.routes.common import (
    actor_of,
    audit_log_of,
    external_base_url,
    sso_flow_of,
    sso_provider_service_of,
)
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
# What an OAuth error code looks like: a refusal's reason keeps the callback's error only when it is one.
OAUTH_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


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


class _UserRefusedError(SsoError):
    """A sign-in refused to the user the identity matched: disabled, or linked to another account at the provider."""

    def __init__(self, user: User, message: str, detail: str) -> None:
        super().__init__(message, detail)
        self.user = user


async def _turn_away(
    request: Request,
    session: AsyncSession,
    kind: AuthProviderKind,
    started: bool,
    message: str,
    reason: str,
    user: User | None = None,
) -> RedirectResponse:
    """The login page with ``message``, the audit log told ``reason`` first when ``started``.

    A refusal is recorded only for a state this install issued for that provider and has not used, valid 10 minutes,
    which anyone can obtain by starting a sign-in.
    """
    if started:
        # The request's connection goes back to the pool first: the hook may open a session of its own.
        await session.rollback()
        await audit_log_of(request).sign_in_failed(request, "sso", reason, user=user, provider=kind.value)
    return _to_login(request, message)


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
    # Read before anything below spends the state.
    started = await sso_flow_of(request).pending(state, kind) if state else False
    if error:
        logger.warning("Sign-in refused by provider", provider=kind.value, error=error, description=error_description)
        if error == "access_denied":
            return await _turn_away(request, session, kind, started, "You cancelled the sign-in", "cancelled")
        reason = f"refused by the provider: {error}" if OAUTH_ERROR_CODE.fullmatch(error) else "refused by the provider"
        return await _turn_away(request, session, kind, started, f"{label} refused the sign-in", reason)
    if not code or not state:
        return _to_login(request, f"{label} did not complete the sign-in")

    config = await _enabled_provider(session, kind)
    if config is None:
        return await _turn_away(
            request, session, kind, started, f"{label} sign-in is not enabled", "provider not enabled"
        )

    try:
        identity = await sso_flow_of(request).complete(config, redirect_uri_for(request, kind), state, code)
        if request.cookies.get(STATE_COOKIE) != state:
            # The state was genuine (and is now spent), but this browser did not start it.
            raise SsoError(
                "This sign-in attempt did not start in this browser. Please try again.",
                "sso state cookie missing or for another login",
            )
        user, linked = await _resolve_user(session, identity)
    except SsoError as exc:
        logger.warning("Sign-in failed", provider=kind.value, reason=exc.detail)
        refused = exc.user if isinstance(exc, _UserRefusedError) else None
        return await _turn_away(request, session, kind, started, exc.message, exc.detail, refused)
    except Exception:
        # A top-level navigation: the person needs the login page, not a JSON 500.
        logger.exception("Sign-in failed unexpectedly", provider=kind.value)
        return await _turn_away(
            request,
            session,
            kind,
            started,
            f"{label} sign-in could not be completed. Please try again.",
            "unexpected error",
        )

    token = issue_session_token(user)
    await UserRepository(session).touch_last_login(user.id)
    login_code = await sso_flow_of(request).issue_login_code(token)
    # Committed before the hook, which may open a session of its own.
    await session.commit()
    await audit_log_of(request).signed_in(request, user, "sso", provider=kind.value, linked=linked)
    logger.info("User signed in", username=user.username, role=user.role.value, provider=kind.value)
    response = RedirectResponse(f"{external_base_url(request)}{COMPLETE_PATH}?code={login_code}", status_code=303)
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


async def _resolve_user(session: AsyncSession, identity: Identity) -> tuple[User, bool]:
    """The user this identity signs in as, linking it on first sight, and whether this sign-in linked it.

    Raises SsoError otherwise.
    """
    identities = AuthIdentityRepository(session)
    users = UserRepository(session)

    linked = await identities.get_by_subject(identity.provider, identity.subject)
    if linked is not None:
        user = await users.get_by_id(linked.user_id)
        if user is None:
            raise SsoError("Your account no longer exists", f"identity {linked.id} points at a missing user")
        if not user.is_active:
            raise _UserRefusedError(
                user, "Your account is disabled", f"inactive user {user.username} via {identity.provider.value}"
            )
        await identities.touch_last_login(linked.id, identity.email)
        return user, False

    # Both sides are lowercase: the identity by api.sso.identity, the user
    # table by every write path and migration 038.
    user = await users.get_by_email(identity.email)
    if user is None:
        raise SsoError(
            f"No Octoprox account has the email {identity.email}. Ask an admin to add you.",
            f"{identity.provider.value} {identity.subject} ({identity.email}) matches no user",
        )
    if not user.is_active:
        raise _UserRefusedError(
            user, "Your account is disabled", f"inactive user {user.username} via {identity.provider.value}"
        )
    other = await identities.get_for_user(user.id, identity.provider)
    if other is not None:
        raise _UserRefusedError(
            user,
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
    return user, True


@public_router.post("/exchange", response_model=LoginResponse)
async def exchange_login_code(request: Request, data: SsoExchangeRequest) -> LoginResponse:
    """Trade the one-time code from the callback for the bearer token it parked."""
    token = await sso_flow_of(request).redeem_login_code(data.code)
    if token is None:
        raise HTTPException(status_code=400, detail="This sign-in code has expired or was already used")
    return LoginResponse(access_token=token, expires_in=settings.jwt_expiry_hours * 3600)


# --- admin: the OAuth clients -------------------------------------------------------


@admin_router.get("", response_model=AuthProviderListResponse)
async def list_providers(request: Request, _admin: RequireAdminDep) -> AuthProviderListResponse:
    """Every provider Octoprox knows, set up or not, with the redirect URI to register."""
    configs = await sso_provider_service_of(request).every()
    return AuthProviderListResponse(
        providers=[AuthProviderResponse.of(config, redirect_uri_for(request, config.kind)) for config in configs]
    )


@admin_router.put("/{kind}", response_model=AuthProviderResponse)
async def save_provider(
    request: Request, kind: AuthProviderKind, doc: AuthProviderDoc, admin: RequireAdminDep
) -> AuthProviderResponse:
    """Replace a provider's client. A null secret keeps the stored one."""
    saved = await sso_provider_service_of(request).save(kind, doc, actor_of(request, admin))
    return AuthProviderResponse.of(saved, redirect_uri_for(request, kind))
