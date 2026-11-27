# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project model definitions for multi-tenancy."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.core import utc_now
from api.core.stats import MetricDelta
from api.geo.models import (
    MAX_CITY_RADIUS_KM,
    MAX_OBSERVATION_RETENTION_DAYS,
    ConflictRule,
    ContradictedAction,
    LocationRules,
    SourcePolicy,
    UnknownAction,
)
from api.models.audit import Audited, AuditEntityType


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


class ExitReputationMode(StrEnum):
    """What a project does with exit reports; each mode does what the one before it does, and more."""

    OFF = "off"
    RECORD = "record"
    AVOID = "avoid"

    @property
    def records(self) -> bool:
        """Whether exit reports are taken, and requests counted for the connectors' block rates."""
        return self is not ExitReputationMode.OFF

    @property
    def avoids(self) -> bool:
        """Whether routing acts on it: exits avoided for a site kept away from it, connectors weighted by their record."""
        return self is ExitReputationMode.AVOID


# The longest "Avoid for": a week, the longest any avoid lasts.
MAX_REPUTATION_AVOID_MINUTES = 10_080
# The longest exit reputation half-life ("Remember for"): past the 7 days an avoid can last, a longer one changes
# nothing.
MAX_REPUTATION_HALF_LIFE_HOURS = 168
# The longest connector memory, in requests ("Connector memory"). The shortest is 20: a busy record's weighted requests
# settle at about 1.44 times it at least, and a factor needs 20 of them.
MAX_REPUTATION_CONNECTOR_HALF_LIFE_REQUESTS = 10_000
# The longest connector memory, in minutes ("Connector memory"): a day. "Remember for" caps it as well.
MAX_REPUTATION_CONNECTOR_HALF_LIFE_MINUTES = 1440
# The most fresh bad reports a project may ask for before an exit is avoided.
MAX_REPUTATION_BAD_REPORTS = 10
# The highest minimum bad share, in percent: at 100 a single good report would keep an exit from ever being avoided
# again for the site, since decayed weights never reach zero.
MAX_REPUTATION_BAD_SHARE = 99
# The highest strength a dynamic connector is compared with the best at a place at: at 20 one blocked 12% of the time
# beside one blocked 1.3% keeps about a tenth of its weight.
MAX_REPUTATION_STRENGTH = 20
# The highest lowest factor, in percent: at 100 no dynamic connector's weight is ever lowered.
MAX_REPUTATION_FLOOR = 100
# The longest a project's exit reputation data is kept after it was last used, in days: a year.
MAX_REPUTATION_RETENTION_DAYS = 365


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


