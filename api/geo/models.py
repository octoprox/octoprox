# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Domain models for IP attribution.

Everything here is a plain value: the readers, resolver and service modules
produce and consume these, and the admin API serialises them as-is.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now


class GeoVendor(StrEnum):
    """Who built a database. Drives nothing but labels and download presets."""

    MAXMIND = "maxmind"
    DBIP = "dbip"
    IPINFO = "ipinfo"
    IP2LOCATION = "ip2location"
    OTHER = "other"


class GeoDatabaseKind(StrEnum):
    """What a database answers. A city database also answers country."""

    COUNTRY = "country"
    CITY = "city"
    ASN = "asn"
    ANONYMOUS = "anonymous"
    UNKNOWN = "unknown"


class GeoDatabaseFormat(StrEnum):
    MMDB = "mmdb"
    IP2LOCATION_BIN = "ip2location_bin"


class GeoDatabaseSource(StrEnum):
    """Where a database's file comes from."""

    UPLOAD = "upload"  # uploaded in the admin panel, stored in Postgres
    URL = "url"  # downloaded from the vendor on a schedule, stored in Postgres
    PATH = "path"  # operator-managed file named in the config file


class GeoSourceKind(StrEnum):
    """The three kinds of evidence about where an IP is."""

    DATABASE = "database"  # a local IP database
    VENDOR = "vendor"  # what the proxy vendor claims (list entry, known-IP list, geo slot)
    ENDPOINT = "endpoint"  # what an echo endpoint reported when requested through the proxy


class ConflictRule(StrEnum):
    """When a vendor claim counts as contradicted."""

    # Every independent source (databases, endpoint) agrees with each other and
    # all of them disagree with the vendor. Databases lag on residential ranges,
    # so one dissenting database alone is not evidence against the vendor.
    CONSENSUS = "consensus"
    # The winning independent answer disagrees with the vendor.
    FIRST = "first"


class LocationPolicy(StrEnum):
    """Per project: what a contradicted vendor claim does to a proxy."""

    OFF = "off"  # ignore conflicts
    WARN = "warn"  # keep routing, surface the flag
    STRICT = "strict"  # flagged proxies are not eligible for this project's requests


class PreflightMode(StrEnum):
    """Per project: verify a session's exit location before forwarding the first request."""

    OFF = "off"
    REPORT = "report"  # record the observation, forward anyway
    # On a mismatch, re-select another eligible proxy while no explicit sticky
    # session is bound yet; once one is bound, behave as reject.
    RETRY = "retry"
    REJECT = "reject"  # fail the request with 502 when the location does not match


class ObservationSource(StrEnum):
    """Which code path learned an IP."""

    DISCOVERY = "discovery"  # provider SDK port-mode discovery or vendor IP list
    GEO_LOOKUP = "geo_lookup"  # exit-location lookup of a static proxy
    HEALTH_CHECK = "health_check"
    PREFLIGHT = "preflight"
    MANUAL = "manual"  # the locate button


# An echo every caller can use: it returns the IP and nothing else, which is
# all attribution needs.
DEFAULT_ECHO_URL = "https://httpbin.org/ip"
DEFAULT_ECHO_IP_PATH = "origin"


def normalize_country(value: Any) -> str | None:
    """Upper-case two-letter code, or None for anything that is not one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return None
    return code


class IpLocation(BaseModel):
    """One database's normalised answer for an IP.

    Only ``country`` takes part in routing today. The rest is stored on the
    proxy so region- or city-level routing can be added without another
    lookup, and shown in the inspector.
    """

    country: str | None = None
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    organization: str | None = None
    # Anonymity flags from privacy or proxy databases. None means the database
    # does not know, False means it says the IP is clean.
    is_anonymous: bool | None = None
    is_hosting: bool | None = None
    is_vpn: bool | None = None
    is_public_proxy: bool | None = None
    is_tor: bool | None = None
    is_residential_proxy: bool | None = None

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: Any) -> str | None:
        return normalize_country(value)

    def merged_with(self, other: IpLocation) -> IpLocation:
        """Fill this record's unknown fields from ``other`` (a lower-priority database)."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if data.get(key) is None and value is not None:
                data[key] = value
        return IpLocation(**data)

    def is_empty(self) -> bool:
        return all(value is None for value in self.model_dump().values())


