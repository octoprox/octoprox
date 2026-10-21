# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Domain models for IP attribution.

Everything here is a plain value: the readers, resolver and service modules
produce and consume these, and the admin API serialises them as-is.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, NamedTuple
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now
from api.models.location import META_MANUAL_LOCATION, LocationTarget, slugify_place


class GeoVendor(StrEnum):
    """Who built a database. Drives nothing but labels and download presets."""

    MAXMIND = "maxmind"
    DBIP = "dbip"
    IPINFO = "ipinfo"
    IP2LOCATION = "ip2location"
    GEONAMES = "geonames"
    OTHER = "other"


class GeoDatabaseKind(StrEnum):
    """What a database answers. A city database also answers country."""

    COUNTRY = "country"
    CITY = "city"
    ASN = "asn"
    ANONYMOUS = "anonymous"
    PLACES = "places"
    UNKNOWN = "unknown"


class GeoDatabaseFormat(StrEnum):
    MMDB = "mmdb"
    IP2LOCATION_BIN = "ip2location_bin"
    GEONAMES = "geonames"


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
    # Any independent source agreeing with the vendor confirms it; none agreeing contradicts it.
    ANY = "any"


class ContradictedAction(StrEnum):
    """Per project: what to do when the independent sources agree against the vendor's claim.

    At preflight a contradicted exit is refused where the client named a place, unless the vendor is trusted.
    """

    # Routes the proxy on where the databases or the echo put the exit; at preflight with no place named, the exit is
    # held to the connector's declared countries.
    EVIDENCE = "evidence"
    # Routes it on the claim, and only shows the contradiction.
    VENDOR = "vendor"
    # Skips it, replacing a pooled session slot; at preflight with no place named, the exit is held to the vendor's
    # exact promise.
    REFUSE = "refuse"


class UnknownAction(StrEnum):
    """Per project: what to do when nothing independent places the exit (an open verdict).

    The verdict is open when no database knows the IP, or the databases disagree under ``consensus``.
    """

    # Routes on the vendor's word.
    VENDOR = "vendor"
    # Skips the proxy until a later sighting places it, and refuses a preflighted request at every level the client
    # named that nothing independent could place.
    REFUSE = "refuse"


class LocationRules(NamedTuple):
    """A project's two location decisions, read together by routing and preflight.

    A country pinned by hand is never open, and a row not judged yet is always eligible.
    """

    # A pin is the operator's word, never overwritten by attribution: refused only under ``REFUSE``.
    contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    unknown: UnknownAction = UnknownAction.VENDOR


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

# How far from the claimed city an independent answer may place the exit and
# still confirm a city claim, once a GeoNames file is loaded. 25 km takes in
# the municipalities around a large city (Diadema, Guarulhos and Osasco are
# about 15 km from São Paulo's centre) without reaching the next real one.
DEFAULT_CITY_RADIUS_KM = 25.0
MAX_CITY_RADIUS_KM = 500.0


def normalize_country(value: Any) -> str | None:
    """Upper-case two-letter code, or None for anything that is not one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return None
    return code


class IpLocation(BaseModel):
    """One database's normalised answer for an IP: ``country``, ``state_code`` and ``city`` take part in routing."""

    country: str | None = None
    # The first-level subdivision's name as the database spells it, for display.
    region: str | None = None
    # Its ISO 3166-2 part (``NY``, ``ON``, ``ENG``): what ``-st-`` names, and with the country and the city what
    # ``Proxy.location_matches`` matches a fixed exit on.
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
    """What the resolver decided for one IP, per level: the claim, the independent answer, the routing value and a verdict.

    Each verdict judges the claim by the independent evidence under the conflict rule, never by the resolved value.
    """

    # The first candidate in ``VALUE_PRECEDENCE`` order with an answer (the
    # claim only when nothing independent knows): what observations record.
    resolved_country: str | None = None
    resolved_source: GeoSourceKind | None = None
    resolved_origin: str | None = None
    # The first independent answer, which verification compares against; None
    # when only the vendor had an opinion.
    observed_country: str | None = None
    country_conflict: bool | None = None
    # What was promised: the vendor's claim, or the place a preflighted request required.
    claimed_country: str | None = None
    # The databases' merged record, for display and for the row's ``location``.
    location: IpLocation | None = None
    candidates: list[LocationCandidate] = Field(default_factory=list)
    # The state and the city, resolved the same way as the country.
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
    city_distance_km: float | None = None

    def compact_candidates(self) -> list[dict[str, Any]]:
        """Short form stored in proxy metadata: no nested location records."""
        return [
            {"source": c.source.value, "origin": c.origin, "country": c.country}
            for c in self.candidates
        ]


# The routing value, by precedence: the databases (in their own priority),
# then the echo endpoint, then the vendor's claim filling what nothing
# independent answered. Fixed: trust in the vendor is ``ContradictedAction``'s
# business, never the value's.
VALUE_PRECEDENCE: tuple[GeoSourceKind, ...] = (GeoSourceKind.DATABASE, GeoSourceKind.ENDPOINT, GeoSourceKind.VENDOR)


