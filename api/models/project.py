# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project model definitions for multi-tenancy."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.core import utc_now
from api.geo.models import (
    MAX_CITY_RADIUS_KM,
    ConflictRule,
    ContradictedAction,
    LocationRules,
    SourcePolicy,
    UnknownAction,
)


class MitmMode(StrEnum):
    """TLS MITM interception modes."""

    OFF = "off"
    PLAIN = "plain"
    MATCH_UA = "match_ua"
    OVERRIDE_UA = "override_ua"


class MitmEngine(StrEnum):
    """TLS engines for browser fingerprint impersonation."""

    CURL_CFFI = "curl_cffi"
    RNET = "rnet"


class MitmBrowser(StrEnum):
    """Browser profiles for TLS fingerprint impersonation."""

    CHROME = "chrome"
    FIREFOX = "firefox"
    SAFARI = "safari"
    EDGE = "edge"
    RANDOM = "random"


def mitm_fields(
    mode: MitmMode, engine: MitmEngine | None, browser: MitmBrowser | None
) -> tuple[MitmEngine | None, MitmBrowser | None]:
    """The engine and browser a TLS interception mode keeps, the ones it does not use cleared.

    The one rule for a project's interception fields, applied on create and on
    every edit; raises ValueError when the mode needs one that is missing.
    """
    if mode in (MitmMode.OFF, MitmMode.PLAIN):
        return None, None
    if engine is None:
        raise ValueError(f"tls_mitm_engine is required when tls_mitm_mode is '{mode.value}'")
    if mode == MitmMode.MATCH_UA:
        return engine, None
    if browser is None:
        raise ValueError(f"tls_mitm_browser is required when tls_mitm_mode is '{mode.value}'")
    return engine, browser


# The project fields attribution judges a proxy by: what a contradicted
# vendor claim and an exit nothing places do, when a vendor counts as
# contradicted, and how far from a claimed city an answer may be. A change
# to any of them re-judges the project's proxies. Preflight is not one: it
# decides whether sessions are checked before use, not how a row is judged.
JUDGEMENT_FIELDS = (
    "location_contradicted",
    "location_unknown",
    "location_conflict_rule",
    "location_city_radius_km",
)


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

    # IP attribution: what a contradicted vendor claim and an exit nothing
    # independent places do to a proxy of this project, and whether a
    # session's exit is verified before its first request when the client
    # named a place, or always on a dynamic gateway (see docs/location-routing.md).
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    # When a vendor counts as contradicted. None inherits the install default (GeoSettings).
    location_conflict_rule: ConflictRule | None = None
    # How far from a claimed city an answer may be and still confirm it, in km;
    # 0 compares names only. None inherits the install default (GeoSettings).
    location_city_radius_km: float | None = Field(default=None, ge=0, le=MAX_CITY_RADIUS_KM)

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def location_rules(self) -> LocationRules:
        return LocationRules(self.location_contradicted, self.location_unknown)

    @property
    def judgement(self) -> tuple[object, ...]:
        """The values of the fields attribution judges the project's proxies by (``JUDGEMENT_FIELDS``)."""
        return tuple(getattr(self, field) for field in JUDGEMENT_FIELDS)

    def mitm_checked(self) -> "Project":
        """This project with the TLS interception fields its mode does not use cleared; ValueError when one it needs is missing.

        The rule ``ProjectCreate`` applies, for an edit, which can change the mode alone and leave the engine and browser as stored.
        """
        engine, browser = mitm_fields(self.tls_mitm_mode, self.tls_mitm_engine, self.tls_mitm_browser)
        return self.model_copy(update={"tls_mitm_engine": engine, "tls_mitm_browser": browser})

    def source_policy(self, default: SourcePolicy) -> SourcePolicy:
        """This project's source policy: its own conflict rule and city radius on top of the install default, plus its location decisions."""
        return SourcePolicy(
            conflict_rule=self.location_conflict_rule or default.conflict_rule,
            contradicted=self.location_contradicted,
            unknown=self.location_unknown,
            city_radius_km=default.city_radius_km if self.location_city_radius_km is None else self.location_city_radius_km,
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
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = Field(default=None, ge=0, le=MAX_CITY_RADIUS_KM)

    @model_validator(mode="after")
    def validate_mitm_fields(self) -> "ProjectCreate":
        """Clear the TLS interception fields the mode does not use; refuse one it needs missing."""
        self.tls_mitm_engine, self.tls_mitm_browser = mitm_fields(
            self.tls_mitm_mode, self.tls_mitm_engine, self.tls_mitm_browser
        )
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
    location_contradicted: ContradictedAction | None = None
    location_unknown: UnknownAction | None = None
    location_preflight: bool | None = None
    # "" clears the override (inherit the install default).
    location_conflict_rule: ConflictRule | Literal[""] | None = None
    # "" clears the override (inherit the install default).
    location_city_radius_km: Annotated[float, Field(ge=0, le=MAX_CITY_RADIUS_KM)] | Literal[""] | None = None

    def changes(self) -> dict[str, object]:
        """The fields this edit sets, with the empty strings that clear an override turned into None."""
        changes: dict[str, object] = self.model_dump(exclude_none=True)
        for field in ("location_conflict_rule", "location_city_radius_km"):
            if changes.get(field) == "":
                changes[field] = None
        return changes


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
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = None
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
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = None
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0
    created_at: datetime

