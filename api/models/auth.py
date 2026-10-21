# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Models for sign-in providers, linked identities and personal API tokens.

Sign-in through Google, Microsoft or GitHub replaces the password for a
person the admin has already added: the provider says who is at the
keyboard, and the user row with that verified email is who signs in. Nobody
self-registers. See docs/sso.md.
"""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now


class AuthProviderKind(StrEnum):
    """The sign-in providers an admin can set up."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    GITHUB = "github"


PROVIDER_LABELS: dict[AuthProviderKind, str] = {
    AuthProviderKind.GOOGLE: "Google",
    AuthProviderKind.MICROSOFT: "Microsoft",
    AuthProviderKind.GITHUB: "GitHub",
}

# Entra's multi-tenant pseudo tenants. Any Microsoft account in the world can
# sign in through them, and the email claim is then whatever the account
# holder typed, so an install must name the one tenant whose accounts count.
MICROSOFT_MULTI_TENANT_ALIASES = frozenset({"common", "organizations", "consumers"})


class AuthProviderConfig(BaseModel):
    """What is stored for one provider: the OAuth client and whether it is on."""

    kind: AuthProviderKind
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ""
    updated_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def needs_tenant(self) -> bool:
        return self.kind == AuthProviderKind.MICROSOFT

    @property
    def configured(self) -> bool:
        """Whether a login could be started: client set, and a tenant where one is needed."""
        if not self.client_id or not self.client_secret:
            return False
        return bool(self.tenant_id) if self.needs_tenant else True


class AuthProviderDoc(BaseModel):
    """What the admin saves for a provider. A null secret keeps the stored one."""

    enabled: bool = False
    # Bounded to what the columns hold, so a stray paste is a 422, not a 500.
    client_id: str = Field(default="", max_length=255)
    client_secret: str | None = None
    tenant_id: str = Field(default="", max_length=255)

    @field_validator("client_id", "tenant_id")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("client_secret")
    @classmethod
    def strip_secret(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None


class AuthProviderResponse(BaseModel):
    """A provider as the admin page shows it. The secret never leaves the server."""

    kind: AuthProviderKind
    label: str
    enabled: bool
    client_id: str
    has_client_secret: bool
    tenant_id: str
    # What to register with the provider as the allowed redirect URI.
    redirect_uri: str
    configured: bool
    updated_by: str | None
    updated_at: datetime


class AuthProviderListResponse(BaseModel):
    providers: list[AuthProviderResponse]


class PublicSsoProvider(BaseModel):
    """What the login page needs to draw a button."""

    kind: AuthProviderKind
    label: str


class PublicSsoProvidersResponse(BaseModel):
    providers: list[PublicSsoProvider]


class SsoExchangeRequest(BaseModel):
    """The one-time code the callback handed the browser, traded for a bearer token."""

    code: str = Field(min_length=1, max_length=128)


class AuthIdentity(BaseModel):
    """A provider account linked to a user."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    provider: AuthProviderKind
    subject: str
    email: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    last_login_at: datetime | None = None


class AuthIdentityResponse(BaseModel):
    id: str
    provider: AuthProviderKind
    label: str
    email: str
    created_at: datetime
    last_login_at: datetime | None


class AuthIdentityListResponse(BaseModel):
    identities: list[AuthIdentityResponse]


class ApiToken(BaseModel):
    """A personal access token as stored: hash and prefix, never the secret."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str
    name: str
    token_hash: str
    token_prefix: str
    created_by: str | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utc_now()


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # Days until the token stops working; null means it never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v


class ApiTokenResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_by: str | None
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    expired: bool


class ApiTokenCreatedResponse(BaseModel):
    """The one response that carries the secret. It cannot be fetched again."""

    token: str
    api_token: ApiTokenResponse


class ApiTokenListResponse(BaseModel):
    tokens: list[ApiTokenResponse]
