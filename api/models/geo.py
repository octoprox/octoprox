# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP attribution's admin answers: the settings, the databases, lookups, re-attribution, the observation log, exit
statistics and the pipeline's status, with the filters the log and the exit IPs view take from the query string."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from api.geo.models import (
    ExitCoverage,
    GeoDatabaseInfo,
    GeoSettings,
    IpLocation,
    LoadedDatabase,
    Resolution,
    SourcePolicy,
)

if TYPE_CHECKING:
    from api.geo.models import LocationCandidate
    from api.models.system import GatewaySnapshot

# The country badge an observation or an exit IP is filtered on.
Verdict = Literal["contradicted", "open", "confirmed", "no_claim"]

# What exit statistics can see of each dynamic-sessions connector among the ids given, as the api's read model works it
# out; other connectors are absent.
ExitCoverages = Callable[[Iterable[str]], Awaitable[dict[str, ExitCoverage]]]


class GeoSettingsResponse(BaseModel):
    settings: GeoSettings
    from_database: bool
    echo_enabled: bool
    echo_trusted_proxies: list[str]
    # The databases the api process that answered has open, in lookup order.
    databases_loaded: list[LoadedDatabase]


class GeoDatabaseResponse(GeoDatabaseInfo):
    """A database row, with where it is open: the api process that answered, and each live gateway.

    Never the download credentials: any signed-in user may list databases, so the URL shows with its secrets masked.
    """

    has_update_auth: bool = False

    # Whether the api process that answered has it open (lookups and /echo
    # use its copy), and why not when opening it failed there.
    loaded_here: bool = False
    load_error: str | None = None
    has_file: bool = True
    # When the schedule is next due; null without one, or while it is paused or disabled.
    next_update_at: datetime | None = None
    # How many of the live gateways have it open, out of how many, as their
    # last heartbeats (a few seconds old) say; and why it is not open on the
    # gateways that report a reason, by gateway instance id.
    gateways_loaded: int = 0
    gateways_total: int = 0
    gateway_errors: dict[str, str] = Field(default_factory=dict)


class GeoDatabaseListResponse(BaseModel):
    databases: list[GeoDatabaseResponse]


class GeoDatabaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    update_url: str | None = None
    update_interval_hours: int | None = Field(default=None, ge=0, le=24 * 365)
    update_auth: dict[str, Any] | None = None
    update_paused: bool | None = None


