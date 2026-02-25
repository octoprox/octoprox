# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

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
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.request_count == 0:
            return 0.0
        return (self.success_count / self.request_count) * 100


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


class ProxyUpdate(BaseModel):
    """Schema for updating a proxy."""
    host: str | None = None
    port: int | None = None
    protocol: ProxyProtocol | None = None
    username: str | None = None
    password: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


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
    tags: list[str]
    created_at: datetime

