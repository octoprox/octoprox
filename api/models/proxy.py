# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy model definitions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now
from api.core.stats import MetricDelta, success_rate
from api.geo.models import (
    MANUAL_SOURCE,
    META_CITY_VERDICT,
    META_COUNTRY,
    META_COUNTRY_SOURCE,
    META_DISCOVERED_IP,
    META_DYNAMIC_SESSIONS,
    META_ENDPOINT_CITY,
    META_ENDPOINT_COUNTRY,
    META_ENDPOINT_STATE,
    META_GEO_CITY,
    META_GEO_COUNTRY,
    META_GEO_STATE,
    META_LOCATION,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_SESSION_GENERATION,
    META_STATE_VERDICT,
    META_VENDOR_CITY,
    META_VENDOR_COUNTRY,
    META_VENDOR_STATE,
    LocationVerdict,
    normalize_country,
)
from api.models.audit import Audited, AuditEntityType
from api.models.location import (
    META_MANUAL_LOCATION,
    LocationTarget,
    normalize_country_code,
    normalize_state_code,
    slugify_place,
)
from api.reputation.site import canonical_ip

if TYPE_CHECKING:
    from api.models.connector import Connector


def metadata_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """What ``after`` changed of the metadata ``before``, key by key: each key it sets anew with its value, and the keys it drops."""
    changed = {key: value for key, value in after.items() if key not in before or before[key] != value}
    return changed, {key for key in before if key not in after}


class ProxyProtocol(StrEnum):
    """Supported proxy protocols."""
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


def proxy_url(protocol: str, host: str, port: int, username: str | None, password: str | None) -> str:
    """A proxy URL whose credentials every client that parses it gets back exactly as stored.

    They are escaped: a URL parser decodes ``%xx`` in them and ends them at ``/``, ``?`` or ``#``.
    """
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username and password else ""
    return f"{protocol}://{auth}{host}:{port}"


class ProxyStatus(StrEnum):
    """Proxy health status."""
    UNKNOWN = "unknown"
    INITIALIZING = "initializing"  # Newly created, still starting up (grace period for health checks)
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"  # Not accepting new connections, waiting for existing to complete
    TERMINATING = "terminating"  # Being terminated/removed

    @property
    def retiring(self) -> bool:
        """Whether a proxy in this status is on its way out: draining or terminating."""
        return self in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING)


