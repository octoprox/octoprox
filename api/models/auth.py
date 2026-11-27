# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Models for sign-in providers, linked identities and personal API tokens.

Sign-in through Google, Microsoft or GitHub replaces the password for a
person the admin has already added: the provider says who is at the
keyboard, and the user row with that verified email is who signs in. Nobody
self-registers. See docs/sso.md.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now
from api.models.audit import Audited, AuditEntityType


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


class AuthProviderConfig(Audited):
    """What is stored for one provider: the OAuth client and whether it is on."""

    audit_type: ClassVar[AuditEntityType] = AuditEntityType.SIGN_IN_PROVIDER
    # The kind is the entry's entity id.
    audit_ignored: ClassVar[frozenset[str]] = Audited.audit_ignored | {"kind", "updated_by"}
    audit_masked: ClassVar[frozenset[str]] = frozenset({"client_secret"})

    kind: AuthProviderKind
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ""
    updated_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    def audit_id(self) -> str:
        return self.kind.value

    def audit_name(self) -> str:
        return PROVIDER_LABELS[self.kind]

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

    @classmethod
    def of(cls, config: AuthProviderConfig, redirect_uri: str) -> "AuthProviderResponse":
        """A provider's client as the admin page shows it, with the callback URL to register."""
        return cls(
            kind=config.kind,
            label=PROVIDER_LABELS[config.kind],
            enabled=config.enabled,
            client_id=config.client_id,
            has_client_secret=bool(config.client_secret),
            tenant_id=config.tenant_id,
            redirect_uri=redirect_uri,
            configured=config.configured,
            updated_by=config.updated_by,
            updated_at=config.updated_at,
        )


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

    @classmethod
    def of(cls, identity: AuthIdentity) -> "AuthIdentityResponse":
        """A linked provider account, labelled as the sign-in buttons are."""
        return cls(
            id=identity.id,
            provider=identity.provider,
            label=PROVIDER_LABELS[identity.provider],
            email=identity.email,
            created_at=identity.created_at,
            last_login_at=identity.last_login_at,
        )


class AuthIdentityListResponse(BaseModel):
    identities: list[AuthIdentityResponse]

    @classmethod
    def of(cls, identities: list[AuthIdentity]) -> "AuthIdentityListResponse":
        """The linked provider accounts, in the order given."""
        return cls(identities=[AuthIdentityResponse.of(identity) for identity in identities])


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


class TokenUsage(BaseModel):
    """How many tokens a user holds, expired ones included, and the latest use of any of them."""

    count: int = 0
    last_used_at: datetime | None = None


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


@dataclass(frozen=True)
class IssuedToken:
    """A token just issued, with its secret: the only time the secret exists outside the caller's hands."""

    token: ApiToken
    secret: str


class ApiTokenResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_by: str | None
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    expired: bool

    @classmethod
    def of(cls, token: ApiToken) -> "ApiTokenResponse":
        """A token as listed: its prefix and stamps, never the secret or its hash."""
        return cls(
            id=token.id,
            name=token.name,
            token_prefix=token.token_prefix,
            created_by=token.created_by,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
            expired=token.expired,
        )


class ApiTokenCreatedResponse(BaseModel):
    """The one response that carries the secret. It cannot be fetched again."""

    token: str
    api_token: ApiTokenResponse

    @classmethod
    def of(cls, issued: IssuedToken) -> "ApiTokenCreatedResponse":
        """A token just issued, with its secret."""
        return cls(token=issued.secret, api_token=ApiTokenResponse.of(issued.token))


class ApiTokenListResponse(BaseModel):
    tokens: list[ApiTokenResponse]

    @classmethod
    def of(cls, tokens: list[ApiToken]) -> "ApiTokenListResponse":
        """The tokens as listed, in the order given."""
        return cls(tokens=[ApiTokenResponse.of(token) for token in tokens])