class GeoDatabaseFromUrl(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    update_url: str = Field(min_length=8)
    update_interval_hours: int = Field(default=24, ge=0, le=24 * 365)
    update_auth: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class LookupRequest(BaseModel):
    ip: str
    # What to judge the databases' answer against, as a vendor's claim would be.
    claimed_country: str | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    # Resolve under this project's source policy instead of the install default.
    project_id: str | None = None


class LookupCandidate(BaseModel):
    source: str
    origin: str
    country: str | None
    location: IpLocation | None = None

    @classmethod
    def of(cls, candidate: LocationCandidate) -> LookupCandidate:
        """One database's answer for the looked-up IP."""
        return cls(
            source=candidate.source.value,
            origin=candidate.origin,
            country=candidate.country,
            location=candidate.location,
        )


class LookupResponse(BaseModel):
    ip: str
    resolution: Resolution
    policy: SourcePolicy
    candidates: list[LookupCandidate]
    databases_loaded: int


class ReattributeRequest(BaseModel):
    # One connector's proxies; every proxy when absent.
    connector_id: str | None = None


class ReattributeQueued(BaseModel):
    """A re-attribution a worker will run: the job's kind, and the key it is queued under (``all`` or ``connector:<id>``)."""

    kind: Literal["reattribute"] = "reattribute"
    key: str


class ObservationsResponse(BaseModel):
    observations: list[dict[str, Any]]
    # Rows matching the filters, counted no further than 10,000 past this
    # page; total_exact says whether counting reached the end. Short of it
    # the total is a lower bound, and /count gives the exact one.
    total: int
    total_exact: bool
    limit: int
    offset: int


class CountResponse(BaseModel):
    # Every row matching the filters, counted outright.
    total: int


class ClaimBreakdown(BaseModel):
    """A contradicted pair at one level: what was claimed, what attribution resolved, how many exits."""

    level: Literal["country", "state", "city"] = "country"
    claimed: str | None
    observed: str | None
    exits: int


class LevelAccuracy(BaseModel):
    """How a connector's exits with a claim at one level were judged, each exit once."""

    claimed: int = 0
    confirmed: int = 0
    contradicted: int = 0
    # A claim without a verdict: nothing independent answered at that level (no
    # database knows the IP, or none knows its city), or the independent sources
    # disagreed under ``consensus``. Neither the vendor's credit nor its fault.
    open: int = 0
    # Confirmed over confirmed plus contradicted, at every level; None before any verdict.
    accuracy: float | None = None

    @classmethod
    def from_counts(cls, counts: dict[str, int]) -> LevelAccuracy:
        judged = counts.get("confirmed", 0) + counts.get("contradicted", 0)
        return cls(
            accuracy=round(counts.get("confirmed", 0) / judged, 4) if judged else None, **counts
        )


class ConnectorAccuracy(BaseModel):
    """How a connector's distinct exits, seen in the window, judge the vendor's claims.

    Each exit counts once with the verdict of its latest observation, at
    every level a claim was made: the country the vendor promised, and the
    state and city requests asked for.
    """

    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    coverage: ExitCoverage | None = None
    exits: int
    country: LevelAccuracy = Field(default_factory=LevelAccuracy)
    state: LevelAccuracy = Field(default_factory=LevelAccuracy)
    city: LevelAccuracy = Field(default_factory=LevelAccuracy)
    breakdown: list[ClaimBreakdown]


class AccuracyResponse(BaseModel):
    since: datetime
    connectors: list[ConnectorAccuracy]


class ConnectorExits(BaseModel):
    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    coverage: ExitCoverage | None = None
    unique_total: int
    unique_in_window: int
    sightings: int
    reused: int  # IPs handed out more than once
    max_sightings: int
    last_seen: datetime | None = None


class ExitsResponse(BaseModel):
    since: datetime
    connectors: list[ConnectorExits]


class ConnectorSharing(BaseModel):
    """A connector's distinct exits seen in the window, how many it reused and how many another connector also had."""

    connector_id: str
    connector_name: str | None = None
    provider: str | None = None  # the connector's credential type
    coverage: ExitCoverage | None = None
    exits: int
    reused: int  # handed out more than once, ever
    shared: int  # also handed out by another connector of the project in the window


class SharedPair(BaseModel):
    """The exits two connectors both handed out in the window; ``connector_id`` sorts before ``other_id``."""

    connector_id: str
    other_id: str
    shared: int


class SharingResponse(BaseModel):
    since: datetime
    exits: int  # distinct exits across the project's connectors
    shared: int  # of those, handed out by more than one connector
    connectors: list[ConnectorSharing]
    pairs: list[SharedPair]


class ExitIp(BaseModel):
    """One distinct exit of a connector, with the state of its latest observation."""

    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    ip: str
    first_seen: datetime
    last_seen: datetime
    sightings: int
    country: str | None = None
    proxy_id: str | None = None
    source: str | None = None
    claimed_country: str | None = None
    resolved_source: str | None = None
    country_conflict: bool | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    resolved_state: str | None = None
    resolved_city: str | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None


class ExitIpsResponse(BaseModel):
    ips: list[ExitIp]
    # Counted as for observations: total_exact false makes total a lower bound.
    total: int
    total_exact: bool
    limit: int
    offset: int


class GatewayGeoStatus(BaseModel):
    """One live gateway's attribution, as its last heartbeat (a few seconds old) reports it."""

    instance_id: str
    # The IP databases it has open, the ones it still waits for (their ids),
    # and why the others are not open, by database id.
    databases_loaded: int
    databases_missing: list[str]
    load_errors: dict[str, str]
    # Echoes preflight sent through an upstream, and verdicts that placed the
    # exit elsewhere, since the gateway started.
    preflight_checks: int
    preflight_rejections: int
    # Observations and preflight sightings waiting for Redis, and those
    # dropped since the gateway started because Redis would not take them.
    observations_pending: int
    observations_dropped: int
    sightings_pending: int
    sightings_dropped: int

    @classmethod
    def of(cls, instance_id: str, gateway: GatewaySnapshot) -> GatewayGeoStatus:
        """One gateway's attribution, from its heartbeat."""
        return cls(
            instance_id=instance_id,
            databases_loaded=len(gateway.geo_databases_loaded),
            databases_missing=gateway.geo_databases_missing,
            load_errors=gateway.geo_database_errors,
            preflight_checks=gateway.preflight_checks,
            preflight_rejections=gateway.preflight_rejections,
            observations_pending=gateway.observations_pending,
            observations_dropped=gateway.observations_dropped,
            sightings_pending=gateway.sightings_pending,
            sightings_dropped=gateway.sightings_dropped,
        )


class GeoStatusResponse(BaseModel):
    # The api process that answered: the databases it has open, out of
    # every database listed, and why the others are not open here.
    databases_loaded: int
    databases_total: int
    load_errors: dict[str, str]
    policy_from_database: bool
    # This api process's own observation buffer (Detect records into it);
    # each gateway's is in ``gateways``.
    pending_observations: int
    published_observations: int
    dropped_observations: int
    # Published by every process and not yet ingested by a worker (the
    # observations stream: sightings, and verdict rewrites after a
    # re-attribution); exit IPs and accuracy lag until they are. None when
    # Redis cannot say.
    queued_observations: int | None = None
    # The planner's row estimate, within a few percent: an exact count reads the table.
    stored_observations: int
    # Preflight runs on gateways: summed over every live gateway, since each started.
    preflight_checks: int
    preflight_rejections: int
    # Every live gateway, by instance id; empty when the heartbeats cannot be read.
    gateways: list[GatewayGeoStatus] = Field(default_factory=list)


@dataclass(frozen=True)
class ObservationFilters:
    """The observation log's filters as the query string gave them; a filter left None does not narrow."""

    connector_id: str | None = None
    proxy_id: str | None = None
    project_id: str | None = None
    source: str | None = None
    ip: str | None = None
    claimed_country: str | None = None
    resolved_country: str | None = None
    # The country's: the state and city verdicts come per row and move no badge.
    verdict: Verdict | None = None
    # The older spelling of ``verdict=contradicted``.
    conflicts_only: bool = False
    # Where the log starts instead of now; a time without an offset is UTC.
    until: datetime | None = None


@dataclass(frozen=True)
class ExitIpFilters:
    """The exit IPs view's filters as the query string gave them; a project stands for its connectors."""

    project_id: str | None = None
    connector_id: str | None = None
    ip: str | None = None
    proxy_id: str | None = None
    country: str | None = None
    claimed_country: str | None = None
    verdict: Verdict | None = None