class Proxy(Audited):
    """Represents a proxy server.

    Its entries record what a person sets (``audit_fields``); the service names the project, which the row does not hold.
    """

    audit_type: ClassVar[AuditEntityType] = AuditEntityType.PROXY
    audit_masked: ClassVar[frozenset[str]] = frozenset({"password"})

    id: str = Field(default_factory=lambda: str(uuid4()))
    host: str
    port: int
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: str | None = None
    password: str | None = None

    # Display host - for UI display purposes
    # For most proxies this equals host, but for Oxylabs port-based proxies
    # this contains the discovered IP while host keeps the Oxylabs endpoint for routing.
    # Attribution no longer writes it; see effective_display_host.
    display_host: str | None = None

    # Status and health. ``status`` is a column of the proxies table: stored
    # with the row when it is added, then changed only by the health job's
    # compare-and-set and by drain and terminate, never by an edit.
    status: ProxyStatus = ProxyStatus.UNKNOWN
    # When ``status`` last changed; None until it first does.
    status_changed_at: datetime | None = None

    # Metadata
    connector_id: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def audit_name(self) -> str:
        return f"{self.host}:{self.port}"

    def audit_fields(self) -> dict[str, Any]:
        """The definition and tags a person sets, and the country, state and city pinned by hand; never what health
        checks, syncs or attribution write."""
        fields = self.model_dump(mode="json", include={"host", "port", "protocol", "username", "password", "tags"})
        pinned = self.manual_location
        fields["country"] = self.country if self.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE else None
        fields["state"] = pinned.get("state_code")
        fields["city"] = pinned.get("city")
        return fields

    @property
    def url(self) -> str:
        """The proxy URL, its credentials escaped (``proxy_url``)."""
        return proxy_url(self.protocol, self.host, self.port, self.username, self.password)

    @property
    def effective_display_host(self) -> str:
        """The host the UI shows: the exit attribution last saw, else ``display_host``, else ``host``.

        Attribution writes the exit into the metadata alone (``discovered_ip``), so the newest sighting is what shows.
        """
        discovered = self.metadata.get(META_DISCOVERED_IP)
        if isinstance(discovered, str) and discovered:
            return discovered
        return self.display_host if self.display_host else self.host

    @property
    def country(self) -> str | None:
        """Exit country (upper-case ISO code) if known.

        Prefers the country discovered or listed by the vendor (``metadata.country``)
        over the country the slot was provisioned for (``metadata.geo``).
        """
        for key in (META_COUNTRY, META_GEO_COUNTRY):
            value = self.metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
        return None

    @property
    def manual_location(self) -> dict[str, str]:
        """State code and city pinned by hand (``metadata.manual_location``), empty when none."""
        value = self.metadata.get(META_MANUAL_LOCATION)
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(v, str) and v}

    def _place_level(self, key: str, source_key: str, pin_key: str, location_key: str) -> tuple[str | None, str | None]:
        """``(value, source)`` for the state or city: the pin, else what attribution resolved, else the databases' record."""
        pinned = self.manual_location.get(pin_key)
        if pinned:
            return pinned, "manual"
        resolved = self.metadata.get(key)
        if resolved:
            return resolved, self.metadata.get(source_key) or None
        location = self.metadata.get("location")
        value = location.get(location_key) if isinstance(location, dict) else None
        return (value, "database") if value else (None, None)

    @property
    def state_code(self) -> str | None:
        """ISO 3166-2 subdivision part of the exit's state: pinned by hand, else what attribution resolved."""
        return self._place_level("state_code", "state_source", "state_code", "state_code")[0]

    @property
    def state_source(self) -> str | None:
        """Where the state comes from: ``manual``, or the attribution source that resolved it."""
        return self._place_level("state_code", "state_source", "state_code", "state_code")[1]

    @property
    def city_slug(self) -> str | None:
        """The exit's city as a slug: pinned by hand, else what attribution resolved.

        The databases' record holds the name, so only that fallback is slugified.
        """
        value, source = self._place_level("city", "city_source", "city", "city")
        return slugify_place(value) if source == "database" and value else value

    @property
    def city_source(self) -> str | None:
        """Where the city comes from: ``manual``, or the attribution source that resolved it."""
        return self._place_level("city", "city_source", "city", "city")[1]

    def location_matches(self, target: LocationTarget) -> bool:
        """Whether this fixed exit is known to be in the state and city ``target`` names.

        The country is matched by the routing layer on ``country``; this
        checks the levels below it. An exit whose state or city is unknown
        does not match a request that names one: a request is never sent
        somewhere it might not be.
        """
        if target.state and self.state_code != target.state:
            return False
        return not target.city or self.city_slug == target.city

    @property
    def is_dynamic_gateway(self) -> bool:
        """Whether this is the gateway row of a dynamic-sessions connector."""
        return self.metadata.get(META_DYNAMIC_SESSIONS) == "true"

    @property
    def session_generation(self) -> int:
        """How many times the client session this proxy was rendered for has moved to a new vendor session; 0 when never."""
        return int(self.metadata.get(META_SESSION_GENERATION) or 0)

    @property
    def static_exit_ip(self) -> str | None:
        """The exit IP exit reputation knows a stored row by: where it was last seen exiting, as canonical text.

        None for a row never seen exiting, and for a dynamic gateway, whose exit is chosen per request.
        """
        ip = self.metadata.get(META_DISCOVERED_IP)
        if not isinstance(ip, str) or self.is_dynamic_gateway:
            return None
        return canonical_ip(ip)

    @property
    def claimed_country(self) -> str | None:
        """The vendor's claim for this proxy: its recorded claim, else the geo it was provisioned for."""
        for key in (META_VENDOR_COUNTRY, META_GEO_COUNTRY):
            code = normalize_country(self.metadata.get(key))
            if code:
                return code
        return None

    def outside_declared_countries(self, declared: Sequence[str]) -> bool:
        """Whether a row the vendor placed in a declared country now resolves outside the connector's list: routing never uses it.

        A row that made no claim, or whose country an operator pinned, is never outside it here.
        """
        if self.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            return False
        return bool(declared) and self.country is not None and self.country not in declared and self.claimed_country is not None

    @property
    def claimed_place(self) -> LocationTarget | None:
        """Everything promised about this proxy's exit, per level: what attribution judges and preflight verifies against.

        The vendor's word or the slot's provisioned place, else what an operator pinned, each normalised when written.
        """
        manual = self.manual_location
        metadata = self.metadata
        country = self.claimed_country
        if country is None and metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            country = metadata.get(META_COUNTRY)
        return LocationTarget(
            country=country or None,
            state=metadata.get(META_VENDOR_STATE) or metadata.get(META_GEO_STATE) or manual.get("state_code") or None,
            city=metadata.get(META_VENDOR_CITY) or metadata.get(META_GEO_CITY) or manual.get("city") or None,
        ) or None

    @property
    def endpoint_place(self) -> LocationTarget | None:
        """What a third-party endpoint last reported about this proxy's exit, per level."""
        metadata = self.metadata
        return LocationTarget(
            country=metadata.get(META_ENDPOINT_COUNTRY) or None,
            state=metadata.get(META_ENDPOINT_STATE) or None,
            city=metadata.get(META_ENDPOINT_CITY) or None,
        ) or None