class LocationCandidate(BaseModel):
    """One source's opinion about an IP's country."""

    source: GeoSourceKind
    origin: str  # database id, "vendor", or the endpoint URL
    country: str | None = None
    location: IpLocation | None = None

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: Any) -> str | None:
        return normalize_country(value)


class Resolution(BaseModel):
    """What the resolver decided for one IP."""

    country: str | None = None
    source: GeoSourceKind | None = None
    origin: str | None = None
    # The vendor's claim is contradicted by independent evidence (see ConflictRule).
    conflict: bool = False
    # Independent sources disagree with each other; the answer is uncertain.
    disagreement: bool = False
    claimed_country: str | None = None
    location: IpLocation | None = None
    candidates: list[LocationCandidate] = Field(default_factory=list)

    def compact_candidates(self) -> list[dict[str, Any]]:
        """Short form stored in proxy metadata: no nested location records."""
        return [
            {"source": c.source.value, "origin": c.origin, "country": c.country}
            for c in self.candidates
        ]


def dedupe_sources(value: list[GeoSourceKind]) -> list[GeoSourceKind]:
    """Order-preserving dedupe of a source list; at least one source must remain."""
    seen: list[GeoSourceKind] = []
    for kind in value:
        if kind not in seen:
            seen.append(kind)
    if not seen:
        raise ValueError("at least one source is required")
    return seen


class SourcePolicy(BaseModel):
    """How the three kinds of evidence combine for one project.

    The install keeps a default (``GeoSettings.default_policy``); a project may
    override either field on its own row.
    """

    # Precedence when sources answer differently. The first kind with an answer wins.
    sources: list[GeoSourceKind] = Field(
        default_factory=lambda: [GeoSourceKind.DATABASE, GeoSourceKind.VENDOR, GeoSourceKind.ENDPOINT]
    )
    conflict_rule: ConflictRule = ConflictRule.CONSENSUS

    @field_validator("sources")
    @classmethod
    def _sources(cls, value: list[GeoSourceKind]) -> list[GeoSourceKind]:
        return dedupe_sources(value)


class GeoSettings(BaseModel):
    """Install-wide attribution settings, edited in the admin panel.

    One typed row (``geo_settings``); the config file's ``geo.defaults`` seeds
    it on a fresh install. Judgement (which source wins, when the vendor is
    contradicted) lives here only as the default projects inherit.
    """

    default_sources: list[GeoSourceKind] = Field(
        default_factory=lambda: [GeoSourceKind.DATABASE, GeoSourceKind.VENDOR, GeoSourceKind.ENDPOINT]
    )
    default_conflict_rule: ConflictRule = ConflictRule.CONSENSUS
    # Echo endpoint requested through proxies to learn their exit IP. Becomes
    # the default for health checks, discovery, exit lookups and preflight.
    echo_url: str = DEFAULT_ECHO_URL
    echo_ip_path: str = DEFAULT_ECHO_IP_PATH
    echo_country_path: str | None = None
    echo_timeout_seconds: float = Field(default=15.0, gt=0)
    # Attribute the IP the health checker sees when the check URL is the echo URL.
    health_check_attribution: bool = True
    # Preflight: one echo request per proxy per this window, for projects with preflight on.
    preflight_session_ttl_seconds: int = Field(default=600, ge=10)
    # Preflight ``retry``: how many proxies to try before answering 502.
    preflight_max_attempts: int = Field(default=3, ge=1, le=10)
    # Raw observations are kept this long; aggregates are kept forever.
    observation_retention_days: int = Field(default=7, ge=0)
    # Distinct exit IPs per connector are forgotten this long after they were
    # last seen; 0 keeps them forever.
    exit_ip_retention_days: int = Field(default=0, ge=0)

    @field_validator("default_sources")
    @classmethod
    def _sources(cls, value: list[GeoSourceKind]) -> list[GeoSourceKind]:
        return dedupe_sources(value)

    @property
    def default_policy(self) -> SourcePolicy:
        return SourcePolicy(sources=list(self.default_sources), conflict_rule=self.default_conflict_rule)


