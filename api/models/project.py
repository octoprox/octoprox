# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project model definitions for multi-tenancy."""

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from api.core import utc_now
from api.geo.models import (
    ConflictRule,
    GeoSourceKind,
    LocationPolicy,
    PreflightMode,
    SourcePolicy,
    dedupe_sources,
)


class MitmMode(str, Enum):
    """TLS MITM interception modes."""

    OFF = "off"
    PLAIN = "plain"
    MATCH_UA = "match_ua"
    OVERRIDE_UA = "override_ua"


class MitmEngine(str, Enum):
    """TLS engines for browser fingerprint impersonation."""

    CURL_CFFI = "curl_cffi"
    RNET = "rnet"


class MitmBrowser(str, Enum):
    """Browser profiles for TLS fingerprint impersonation."""

    CHROME = "chrome"
    FIREFOX = "firefox"
    SAFARI = "safari"
    EDGE = "edge"
    RANDOM = "random"


def _normalize_sources(value: list[GeoSourceKind] | None) -> list[GeoSourceKind] | None:
    """Deduplicate in order; an empty list means "no override"."""
    return dedupe_sources(value) if value else None


class Project(BaseModel):
    """Represents a project for multi-tenancy."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""

    # Proxy authentication credentials (plain text as per requirements)
    username: str
    password: str

    # Project-level settings (override global defaults)
    routing_strategy: str = "round_robin"
    health_check_interval: int = 60  # seconds
    health_check_timeout: int = 30  # seconds
    connection_timeout: int = 30  # seconds
    max_retries: int = 3

    # TLS MITM interception
    tls_mitm_mode: MitmMode = MitmMode.OFF
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None

    # Metrics settings
    metrics_retention_days: int = 90  # 0 = keep forever

    # IP attribution: what a contradicted vendor location does to a proxy of
    # this project, and whether a session's exit is verified before its first
    # request is forwarded (see docs/ip-attribution.md).
    location_policy: LocationPolicy = LocationPolicy.OFF
    location_preflight: PreflightMode = PreflightMode.OFF
    # Which evidence decides a proxy's country and when a vendor counts as
    # contradicted. None inherits the install default (GeoSettings).
    location_sources: list[GeoSourceKind] | None = None
    location_conflict_rule: ConflictRule | None = None

    # Aggregate statistics (persists across proxy rotation)
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def merge_definition_from(self, other: "Project") -> None:
        """Adopt the Postgres-backed *definition* fields from ``other``.

        Used by ``ProxyManager.reload_project`` and ``full_reload`` to
        apply a fresh-from-DB ``Project`` to the existing cached instance
        without clobbering the per-request aggregate counters that live
        on this object in memory.

        Adding a new column to the projects table? Add the field here
        too. Adding a new runtime-only field? Leave it out.
        """
        self.name = other.name
        self.description = other.description
        self.username = other.username
        self.password = other.password
        self.routing_strategy = other.routing_strategy
        self.health_check_interval = other.health_check_interval
        self.health_check_timeout = other.health_check_timeout
        self.connection_timeout = other.connection_timeout
        self.max_retries = other.max_retries
        self.tls_mitm_mode = other.tls_mitm_mode
        self.tls_mitm_engine = other.tls_mitm_engine
        self.tls_mitm_browser = other.tls_mitm_browser
        self.metrics_retention_days = other.metrics_retention_days
        self.location_policy = other.location_policy
        self.location_preflight = other.location_preflight
        self.location_sources = other.location_sources
        self.location_conflict_rule = other.location_conflict_rule
        self.created_at = other.created_at
        self.updated_at = other.updated_at

    def source_policy(self, default: SourcePolicy) -> SourcePolicy:
        """This project's source policy: its own overrides on top of the install default."""
        if self.location_sources is None and self.location_conflict_rule is None:
            return default
        return SourcePolicy(
            sources=list(self.location_sources) if self.location_sources else list(default.sources),
            conflict_rule=self.location_conflict_rule or default.conflict_rule,
        )