class SourcePolicy(BaseModel):
    """How a vendor's claim is judged for one project, and what a contradiction does to the routing value.

    The install's default rule (``GeoSettings.default_policy``) applies unless the project overrides it.
    """

    conflict_rule: ConflictRule = ConflictRule.CONSENSUS
    # Under ``vendor``, a contradicted or an open level carries the claim as its routing value.
    contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    unknown: UnknownAction = UnknownAction.VENDOR
    # How close an independent answer must put the exit to the claimed city to
    # confirm it when the names differ; 0, or no GeoNames file loaded, compares names only.
    city_radius_km: float = DEFAULT_CITY_RADIUS_KM


class GeoSettings(BaseModel):
    """Install-wide attribution settings, edited in the admin panel.

    One typed row (``geo_settings``), seeded on a fresh install from the config file's ``geo.defaults``, whose judgement
    of a contradicted vendor is only the default projects inherit.
    """

    default_conflict_rule: ConflictRule = ConflictRule.CONSENSUS
    # How far from the claimed city an answer may be and still confirm it;
    # projects may set their own. Takes effect once a GeoNames file is loaded.
    default_city_radius_km: float = Field(default=DEFAULT_CITY_RADIUS_KM, ge=0, le=MAX_CITY_RADIUS_KM)
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

    @property
    def default_policy(self) -> SourcePolicy:
        return SourcePolicy(conflict_rule=self.default_conflict_rule, city_radius_km=self.default_city_radius_km)


class GeoDatabaseInfo(BaseModel):
    """A database the install knows about, as anyone signed in may see it: everything but its download credentials."""

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
    # A paused schedule keeps its URL and interval; only a manual refresh downloads.
    update_paused: bool = False
    last_update_at: datetime | None = None
    last_update_error: str | None = None
    uploaded_by: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeoDatabaseRecord(GeoDatabaseInfo):
    """A database as stored in Postgres or named in the config, download credentials included."""

    # What the scheduled download authenticates with at the vendor (a
    # username and password, or a token). Read by the downloads only: the API
    # answers with GeoDatabaseInfo and says whether these are set.
    update_auth: dict[str, Any] = Field(default_factory=dict)


class IpObservation(BaseModel):
    """One time an IP was seen behind a proxy, with what everyone said about it.

    Produced on the request and worker paths, buffered in-process, appended to the observations stream and written to
    Postgres by the workers' ingest, never synchronously on the path that produced it.
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

    The ingest applies it to the exit row's claim, resolution and verdict only: no log row, hand-out or move of its times.
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


class ExitCoverage(BaseModel):
    """How much of a dynamic-sessions connector's traffic these numbers can see; absent where health checks see exits.

    Only preflight sees a dynamic connector's exits, so below 100 percent the session-less figures are undercounts.
    """

    dynamic: bool = True
    # Off: preflight sees none of its exits; only Detect or a plugin records them.
    preflight_on: bool
    sampled_percent: int


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
META_STATE_VERDICT = "state_verdict"  # how a claimed state fared: confirmed, contradicted, open
META_CITY_VERDICT = "city_verdict"
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


INDEPENDENT_SOURCES = (GeoSourceKind.DATABASE.value, GeoSourceKind.ENDPOINT.value)


def eligible_under(rules: LocationRules, metadata: dict[str, Any], target: LocationTarget | None = None) -> bool:
    """Whether the verdicts written on a proxy row let the project route to it for ``target``.

    The country is always judged, a state or city only when ``target`` names it; a row not judged yet is in.
    """
    judged = META_LOCATION_CHECKED_AT in metadata or META_LOCATION_CONFLICT in metadata
    verdict = metadata.get(META_LOCATION_VERDICT)
    if judged and not _level_eligible(
        rules,
        contradicted=metadata.get(META_LOCATION_CONFLICT) is True or verdict == LocationVerdict.CONTRADICTED,
        open_=verdict == LocationVerdict.OPEN,
        pinned=metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE,
        placed=metadata.get(META_COUNTRY_SOURCE) in INDEPENDENT_SOURCES or verdict == LocationVerdict.CONFIRMED,
    ):
        return False
    if target is None:
        return True
    manual = metadata.get(META_MANUAL_LOCATION)
    pins = manual if isinstance(manual, dict) else {}
    for asked, key, source_key, verdict_key in (
        (target.state, META_STATE, META_STATE_SOURCE, META_STATE_VERDICT),
        (target.city, META_CITY, META_CITY_SOURCE, META_CITY_VERDICT),
    ):
        pinned = bool(pins.get(key))
        if not asked or (key not in metadata and not pinned):
            continue
        level_verdict = metadata.get(verdict_key)
        if not _level_eligible(
            rules,
            contradicted=level_verdict == LocationVerdict.CONTRADICTED,
            open_=level_verdict == LocationVerdict.OPEN,
            pinned=pinned,
            placed=metadata.get(source_key) in INDEPENDENT_SOURCES or level_verdict == LocationVerdict.CONFIRMED,
        ):
            return False
    return True


def _level_eligible(rules: LocationRules, *, contradicted: bool, open_: bool, pinned: bool, placed: bool) -> bool:
    """One level of a judged row against the project's rules."""
    if contradicted:
        if rules.contradicted == ContradictedAction.REFUSE:
            return False
        # The evidence placed the exit; routing on it needs the value to be the
        # evidence's. A pin is the operator's deliberate placement and stays.
        return rules.contradicted == ContradictedAction.VENDOR or placed or pinned
    return not (rules.unknown == UnknownAction.REFUSE and not pinned and (open_ or not placed))
