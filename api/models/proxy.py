# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now


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


def _normalize_optional_country(value: str | None) -> str | None:
    """Upper-case ISO code, ``""`` to clear, or None when not provided."""
    if value is None:
        return None
    code = value.strip().upper()
    if code == "":
        return ""
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValueError("country must be a two-letter ISO 3166-1 alpha-2 code")
    return code


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

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        code = _normalize_optional_country(value)
        return code or None


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

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        return _normalize_optional_country(value)


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
    tags: list[str]
    created_at: datetime