class ProjectCreate(BaseModel):
    """Schema for creating a new project."""
    name: str
    description: str = ""
    username: str
    password: str
    routing_strategy: str = "round_robin"
    health_check_interval: int = 60
    health_check_timeout: int = 30
    connection_timeout: int = 30
    max_retries: int = 3
    tls_mitm_mode: MitmMode = MitmMode.OFF
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    metrics_retention_days: int = 90
    location_policy: LocationPolicy = LocationPolicy.OFF
    location_preflight: PreflightMode = PreflightMode.OFF
    location_sources: list[GeoSourceKind] | None = None
    location_conflict_rule: ConflictRule | None = None

    @field_validator("location_sources")
    @classmethod
    def _sources(cls, value: list[GeoSourceKind] | None) -> list[GeoSourceKind] | None:
        return _normalize_sources(value)

    @model_validator(mode="after")
    def validate_mitm_fields(self) -> "ProjectCreate":
        """Validate and clean MITM fields based on mode."""
        mode = self.tls_mitm_mode
        if mode in (MitmMode.OFF, MitmMode.PLAIN):
            self.tls_mitm_engine = None
            self.tls_mitm_browser = None
        elif mode == MitmMode.MATCH_UA:
            if self.tls_mitm_engine is None:
                msg = "tls_mitm_engine is required when tls_mitm_mode is 'match_ua'"
                raise ValueError(msg)
            self.tls_mitm_browser = None
        elif mode == MitmMode.OVERRIDE_UA:
            if self.tls_mitm_engine is None:
                msg = "tls_mitm_engine is required when tls_mitm_mode is 'override_ua'"
                raise ValueError(msg)
            if self.tls_mitm_browser is None:
                msg = "tls_mitm_browser is required when tls_mitm_mode is 'override_ua'"
                raise ValueError(msg)
        return self


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""
    name: str | None = None
    description: str | None = None
    username: str | None = None
    password: str | None = None
    routing_strategy: str | None = None
    health_check_interval: int | None = None
    health_check_timeout: int | None = None
    connection_timeout: int | None = None
    max_retries: int | None = None
    tls_mitm_mode: MitmMode | None = None
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    metrics_retention_days: int | None = None
    location_policy: LocationPolicy | None = None
    location_preflight: PreflightMode | None = None
    # An empty list clears the override (inherit the install default).
    location_sources: list[GeoSourceKind] | None = None
    # "" clears the override.
    location_conflict_rule: ConflictRule | Literal[""] | None = None

    @field_validator("location_sources")
    @classmethod
    def _sources(cls, value: list[GeoSourceKind] | None) -> list[GeoSourceKind] | None:
        if value == []:
            return []
        return _normalize_sources(value)


class ProjectResponse(BaseModel):
    """Schema for project API responses."""
    id: str
    name: str
    description: str
    username: str
    password: str
    routing_strategy: str
    health_check_interval: int
    health_check_timeout: int
    connection_timeout: int
    max_retries: int
    tls_mitm_mode: MitmMode
    tls_mitm_engine: MitmEngine | None
    tls_mitm_browser: MitmBrowser | None
    metrics_retention_days: int
    location_policy: LocationPolicy = LocationPolicy.OFF
    location_preflight: PreflightMode = PreflightMode.OFF
    location_sources: list[GeoSourceKind] | None = None
    location_conflict_rule: ConflictRule | None = None
    created_at: datetime
    updated_at: datetime
    # Aggregated stats (populated by API)
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0


class ProjectSummary(BaseModel):
    """Summary of a project for listing."""
    id: str
    name: str
    description: str
    username: str
    password: str
    routing_strategy: str
    tls_mitm_mode: MitmMode = MitmMode.OFF
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    location_policy: LocationPolicy = LocationPolicy.OFF
    location_preflight: PreflightMode = PreflightMode.OFF
    location_sources: list[GeoSourceKind] | None = None
    location_conflict_rule: ConflictRule | None = None
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0
    created_at: datetime

