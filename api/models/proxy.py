# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now
from api.models.location import (
    META_MANUAL_LOCATION,
    LocationTarget,
    normalize_country_code,
    normalize_state_code,
    slugify_place,
)


class ProxyProtocol(str, Enum):
    """Supported proxy protocols."""
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class ProxyStatus(str, Enum):
    """Proxy health status."""
    UNKNOWN = "unknown"
    INITIALIZING = "initializing"  # Newly created, still starting up (grace period for health checks)
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"  # Not accepting new connections, waiting for existing to complete
    TERMINATING = "terminating"  # Being terminated/removed


class Proxy(BaseModel):
    """Represents a proxy server."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    host: str
    port: int
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: str | None = None
    password: str | None = None

    # Display host - for UI display purposes
    # For most proxies this equals host, but for Oxylabs port-based proxies
    # this contains the discovered IP while host keeps the Oxylabs endpoint for routing
    display_host: str | None = None

    # Status and health
    status: ProxyStatus = ProxyStatus.UNKNOWN
    consecutive_failures: int = 0
    last_check_latency_ms: float = 0.0

    # Statistics
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0

    # Metadata
    connector_id: str
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def url(self) -> str:
        """Get the proxy URL."""
        auth = ""
        if self.username and self.password:
            auth = f"{self.username}:{self.password}@"
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @property
    def effective_display_host(self) -> str:
        """Get the host to display in UI. Falls back to host if display_host is not set."""
        return self.display_host if self.display_host else self.host

    @property
    def country(self) -> str | None:
        """Exit country (upper-case ISO code) if known.

        Prefers the country discovered or listed by the vendor (``metadata.country``)
        over the country the slot was provisioned for (``metadata.geo``).
        """
        for key in ("country", "geo"):
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
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.request_count == 0:
            return 0.0
        return (self.success_count / self.request_count) * 100

    def apply_status_snapshot(self, status_data: dict[str, Any]) -> None:
        """Adopt the health fields from a Redis ``proxy:status:<id>`` hash.

        Redis - not Postgres - is authoritative for these three: the health
        checker writes them there on every probe and they are never persisted
        to the proxies table. Anything that refreshes a cached proxy therefore
        takes its status from here, and a peer's health flip needs no database
        read at all.
        """
        self.status = status_data["status"]
        self.last_check_latency_ms = status_data["latency_ms"]
        self.consecutive_failures = status_data["consecutive_failures"]

    def merge_definition_from(self, other: "Proxy") -> None:
        """Adopt the Postgres-backed *definition* fields from ``other``.

        Used by ``ProxyManager.reload_proxy`` and ``full_reload`` to apply
        a fresh-from-DB ``Proxy`` to the existing cached instance without
        clobbering runtime state - ``status``, ``last_check_latency_ms``,
        ``consecutive_failures`` (refreshed from Redis), and the
        per-request counters (``request_count`` and friends, incremented
        in-process and reconciled with Redis on the next hydrate).

        Adding a new column to the proxies table? Add the field here
        too. Adding a new runtime-only field? Leave it out.
        """
        self.host = other.host
        self.port = other.port
        self.protocol = other.protocol
        self.username = other.username
        self.password = other.password
        self.display_host = other.display_host
        self.connector_id = other.connector_id
        self.tags = other.tags
        self.metadata = other.metadata
        self.created_at = other.created_at
        self.updated_at = other.updated_at


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
    location: dict[str, Any] | None = None
    # State code and city that -st- and -city- routing matches this exit on,
    # each with its source: manual when pinned by hand, else the attribution
    # source (database, vendor, endpoint) the policy let decide.
    state_code: str | None = None
    state_source: str | None = None
    city: str | None = None
    city_source: str | None = None
    tags: list[str]
    created_at: datetime