class ProxyCreate(BaseModel):
    """Schema for creating a new proxy."""
    host: str
    port: int
    connector_id: str
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: str | None = None
    password: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Exit country (ISO code). When omitted the exit location is looked up through the proxy.
    country: str | None = None
    # Exit state (ISO 3166-2 subdivision part, "NY") and city, pinned by hand
    # for -st- and -city- routing when the databases place the IP wrongly.
    state: str | None = None
    city: str | None = None

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        return normalize_country_code(value)

    @field_validator("state")
    @classmethod
    def _state(cls, value: str | None) -> str | None:
        return normalize_state_code(value)

    @field_validator("city")
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        return slugify_place(value)


class ProxyUpdate(BaseModel):
    """Schema for updating a proxy."""
    host: str | None = None
    port: int | None = None
    protocol: ProxyProtocol | None = None
    username: str | None = None
    password: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    # Exit country (ISO code); an empty string clears it.
    country: str | None = None
    # Exit state code and city pinned by hand; an empty string clears each.
    state: str | None = None
    city: str | None = None

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        # An empty string clears the country; None leaves it untouched.
        if value is not None and value.strip() == "":
            return ""
        return normalize_country_code(value)

    @field_validator("state")
    @classmethod
    def _state(cls, value: str | None) -> str | None:
        if value is not None and value.strip() == "":
            return ""
        return normalize_state_code(value)

    @field_validator("city")
    @classmethod
    def _city(cls, value: str | None) -> str | None:
        if value is not None and value.strip() == "":
            return ""
        return slugify_place(value)


class HealthSoft(BaseModel):
    """A proxy's soft health between checks, what the next check builds on and the api shows; the status is a Postgres column.

    Stored as the hash ``PROXY_HEALTH_KEY``, one field per model field as
    ``model_dump(mode="json")``, expiring after ``PROXY_HEALTH_TTL_SECONDS``.
    """

    # Failed checks in a row; 0 after a success.
    failures: int = 0
    # How long the last check took, in milliseconds.
    latency_ms: float = 0.0
    # When the last check ran (naive UTC).
    checked_at: datetime


