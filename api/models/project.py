# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project model definitions for multi-tenancy."""

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from api.core import utc_now


class MitmMode(str, Enum):
    """TLS MITM interception modes."""

    OFF = "off"
    PLAIN = "plain"
    MATCH_UA = "match_ua"
    OVERRIDE_UA = "override_ua"


class MitmEngine(str, Enum):
    """TLS engines for browser fingerprint impersonation."""

    CURL_CFFI = "curl_cffi"
    RNET = "rnet"


class MitmBrowser(str, Enum):
    """Browser profiles for TLS fingerprint impersonation."""

    CHROME = "chrome"
    FIREFOX = "firefox"
    SAFARI = "safari"
    EDGE = "edge"
    RANDOM = "random"


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

    # Aggregate statistics (persists across proxy rotation)
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    credential_count: int = 0
    connector_count: int = 0
    proxy_count: int = 0
    healthy_proxy_count: int = 0
    created_at: datetime