class Project(Audited):
    """Represents a project for multi-tenancy."""

    audit_type: ClassVar[AuditEntityType] = AuditEntityType.PROJECT
    # A delete is its own action, recorded when it marks the project.
    audit_ignored: ClassVar[frozenset[str]] = Audited.audit_ignored | {"pending_deletion"}
    audit_masked: ClassVar[frozenset[str]] = frozenset({"password"})

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""

    # Proxy authentication credentials (plain text as per requirements)
    username: str
    password: str

    # Project-level settings (override global defaults)
    routing_strategy: str = "round_robin"
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
    # Whether a client session preflight refused moves to a new exit, which it
    # then keeps, instead of being refused (see docs/location-routing.md).
    location_session_move: bool = False
    # When a vendor counts as contradicted. None inherits the install default (GeoSettings).
    location_conflict_rule: ConflictRule | None = None
    # How far from a claimed city an answer may be and still confirm it, in km;
    # 0 compares names only. None inherits the install default (GeoSettings).
    location_city_radius_km: float | None = Field(default=None, ge=0, le=MAX_CITY_RADIUS_KM)

    # Whether clients are told each request's exit IP in X-Octoprox-* response
    # headers (see docs/connecting.md).
    exit_headers: bool = False

    # Whether the project's sightings get an observation log row, and for how
    # many days (0 forever); None inherits the install's observation retention.
    # Off, the exit IPs and provider accuracy still learn of every sighting.
    observation_log: bool = True
    observation_retention_days: int | None = Field(default=None, ge=0, le=MAX_OBSERVATION_RETENTION_DAYS)

    # Exit reputation (see docs/exit-reputation.md): what exit reports do, how many minutes a first avoid lasts (each
    # repeat doubles it), how many hours an exit IP's reports take to count half ("Remember for", the longest a dynamic
    # connector's evidence takes too), how many more requests and how many minutes a dynamic connector's evidence takes
    # to count half, whichever is longer ("Connector memory"), how many fresh bad reports avoid an exit for a site, the
    # least share (percent) of the site's recent reports that must be bad, how hard a dynamic connector is weighed
    # against the best one at a place ("Strength", 1 the plain trim), the least share (percent) of its
    # weight a dynamic connector keeps ("Lowest factor"), and how many days the project's data is kept after it was
    # last used ("Keep data for", 0 forever): a site's records (an exit IP's reports and avoids, a dynamic connector's
    # factor per place) after the day they were last seen, by a report or a request counted, and the Over time chart's
    # history after the time it was recorded. The hourly compaction on the workers deletes the older ones, an exit IP's
    # record only once its avoid has passed; a record that goes takes its evidence with it, and the next report starts
    # it afresh. At the default "Remember for" of a day, 30 days leaves evidence worth about a billionth of a fresh
    # report, so an IP that comes back after that starts again with nothing lost.
    exit_reputation: ExitReputationMode = ExitReputationMode.OFF
    exit_reputation_avoid_minutes: int = Field(default=60, ge=1, le=MAX_REPUTATION_AVOID_MINUTES)
    exit_reputation_half_life_hours: int = Field(default=24, ge=1, le=MAX_REPUTATION_HALF_LIFE_HOURS)
    exit_reputation_connector_half_life_requests: int = Field(
        default=100, ge=20, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_REQUESTS
    )
    exit_reputation_connector_half_life_minutes: int = Field(
        default=10, ge=1, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_MINUTES
    )
    exit_reputation_bad_reports: int = Field(default=1, ge=1, le=MAX_REPUTATION_BAD_REPORTS)
    exit_reputation_bad_share: int = Field(default=50, ge=1, le=MAX_REPUTATION_BAD_SHARE)
    exit_reputation_strength: int = Field(default=1, ge=1, le=MAX_REPUTATION_STRENGTH)
    exit_reputation_floor: int = Field(default=10, ge=1, le=MAX_REPUTATION_FLOOR)
    exit_reputation_retention_days: int = Field(default=30, ge=0, le=MAX_REPUTATION_RETENTION_DAYS)

    # Deleted, and waiting for its cloud instances to be terminated: its clients are turned away and it takes no new
    # connector. Only a delete sets it (``ProjectRepository.mark_pending_deletion``); create and edit never write it.
    pending_deletion: bool = False

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def audit_project(self) -> str | None:
        return self.id

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
    connection_timeout: int = 30
    max_retries: int = 3
    tls_mitm_mode: MitmMode = MitmMode.OFF
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    metrics_retention_days: int = 90
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_session_move: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = Field(default=None, ge=0, le=MAX_CITY_RADIUS_KM)
    exit_headers: bool = False
    observation_log: bool = True
    observation_retention_days: int | None = Field(default=None, ge=0, le=MAX_OBSERVATION_RETENTION_DAYS)
    exit_reputation: ExitReputationMode = ExitReputationMode.OFF
    exit_reputation_avoid_minutes: int = Field(default=60, ge=1, le=MAX_REPUTATION_AVOID_MINUTES)
    exit_reputation_half_life_hours: int = Field(default=24, ge=1, le=MAX_REPUTATION_HALF_LIFE_HOURS)
    exit_reputation_connector_half_life_requests: int = Field(
        default=100, ge=20, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_REQUESTS
    )
    exit_reputation_connector_half_life_minutes: int = Field(
        default=10, ge=1, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_MINUTES
    )
    exit_reputation_bad_reports: int = Field(default=1, ge=1, le=MAX_REPUTATION_BAD_REPORTS)
    exit_reputation_bad_share: int = Field(default=50, ge=1, le=MAX_REPUTATION_BAD_SHARE)
    exit_reputation_strength: int = Field(default=1, ge=1, le=MAX_REPUTATION_STRENGTH)
    exit_reputation_floor: int = Field(default=10, ge=1, le=MAX_REPUTATION_FLOOR)
    exit_reputation_retention_days: int = Field(default=30, ge=0, le=MAX_REPUTATION_RETENTION_DAYS)

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
    connection_timeout: int | None = None
    max_retries: int | None = None
    tls_mitm_mode: MitmMode | None = None
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    metrics_retention_days: int | None = None
    location_contradicted: ContradictedAction | None = None
    location_unknown: UnknownAction | None = None
    location_preflight: bool | None = None
    location_session_move: bool | None = None
    # "" clears the override (inherit the install default).
    location_conflict_rule: ConflictRule | Literal[""] | None = None
    # "" clears the override (inherit the install default).
    location_city_radius_km: Annotated[float, Field(ge=0, le=MAX_CITY_RADIUS_KM)] | Literal[""] | None = None
    exit_headers: bool | None = None
    observation_log: bool | None = None
    # "" clears the override (inherit the install's observation retention).
    observation_retention_days: Annotated[int, Field(ge=0, le=MAX_OBSERVATION_RETENTION_DAYS)] | Literal[""] | None = None
    exit_reputation: ExitReputationMode | None = None
    exit_reputation_avoid_minutes: Annotated[int, Field(ge=1, le=MAX_REPUTATION_AVOID_MINUTES)] | None = None
    exit_reputation_half_life_hours: Annotated[int, Field(ge=1, le=MAX_REPUTATION_HALF_LIFE_HOURS)] | None = None
    exit_reputation_connector_half_life_requests: (
        Annotated[int, Field(ge=20, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_REQUESTS)] | None
    ) = None
    exit_reputation_connector_half_life_minutes: (
        Annotated[int, Field(ge=1, le=MAX_REPUTATION_CONNECTOR_HALF_LIFE_MINUTES)] | None
    ) = None
    exit_reputation_bad_reports: Annotated[int, Field(ge=1, le=MAX_REPUTATION_BAD_REPORTS)] | None = None
    exit_reputation_bad_share: Annotated[int, Field(ge=1, le=MAX_REPUTATION_BAD_SHARE)] | None = None
    exit_reputation_strength: Annotated[int, Field(ge=1, le=MAX_REPUTATION_STRENGTH)] | None = None
    exit_reputation_floor: Annotated[int, Field(ge=1, le=MAX_REPUTATION_FLOOR)] | None = None
    exit_reputation_retention_days: Annotated[int, Field(ge=0, le=MAX_REPUTATION_RETENTION_DAYS)] | None = None

    def changes(self) -> dict[str, object]:
        """The fields this edit sets, with the empty strings that clear an override turned into None."""
        changes: dict[str, object] = self.model_dump(exclude_none=True)
        for name in ("location_conflict_rule", "location_city_radius_km", "observation_retention_days"):
            if changes.get(name) == "":
                changes[name] = None
        return changes