@dataclass(frozen=True)
class ProxyView:
    """A proxy row with its runtime state."""

    # The stored row: its definition and its status column.
    proxy: Proxy
    # Its connector; None only while a delete is under way.
    connector: "Connector | None"
    # All-time totals plus the window not flushed yet.
    counters: MetricDelta
    # Failures in a row and latency from the last health check; None when not checked lately.
    health: HealthSoft | None
    # Seconds left of a rate-limit quarantine, 0 when none.
    quarantine_remaining_seconds: float

    @classmethod
    def fresh(cls, proxy: Proxy, connector: "Connector | None") -> "ProxyView":
        """A row added a moment ago as the read model would show it: nothing counted, no quarantine, never checked."""
        return cls(
            proxy=proxy, connector=connector, counters=MetricDelta(), health=None, quarantine_remaining_seconds=0.0
        )

    @property
    def quarantined(self) -> bool:
        """Whether a rate limit has the proxy in quarantine now."""
        return self.quarantine_remaining_seconds > 0

    @property
    def outside_declared_countries(self) -> bool:
        """Whether the vendor placed the exit in a country its connector declares and it now resolves outside them."""
        return self.proxy.outside_declared_countries(self.connector.countries if self.connector else [])

    @property
    def exit_ip(self) -> str | None:
        """The IP the row's exit reputation is kept under, so the view and the routing never disagree."""
        return self.proxy.static_exit_ip


@dataclass(frozen=True)
class ProxyListView:
    """A project's proxies, every one, with how many requests can be routed to."""

    proxies: list[ProxyView]
    # Healthy, not quarantined, on an enabled connector no traffic limit blocks.
    healthy: int


@dataclass(frozen=True)
class UploadError:
    """One line of an upload that did not become a proxy, and why."""

    line_number: int
    line: str
    error: str


@dataclass(frozen=True)
class ProxyUpload:
    """What an upload did: the proxies it added to its connector and the lines it could not read."""

    # Non-empty lines in the file.
    total_lines: int
    # The static connector the proxies were added to, as the upload's transaction read it.
    connector: "Connector"
    added: list[Proxy] = field(default_factory=list)
    errors: list[UploadError] = field(default_factory=list)

    @property
    def views(self) -> list[ProxyView]:
        """The added proxies as the read model would show them now: nothing counted, no quarantine, never checked."""
        return [ProxyView.fresh(proxy, self.connector) for proxy in self.added]


