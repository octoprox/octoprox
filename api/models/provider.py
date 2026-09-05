# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Domain and API models for provider descriptors managed through the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from api.core import utc_now
from api.models.cloud_options import CountryOption
from api.providers.sdk.descriptor import FieldSpec, OptionSpec, ProviderDescriptor

ProviderAuditAction = Literal["created", "updated", "deleted", "imported"]


class ProviderRecord(BaseModel):
    """A custom descriptor row as stored in Postgres."""

    id: str
    name: str
    spec: dict[str, Any]
    enabled: bool = True
    version: int = 1
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProviderAuditEntry(BaseModel):
    """One row of the provider audit log."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    provider_id: str
    action: ProviderAuditAction
    actor: str
    egress_hosts: list[str] = Field(default_factory=list)
    hosts_changed: bool = False
    spec: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


# --- API schemas ---------------------------------------------------------------------


class ProviderSummary(BaseModel):
    """Catalog entry the UI uses to render pickers and forms."""

    id: str
    name: str
    description: str
    kind: Literal["code", "descriptor"]
    source: Literal["builtin", "file", "plugin", "custom"]
    editable: bool
    syncable: bool
    cloud: bool
    beta: bool = False
    logo: str | None = None
    docs_url: str | None = None
    credential_fields: list[FieldSpec]
    connector_fields: list[FieldSpec]
    proxy_type_field: str | None = None
    proxy_types: list[dict[str, str]] = Field(default_factory=list)
    egress_hosts: list[str] = Field(default_factory=list)
    gateway_hosts: list[str] = Field(default_factory=list)
    has_validation: bool = False
    credential_count: int = 0
    connector_count: int = 0
    version: int = 1
    updated_at: datetime | None = None


class ProviderDetail(ProviderSummary):
    """Summary plus the full descriptor (descriptor kinds only)."""

    spec: ProviderDescriptor | None = None
    origin: str = ""


class ProviderListResponse(BaseModel):
    total: int
    providers: list[ProviderSummary]
    presets: dict[str, list[OptionSpec]] = Field(default_factory=dict)
    countries: list[CountryOption] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    """Create a custom descriptor. ``confirmed_hosts`` must cover every egress host."""

    spec: dict[str, Any]
    confirmed_hosts: list[str] = Field(default_factory=list)
    enabled: bool = True


class ProviderUpdate(BaseModel):
    spec: dict[str, Any] | None = None
    confirmed_hosts: list[str] = Field(default_factory=list)
    enabled: bool | None = None


class ProviderValidateRequest(BaseModel):
    spec: dict[str, Any]


class ProviderValidateResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    spec: ProviderDescriptor | None = None
    egress_hosts: list[str] = Field(default_factory=list)
    gateway_hosts: list[str] = Field(default_factory=list)
    discovery_hosts: list[str] = Field(default_factory=list)
    yaml: str | None = None


class ProviderImportRequest(BaseModel):
    yaml: str
    confirmed_hosts: list[str] = Field(default_factory=list)
    replace: bool = False


class HostConfirmationRequired(BaseModel):
    """409 body: the client must show these hosts and resubmit with ``confirmed_hosts``."""

    detail: str
    egress_hosts: list[str]
    unconfirmed_hosts: list[str]


class ProviderTestRequest(BaseModel):
    action: Literal["validate", "options", "list_proxies"]
    credential_config: dict[str, Any] = Field(default_factory=dict)
    connector_config: dict[str, Any] = Field(default_factory=dict)
    option_name: str | None = None
    spec: dict[str, Any] | None = Field(
        default=None, description="Unsaved descriptor to test instead of the stored one"
    )


class ProviderTestResponse(BaseModel):
    ok: bool
    message: str
    result: Any = None
    traces: list[dict[str, Any]] = Field(default_factory=list)


class ProviderOptionsRequest(BaseModel):
    credential_id: str | None = None
    credential_config: dict[str, Any] | None = None
    connector_config: dict[str, Any] = Field(default_factory=dict)


class ProviderOptionsResponse(BaseModel):
    options: list[dict[str, Any]]
    cached: bool = False


class ProviderAuditResponse(BaseModel):
    total: int
    entries: list[ProviderAuditEntry]