@dataclass(frozen=True)
class PoolCounts:
    """A project's pool as the dashboard counts it: the proxies of its enabled connectors no traffic limit blocks."""

    total: int = 0
    # Healthy and not quarantined: what requests can be routed to.
    healthy: int = 0
    # Quarantined by a rate limit, whatever their status.
    quarantined: int = 0
    draining: int = 0
    terminating: int = 0

    @property
    def unhealthy(self) -> int:
        """What is neither routable nor on its way out (quarantined healthy proxies count here)."""
        return self.total - self.healthy - self.draining - self.terminating


@dataclass
class ProjectCounts:
    """What one project holds, as the projects list shows it (``ProjectRepository.get_counts``)."""

    credentials: int = 0
    connectors: int = 0
    # Proxies per status (ProxyStatus values); a status no proxy has is absent.
    proxies_by_status: dict[str, int] = field(default_factory=dict)

    @property
    def proxies(self) -> int:
        """Every proxy of the project, whatever its status."""
        return sum(self.proxies_by_status.values())


@dataclass(frozen=True)
class ProjectView:
    """A project with what it holds."""

    project: Project
    credentials: int
    connectors: int
    pool: PoolCounts

    @classmethod
    def of(cls, project: Project, counts: ProjectCounts | None, pool: PoolCounts) -> "ProjectView":
        """A project with its counts; a project holding nothing has no counts row."""
        held = counts or ProjectCounts()
        return cls(project=project, credentials=held.credentials, connectors=held.connectors, pool=pool)


@dataclass(frozen=True)
class ProjectLoad:
    """A project's load: its request rate now, and its requests over a range, in total and as a trend."""

    project_id: str
    # Over the demand window, rejected requests included: the rate the Overview and scaling read.
    requests_per_minute: float
    # The range's counters, from the flushed history.
    counters: MetricDelta
    # Requests per bucket, oldest first, the newest ending at the last flush.
    trend: tuple[int, ...]


