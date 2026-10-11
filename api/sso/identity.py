# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""From a provider's claims to the identity Octoprox matches a user on.

The rule every provider has to satisfy before an email counts: the
provider itself must vouch for it. An email the account holder merely typed
would let anyone sign in as anyone, so an unverified one is rejected here,
not matched loosely downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.models.auth import PROVIDER_LABELS, AuthProviderKind


class SsoError(Exception):
    """A sign-in that cannot complete, with a message fit for the login page."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # For the log, never for the browser.
        self.detail = detail or message


@dataclass(frozen=True)
class Identity:
    """Who the provider says is at the keyboard."""

    provider: AuthProviderKind
    # The provider's stable id for the account. Never an email: those change.
    subject: str
    # Verified by the provider, lowercased. Required: it is what a user is matched on.
    email: str
    name: str = ""

    @property
    def label(self) -> str:
        return PROVIDER_LABELS[self.provider]


def _lower_email(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def google_identity(claims: dict[str, Any]) -> Identity:
    """From a verified Google ID token's claims.

    Google sets ``email_verified`` for Workspace accounts and for Gmail
    addresses, and leaves it false for an account registered with an email
    Google has not confirmed; only the former may match a user.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise SsoError("Google did not identify the account", "google id token without sub")
    email = _lower_email(claims.get("email"))
    if not email or claims.get("email_verified") is not True:
        raise SsoError(
            "Your Google account has no verified email address",
            f"google sub {subject}: email missing or unverified",
        )
    return Identity(AuthProviderKind.GOOGLE, subject, email, str(claims.get("name") or ""))


def microsoft_identity(claims: dict[str, Any], expected_issuer: str) -> Identity:
    """From a verified Entra ID token's claims, pinned to the install's tenant.

    The issuer embeds the tenant id, so a token from any other tenant (a
    personal account, another company) fails here even if the signature was
    good. Within the tenant the email and user principal name are set by the
    tenant's own admins, which is what makes them trustworthy.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise SsoError("Microsoft did not identify the account", "entra id token without sub")
    issuer = claims.get("iss")
    if issuer != expected_issuer:
        raise SsoError(
            "Your Microsoft account is not in this organisation's tenant",
            f"entra issuer {issuer!r} is not {expected_issuer!r}",
        )
    email = _lower_email(claims.get("email"))
    if not email:
        upn = _lower_email(claims.get("preferred_username"))
        if "@" in upn:
            email = upn
    if not email:
        raise SsoError(
            "Your Microsoft account has no email address",
            f"entra sub {subject}: no email or user principal name",
        )
    return Identity(AuthProviderKind.MICROSOFT, subject, email, str(claims.get("name") or ""))


def github_identity(user: dict[str, Any], emails: list[dict[str, Any]]) -> Identity:
    """From GitHub's ``/user`` and ``/user/emails`` responses.

    GitHub is OAuth 2.0 without OpenID Connect: no ID token, and the email
    on the profile is whatever the person chose to show. The emails endpoint
    says which addresses GitHub has verified; the primary verified one is
    the account's.
    """
    raw_id = user.get("id")
    if raw_id is None:
        raise SsoError("GitHub did not identify the account", "github /user without id")
    subject = str(raw_id)
    verified = [e for e in emails if e.get("verified") is True and isinstance(e.get("email"), str)]
    primary = next((e for e in verified if e.get("primary") is True), None) or (verified[0] if verified else None)
    if primary is None:
        raise SsoError(
            "Your GitHub account has no verified email address",
            f"github user {subject}: no verified email",
        )
    return Identity(AuthProviderKind.GITHUB, subject, _lower_email(primary["email"]), str(user.get("name") or ""))
