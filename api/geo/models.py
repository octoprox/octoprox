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
from api.models.location import slugify_place


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


class LocationAssurance(StrEnum):
    """Per project: which verdicts on a claimed location may still serve a request.

    One answer for every kind of row. A fixed exit and a pooled session slot
    carry the verdict attribution wrote on them; a dynamic-sessions gateway
    carries none, and its verdict is the one preflight reaches for the vendor
    session rendered for the request. The level decides eligibility and what
    a preflight verdict does to the request; which country a proxy is routed
    on is the source policy's call (``SourcePolicy.sources``) under every
    level.

    ``observe`` serves every verdict and shows it. ``verified`` refuses a
    contradicted claim and serves an open one (no independent source knows
    the IP, or they disagree under ``consensus``) on the vendor's word.
    ``confirmed`` wants clear evidence: only a confirmed claim serves (or one
    not judged yet), so an open claim is refused until a later sighting
    settles it, unless the country was pinned by hand. A refused pooled
    session slot whose claim is contradicted is replaced with a fresh
    session. See :func:`eligible_under`.
    """

    OBSERVE = "observe"  # route everywhere, surface the verdicts
    VERIFIED = "verified"  # a contradicted claim is refused, an open one trusted
    CONFIRMED = "confirmed"  # only a confirmed (or unjudged) claim serves


class LocationVerdict(StrEnum):
    """How a vendor claim fared against the independent evidence, as stored on the row."""

    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    OPEN = "open"  # no independent answer, or the independent sources disagreed under consensus

    @classmethod
    def of(cls, conflict: bool | None, claimed: str | None) -> LocationVerdict | None:
        """The verdict for one level, None when nothing was claimed there."""
        if claimed is None:
            return None
        if conflict is None:
            return cls.OPEN
        return cls.CONTRADICTED if conflict else cls.CONFIRMED


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

    ``country``, ``state_code`` and ``city`` take part in routing: a
    request for ``-st-`` or ``-city-`` matches a fixed exit on them (see
    ``Proxy.location_matches``). ``region`` is the first-level subdivision's
    name as the database spells it, kept for display; ``state_code`` its ISO
    3166-2 part (``NY``, ``ON``, ``ENG``), which is what ``-st-`` names.
    """

    country: str | None = None
    region: str | None = None
    state_code: str | None = None
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
    """One source's opinion about where an IP is.

    ``country`` is the answer at the level every source can give; ``location``
    carries the rest (state code, city, and for databases the whole record).
    """

    source: GeoSourceKind
    origin: str  # database id, "vendor", or the endpoint URL
    country: str | None = None
    location: IpLocation | None = None

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: Any) -> str | None:
        return normalize_country(value)

    @property
    def state(self) -> str | None:
        """The candidate's subdivision code, or None."""
        return (self.location.state_code or None) if self.location else None

    @property
    def city(self) -> str | None:
        """The candidate's city as the comparison slug (a database records the name)."""
        return slugify_place(self.location.city) if self.location else None