class ProjectResponse(BaseModel):
    """Schema for project API responses."""
    id: str
    name: str
    description: str
    username: str
    password: str
    routing_strategy: str
    connection_timeout: int
    max_retries: int
    tls_mitm_mode: MitmMode
    tls_mitm_engine: MitmEngine | None
    tls_mitm_browser: MitmBrowser | None
    metrics_retention_days: int
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_session_move: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = None
    exit_headers: bool = False
    observation_log: bool = True
    observation_retention_days: int | None = None
    exit_reputation: ExitReputationMode = ExitReputationMode.OFF
    exit_reputation_avoid_minutes: int = 60
    exit_reputation_half_life_hours: int = 24
    exit_reputation_connector_half_life_requests: int = 100
    exit_reputation_connector_half_life_minutes: int = 10
    exit_reputation_bad_reports: int = 1
    exit_reputation_bad_share: int = 50
    exit_reputation_strength: int = 1
    exit_reputation_floor: int = 10
    exit_reputation_retention_days: int = 30
    pending_deletion: bool = False
    created_at: datetime
    updated_at: datetime
    # Aggregated stats (populated by API)
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0

    @classmethod
    def of(
        cls,
        project: Project,
        credential_count: int = 0,
        connector_count: int = 0,
        proxy_count: int = 0,
        healthy_proxy_count: int = 0,
    ) -> "ProjectResponse":
        """A project as the API shows it, with the counts given (none for a project just created)."""
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            username=project.username,
            password=project.password,
            routing_strategy=project.routing_strategy,
            connection_timeout=project.connection_timeout,
            max_retries=project.max_retries,
            tls_mitm_mode=project.tls_mitm_mode,
            tls_mitm_engine=project.tls_mitm_engine,
            tls_mitm_browser=project.tls_mitm_browser,
            metrics_retention_days=project.metrics_retention_days,
            location_contradicted=project.location_contradicted,
            location_unknown=project.location_unknown,
            location_preflight=project.location_preflight,
            location_session_move=project.location_session_move,
            location_conflict_rule=project.location_conflict_rule,
            location_city_radius_km=project.location_city_radius_km,
            exit_headers=project.exit_headers,
            observation_log=project.observation_log,
            observation_retention_days=project.observation_retention_days,
            exit_reputation=project.exit_reputation,
            exit_reputation_avoid_minutes=project.exit_reputation_avoid_minutes,
            exit_reputation_half_life_hours=project.exit_reputation_half_life_hours,
            exit_reputation_connector_half_life_requests=project.exit_reputation_connector_half_life_requests,
            exit_reputation_connector_half_life_minutes=project.exit_reputation_connector_half_life_minutes,
            exit_reputation_bad_reports=project.exit_reputation_bad_reports,
            exit_reputation_bad_share=project.exit_reputation_bad_share,
            exit_reputation_strength=project.exit_reputation_strength,
            exit_reputation_floor=project.exit_reputation_floor,
            exit_reputation_retention_days=project.exit_reputation_retention_days,
            pending_deletion=project.pending_deletion,
            created_at=project.created_at,
            updated_at=project.updated_at,
            credential_count=credential_count,
            connector_count=connector_count,
            proxy_count=proxy_count,
            healthy_proxy_count=healthy_proxy_count,
        )

    @classmethod
    def of_view(cls, view: "ProjectView") -> "ProjectResponse":
        """A project with its counts; the proxy counts are its pool's (enabled connectors no traffic limit blocks)."""
        return cls.of(
            view.project,
            credential_count=view.credentials,
            connector_count=view.connectors,
            proxy_count=view.pool.total,
            healthy_proxy_count=view.pool.healthy,
        )