class GeoDatabaseRecord(BaseModel):
    """A database the install knows about, as stored in Postgres or named in the config."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    vendor: GeoVendor = GeoVendor.OTHER
    kind: GeoDatabaseKind = GeoDatabaseKind.UNKNOWN
    format: GeoDatabaseFormat = GeoDatabaseFormat.MMDB
    source: GeoDatabaseSource = GeoDatabaseSource.UPLOAD
    enabled: bool = True
    # Lower runs first. Databases are consulted in priority order and the
    # first one with a country wins among databases.
    priority: int = 100
    # Operator-managed file (source=path). Null for stored blobs.
    path: str | None = None
    sha256: str = ""
    size_bytes: int = 0
    # From the file's own metadata.
    database_type: str = ""
    build_epoch: datetime | None = None
    record_count: int = 0
    ip_version: int = 6
    languages: list[str] = Field(default_factory=list)
    description: str = ""
    # Attribution text the license requires the UI to show, when any.
    attribution: str = ""
    # Scheduled download (source=url).
    update_url: str | None = None
    update_interval_hours: int = 0
    update_auth: dict[str, Any] = Field(default_factory=dict)
    last_update_at: datetime | None = None
    last_update_error: str | None = None
    uploaded_by: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IpObservation(BaseModel):
    """One time an IP was seen behind a proxy, with what everyone said about it.

    Produced on the request and worker paths, buffered in-process, batched
    through Redis and written to Postgres by the leader. Never written
    synchronously on the path that produced it.
    """

    observed_at: datetime = Field(default_factory=utc_now)
    proxy_id: str | None = None
    connector_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    source: ObservationSource
    ip: str
    claimed_country: str | None = None
    endpoint_country: str | None = None
    resolved_country: str | None = None
    resolved_source: GeoSourceKind | None = None
    conflict: bool = False
    disagreement: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    instance_id: str = ""


# What an exit row's ``source`` says when its verdict last came from a re-judgement.
JUDGEMENT_SOURCE = "reattribute"


class ExitJudgement(BaseModel):
    """A re-judgement of an exit already on record, after the evidence changed.

    Produced by re-attribution: no request was made and nothing was sighted,
    the IP already on the proxy was resolved again against the current
    databases. It travels the same Redis list as observations and the flusher
    applies it to the exit row's claim, resolution and verdict only. It is not
    a log row, does not count as a hand-out and does not move first or last
    seen.
    """

    kind: Literal["judgement"] = "judgement"
    judged_at: datetime = Field(default_factory=utc_now)
    proxy_id: str
    connector_id: str | None = None
    ip: str
    claimed_country: str | None = None
    resolved_country: str | None = None
    resolved_source: GeoSourceKind | None = None
    conflict: bool = False
    disagreement: bool = False
    instance_id: str = ""


class LoadedDatabase(BaseModel):
    """A database this instance has open right now."""

    id: str
    name: str
    vendor: GeoVendor
    kind: GeoDatabaseKind
    format: GeoDatabaseFormat
    source: GeoDatabaseSource
    priority: int
    path: str
    size_bytes: int
    database_type: str = ""
    build_epoch: datetime | None = None
    record_count: int = 0


# --- proxy metadata keys ---------------------------------------------------------------------
#
# Written by GeoService.apply_observation. ``country`` itself stays the key
# routing reads (see Proxy.country), so nothing downstream had to change.

META_VENDOR_COUNTRY = "vendor_country"  # what the vendor claimed for this exit
META_ENDPOINT_COUNTRY = "endpoint_country"  # what a third-party discovery or echo endpoint reported for this exit
META_COUNTRY_SOURCE = "country_source"  # GeoSourceKind that produced metadata.country
META_LOCATION = "location"  # IpLocation dump from the databases
META_LOCATION_CONFLICT = "location_conflict"  # bool, see ConflictRule
META_LOCATION_CANDIDATES = "location_candidates"  # compact candidate list
META_LOCATION_CHECKED_AT = "location_checked_at"  # ISO timestamp of the last resolution