class ProxyResponse(BaseModel):
    """Schema for proxy API responses."""
    id: str
    host: str
    port: int
    protocol: str
    username: str | None = None
    password: str | None = None
    display_host: str  # The host to display in UI (falls back to host if not set)
    connector_id: str
    connector_name: str | None = None
    connector_enabled: bool = True
    status: str
    request_count: int
    success_count: int
    failure_count: int
    success_rate: float
    avg_latency_ms: float
    bytes_sent: int = 0
    bytes_received: int = 0
    quarantined: bool = False
    quarantine_remaining_seconds: float = 0.0
    country: str | None = None  # Exit country (ISO code) when discovered or provisioned per geo
    # IP attribution: which source produced ``country``, what the vendor
    # claimed, whether attribution contradicts that claim, and the full
    # location record from the databases (see docs/ip-attribution.md).
    country_source: str | None = None
    vendor_country: str | None = None
    location_conflict: bool = False
    # confirmed, contradicted or open; None when the vendor claimed nothing
    # or the exit was not judged yet. The project's location decisions say
    # what a contradicted or an open claim does to the row.
    location_verdict: LocationVerdict | None = None
    state_verdict: LocationVerdict | None = None
    city_verdict: LocationVerdict | None = None
    # The vendor placed the exit in one of the connector's declared countries
    # and the evidence puts it outside them: routing skips the row for every
    # request, and a pooled slot is kept rather than replaced.
    outside_declared_countries: bool = False
    location: dict[str, Any] | None = None
    # State code and city that -st- and -city- routing matches this exit on,
    # each with its source: manual when pinned by hand, else the attribution
    # source (database, vendor, endpoint) the policy let decide.
    state_code: str | None = None
    state_source: str | None = None
    city: str | None = None
    city_source: str | None = None
    # The IP the row's exit reputation is kept under: its last seen exit, as
    # one canonical text. None on a dynamic gateway, whose every request
    # exits elsewhere, and until the exit is first seen.
    exit_ip: str | None = None
    tags: list[str]
    created_at: datetime

    @classmethod
    def of(cls, view: 'ProxyView') -> 'ProxyResponse':
        """A proxy as the API shows it: the stored row and its connector, its counters and its quarantine."""
        proxy, connector, counters = view.proxy, view.connector, view.counters
        location = proxy.metadata.get(META_LOCATION)
        return cls(
            id=proxy.id,
            host=proxy.host,
            port=proxy.port,
            protocol=proxy.protocol.value,
            username=proxy.username,
            password=proxy.password,
            display_host=proxy.effective_display_host,
            connector_id=proxy.connector_id,
            connector_name=connector.name if connector else None,
            connector_enabled=connector.enabled if connector else True,
            status=proxy.status.value,
            request_count=counters.request_count,
            success_count=counters.success_count,
            failure_count=counters.failure_count,
            success_rate=success_rate(counters),
            avg_latency_ms=counters.avg_latency_ms,
            bytes_sent=counters.bytes_sent,
            bytes_received=counters.bytes_received,
            quarantined=view.quarantined,
            quarantine_remaining_seconds=round(view.quarantine_remaining_seconds, 1),
            country=proxy.country,
            country_source=proxy.metadata.get(META_COUNTRY_SOURCE),
            vendor_country=proxy.metadata.get(META_VENDOR_COUNTRY),
            location_conflict=bool(proxy.metadata.get(META_LOCATION_CONFLICT)),
            location_verdict=proxy.metadata.get(META_LOCATION_VERDICT),
            state_verdict=proxy.metadata.get(META_STATE_VERDICT),
            city_verdict=proxy.metadata.get(META_CITY_VERDICT),
            outside_declared_countries=view.outside_declared_countries,
            location=location if isinstance(location, dict) else None,
            state_code=proxy.state_code,
            state_source=proxy.state_source,
            city=proxy.city_slug,
            city_source=proxy.city_source,
            exit_ip=view.exit_ip,
            tags=proxy.tags,
            created_at=proxy.created_at,
        )


class ProxyListResponse(BaseModel):
    """Response for listing proxies."""
    total: int
    healthy: int
    proxies: list[ProxyResponse]

    @classmethod
    def of(cls, listing: 'ProxyListView') -> 'ProxyListResponse':
        """The project's proxies as listed, in the order given, with how many requests can be routed to."""
        return cls(
            total=len(listing.proxies),
            healthy=listing.healthy,
            proxies=[ProxyResponse.of(view) for view in listing.proxies],
        )


class ProxyUploadError(BaseModel):
    """Error details for a failed proxy upload line."""
    line_number: int
    line: str
    error: str

    @classmethod
    def of(cls, error: 'UploadError') -> 'ProxyUploadError':
        """A line of an upload that did not become a proxy, and why."""
        return cls(line_number=error.line_number, line=error.line, error=error.error)


class ProxyUploadResponse(BaseModel):
    """Response for proxy upload endpoint."""
    total_lines: int
    successful: int
    failed: int
    proxies: list[ProxyResponse]
    errors: list[ProxyUploadError]

    @classmethod
    def of(cls, upload: 'ProxyUpload') -> 'ProxyUploadResponse':
        """What an upload did: the proxies it added, as a read right after shows them, and the lines it skipped."""
        return cls(
            total_lines=upload.total_lines,
            successful=len(upload.added),
            failed=len(upload.errors),
            proxies=[ProxyResponse.of(view) for view in upload.views],
            errors=[ProxyUploadError.of(error) for error in upload.errors],
        )