class ProjectSummary(BaseModel):
    """Summary of a project for listing; the project page edits from it, so it carries every field the form sends."""
    id: str
    name: str
    description: str
    username: str
    password: str
    routing_strategy: str
    metrics_retention_days: int
    tls_mitm_mode: MitmMode = MitmMode.OFF
    tls_mitm_engine: MitmEngine | None = None
    tls_mitm_browser: MitmBrowser | None = None
    location_contradicted: ContradictedAction = ContradictedAction.EVIDENCE
    location_unknown: UnknownAction = UnknownAction.VENDOR
    location_preflight: bool = False
    location_session_move: bool = False
    location_conflict_rule: ConflictRule | None = None
    location_city_radius_km: float | None = None
    exit_headers: bool = False
    observation_log: bool = True
    observation_retention_days: int | None = None
    exit_reputation: ExitReputationMode = ExitReputationMode.OFF
    exit_reputation_avoid_minutes: int = 60
    exit_reputation_half_life_hours: int = 24
    exit_reputation_connector_half_life_requests: int = 100
    exit_reputation_connector_half_life_minutes: int = 10
    exit_reputation_bad_reports: int = 1
    exit_reputation_bad_share: int = 50
    exit_reputation_strength: int = 1
    exit_reputation_floor: int = 10
    exit_reputation_retention_days: int = 30
    pending_deletion: bool = False
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0
    created_at: datetime

    @classmethod
    def of(cls, view: "ProjectView") -> "ProjectSummary":
        """A project with its counts, as listed; the proxy counts are its pool's."""
        project = view.project
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            username=project.username,
            password=project.password,
            routing_strategy=project.routing_strategy,
            metrics_retention_days=project.metrics_retention_days,
            tls_mitm_mode=project.tls_mitm_mode,
            tls_mitm_engine=project.tls_mitm_engine,
            tls_mitm_browser=project.tls_mitm_browser,
            location_contradicted=project.location_contradicted,
            location_unknown=project.location_unknown,
            location_preflight=project.location_preflight,
            location_session_move=project.location_session_move,
            location_conflict_rule=project.location_conflict_rule,
            location_city_radius_km=project.location_city_radius_km,
            exit_headers=project.exit_headers,
            observation_log=project.observation_log,
            observation_retention_days=project.observation_retention_days,
            exit_reputation=project.exit_reputation,
            exit_reputation_avoid_minutes=project.exit_reputation_avoid_minutes,
            exit_reputation_half_life_hours=project.exit_reputation_half_life_hours,
            exit_reputation_connector_half_life_requests=project.exit_reputation_connector_half_life_requests,
            exit_reputation_connector_half_life_minutes=project.exit_reputation_connector_half_life_minutes,
            exit_reputation_bad_reports=project.exit_reputation_bad_reports,
            exit_reputation_bad_share=project.exit_reputation_bad_share,
            exit_reputation_strength=project.exit_reputation_strength,
            exit_reputation_floor=project.exit_reputation_floor,
            exit_reputation_retention_days=project.exit_reputation_retention_days,
            pending_deletion=project.pending_deletion,
            credential_count=view.credentials,
            connector_count=view.connectors,
            proxy_count=view.pool.total,
            healthy_proxy_count=view.pool.healthy,
            created_at=project.created_at,
        )


class ProjectListResponse(BaseModel):
    """Response for listing projects."""
    total: int
    projects: list[ProjectSummary]

    @classmethod
    def of(cls, views: list["ProjectView"]) -> "ProjectListResponse":
        """The projects as listed, in the order given."""
        summaries = [ProjectSummary.of(view) for view in views]
        return cls(total=len(summaries), projects=summaries)


class ProjectLoadEntry(BaseModel):
    """One project's load: its request rate now, and its requests over the range."""
    project_id: str
    # Over the last five minutes, rejected requests included: the Overview's requests per minute.
    requests_per_minute: float
    request_count: int
    success_count: int
    failure_count: int
    bytes_sent: int
    bytes_received: int
    # Requests per bucket of ``bucket_seconds``, oldest first, the last ending at the last metrics flush.
    trend: list[int]

    @classmethod
    def of(cls, load: "ProjectLoad") -> "ProjectLoadEntry":
        """A project's load, its rate rounded to hundredths."""
        return cls(
            project_id=load.project_id,
            requests_per_minute=round(load.requests_per_minute, 2),
            request_count=load.counters.request_count,
            success_count=load.counters.success_count,
            failure_count=load.counters.failure_count,
            bytes_sent=load.counters.bytes_sent,
            bytes_received=load.counters.bytes_received,
            trend=list(load.trend),
        )


class ProjectLoadResponse(BaseModel):
    """Every project's load over one range, for the project list."""
    range: str
    bucket_seconds: int
    projects: list[ProjectLoadEntry]

    @classmethod
    def of(cls, range: str, bucket_seconds: int, loads: list["ProjectLoad"]) -> "ProjectLoadResponse":
        """Every project's load over ``range`` in buckets of ``bucket_seconds``, in the order given."""
        return cls(range=range, bucket_seconds=bucket_seconds, projects=[ProjectLoadEntry.of(load) for load in loads])


# What each range reads: how far back, and the trend's bucket in seconds. Two to three dozen points, none finer than
# the rows kept at that age (minute rows for a day, hourly ones for a week, six-hourly ones after), so no bucket is
# left empty between two full ones.
LOAD_RANGES: dict[str, tuple[timedelta, int]] = {
    "1h": (timedelta(hours=1), 120),
    "6h": (timedelta(hours=6), 900),
    "24h": (timedelta(hours=24), 3600),
    "7d": (timedelta(days=7), 21600),
    "30d": (timedelta(days=30), 86400),
}