class Resolution(BaseModel):
    """What the resolver decided for one IP.

    Three countries, one question each:

    * ``claimed_country``: what was promised. The vendor's claim for the
      exit, or the place a request required when preflight verifies it.
    * ``observed_country``: what independent evidence found. The first
      database or endpoint answer in policy order; None when only the
      vendor had an opinion. This is what verification compares against.
    * ``resolved_country``: what routing uses. The first candidate in
      ``policy.sources`` order with an answer, which the policy may let be
      the claim itself when it ranks the vendor first or nothing else knows.

    ``country_conflict`` judges the claim by the independent evidence under
    the conflict rule, never by ``resolved_country``.

    The state and the city are resolved the same way, from the same
    candidates under the same policy: a vendor's listed city or a ``-city-``
    request is the claim, databases and endpoints are the evidence, and the
    policy decides what routing uses. Every level's verdict has the same
    form: ``country_conflict``, ``state_conflict``, ``city_conflict``.
    """

    resolved_country: str | None = None
    resolved_source: GeoSourceKind | None = None
    resolved_origin: str | None = None
    observed_country: str | None = None
    country_conflict: bool | None = None
    claimed_country: str | None = None
    # The databases' merged record, for display and for the row's ``location``.
    location: IpLocation | None = None
    candidates: list[LocationCandidate] = Field(default_factory=list)
    claimed_state: str | None = None
    observed_state: str | None = None
    resolved_state: str | None = None
    resolved_state_source: GeoSourceKind | None = None
    state_conflict: bool | None = None
    claimed_city: str | None = None
    observed_city: str | None = None
    resolved_city: str | None = None
    resolved_city_source: GeoSourceKind | None = None
    city_conflict: bool | None = None

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
    # Where the echo reports the exit's state and city, when it does; Octoprox's own
    # /echo answers ``state_code`` and ``city`` from the loaded databases.
    echo_state_path: str | None = None
    echo_city_path: str | None = None
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
    country_conflict: bool | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    resolved_state: str | None = None
    resolved_city: str | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None
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
    # One verdict per level: True contradicted, False confirmed, None when nothing
    # was claimed, nothing independent answered, or the sources disagreed.
    country_conflict: bool | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    resolved_state: str | None = None
    resolved_city: str | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None
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
META_COUNTRY_SOURCE = "country_source"  # GeoSourceKind that produced metadata.country, or MANUAL_SOURCE
MANUAL_SOURCE = "manual"  # META_COUNTRY_SOURCE value for a country an operator set by hand
# The same one level down. What routing reads (``state_code``, ``city``) is
# what attribution resolved, with its source; the vendor's and the endpoint's
# own words are kept apart so the claim can be judged.
META_STATE = "state_code"
META_CITY = "city"
META_STATE_SOURCE = "state_source"
META_CITY_SOURCE = "city_source"
META_VENDOR_STATE = "vendor_state"
META_VENDOR_CITY = "vendor_city"
META_ENDPOINT_STATE = "endpoint_state"
META_ENDPOINT_CITY = "endpoint_city"
META_LOCATION = "location"  # IpLocation dump from the databases
# META_MANUAL_LOCATION (api.models.location): region code and city an operator
# pinned by hand on a static proxy; routing prefers them to the databases.
META_LOCATION_CONFLICT = "location_conflict"  # bool: the country claim is contradicted; present once judged
META_LOCATION_VERDICT = "location_verdict"  # LocationVerdict for the country claim; absent when nothing was claimed
META_LOCATION_CANDIDATES = "location_candidates"  # compact candidate list
META_LOCATION_CHECKED_AT = "location_checked_at"  # ISO timestamp of the last resolution


def eligible_under(assurance: LocationAssurance, metadata: dict[str, Any]) -> bool:
    """Whether the verdict written on a proxy row lets the project route to it.

    ``observe`` routes to every row. ``verified`` refuses a contradicted
    claim only. ``confirmed`` also refuses an open one (the independent
    sources disagreed under ``consensus``, or none of them knows the IP).
    Under every level a confirmed claim, a proxy that made no claim, and one
    not judged yet (a slot provisioned seconds ago, before its first health
    check) are in, so that on-demand provisioning and a fresh pool stay
    routable. Rows written before the verdict was stored carry only the flag
    and read as judged by it alone.

    A country an operator pinned by hand is judged like a vendor claim but
    trusted while open: the operator placed that row deliberately, and a
    static list in ranges no database covers would otherwise go dark under
    ``confirmed``. A pin the databases agree against is still out.
    """
    if assurance == LocationAssurance.OBSERVE:
        return True
    if metadata.get(META_LOCATION_CONFLICT) is True:
        return False
    if assurance == LocationAssurance.VERIFIED or metadata.get(META_LOCATION_VERDICT) != LocationVerdict.OPEN:
        return True
    return metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE
