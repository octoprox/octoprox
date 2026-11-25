# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy database models for Octoprox."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.core import utc_now
from api.db.base import Base
from api.models.proxy import ProxyProtocol, ProxyStatus

# The characters a connector's last cloud error keeps.
LAST_ERROR_CHARS = 1024


class UserModel(Base):
    """SQLAlchemy model for user accounts."""

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email_unique", "email", unique=True, postgresql_where=text("email != ''")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    # ``person``, or ``service`` for an account that only ever holds API tokens (api.models.user.UserKind).
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="person", server_default="person")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invite_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invite_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    theme_preference: Mapped[str] = mapped_column(
        String(32), nullable=False, default="light", server_default="light"
    )
    # Set whenever a token is issued (login or invite acceptance). Null until then.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The release whose What's New the user last opened (api.core.releases). Null until then.
    last_seen_release: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AuthProviderModel(Base):
    """A sign-in provider the admin has set up (see api.sso): one row per kind.

    The OAuth client this install registered with Google, Microsoft or GitHub, kept in the database so every instance
    presents the same client and the admin can change it without a redeploy.
    """

    __tablename__ = "auth_providers"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    client_secret: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Microsoft only: the Entra tenant whose accounts may sign in.
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AuthIdentityModel(Base):
    """A provider account a user signs in with: the provider's stable id for it."""

    __tablename__ = "auth_identities"
    __table_args__ = (
        Index("ix_auth_identities_provider_subject_unique", "provider", "subject", unique=True),
        # Also serves lookups by user_id alone, as its leading column.
        Index("ix_auth_identities_user_provider_unique", "user_id", "provider", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    # What the provider reported when the identity was linked; display only.
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiTokenModel(Base):
    """A personal access token: the hash of the secret, never the secret."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # The admin who issued it on the user's behalf, or the user themselves.
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProjectModel(Base):
    """SQLAlchemy model for projects (multi-tenancy)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")

    # Proxy authentication credentials (plain text)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)

    # Project-level settings
    routing_strategy: Mapped[str] = mapped_column(String(50), default="round_robin")
    connection_timeout: Mapped[int] = mapped_column(Integer, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    tls_mitm_mode: Mapped[str] = mapped_column(String(20), default="off")
    tls_mitm_engine: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tls_mitm_browser: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Metrics settings
    metrics_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    # IP attribution: what a contradicted vendor location does to a proxy, and
    # whether a session's exit is verified before its first request.
    location_contradicted: Mapped[str] = mapped_column(String(10), nullable=False, default="evidence", server_default="evidence")
    location_unknown: Mapped[str] = mapped_column(String(10), nullable=False, default="vendor", server_default="vendor")
    location_preflight: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Whether a client session preflight refused moves to a new exit instead of being refused.
    location_session_move: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Null inherits the install default from geo_settings.
    location_conflict_rule: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Km from a claimed city that still confirms it; null inherits geo_settings.default_city_radius_km.
    location_city_radius_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Whether clients are told each request's exit IP in X-Octoprox-* response headers.
    exit_headers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Whether the project's sightings get a row in ip_observations, and for how
    # many days (0 forever); null days inherit geo_settings.observation_retention_days.
    observation_log: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    observation_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Exit reputation: what exit reports do (api.models.project.ExitReputationMode), how many minutes a first avoid
    # lasts, an exit IP record's half-life in hours, a connector record's in requests, the fresh bad reports that avoid
    # an exit for a site, the least bad share in percent, the strength dynamic connectors are compared at, the lowest
    # factor in percent, and the days the project's records and Over time history are kept after last use (0 forever).
    exit_reputation: Mapped[str] = mapped_column(String(16), nullable=False, default="off", server_default="off")
    exit_reputation_avoid_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    exit_reputation_half_life_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24, server_default="24")
    exit_reputation_connector_half_life_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    exit_reputation_bad_reports: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    exit_reputation_bad_share: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    exit_reputation_strength: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    exit_reputation_floor: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    exit_reputation_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    # Set by a delete; the project goes once no connector holds it (its cloud instances terminated).
    pending_deletion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Needs attention lines muted by the project's editors, by line key (api.attention.mutes); never read into the
    # Project model, so the routing feed never carries it.
    attention_mutes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The project's alert cut-offs that differ from the defaults (api.models.alerts.AlertSettings); never read into the
    # Project model, so the routing feed never carries it.
    alert_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    credentials: Mapped[list["CredentialModel"]] = relationship("CredentialModel", back_populates="project", cascade="all, delete-orphan")
    connectors: Mapped[list["ConnectorModel"]] = relationship("ConnectorModel", back_populates="project", cascade="all, delete-orphan")


class CredentialModel(Base):
    """SQLAlchemy model for provider credentials."""

    __tablename__ = "credentials"
    __table_args__ = (
        # Names are how connectors and the UI refer to a credential: unique per project, ignoring case.
        Index("ix_credentials_project_name_unique", "project_id", text("lower(name)"), unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Foreign key to project
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)

    # Relationships
    project: Mapped["ProjectModel"] = relationship("ProjectModel", back_populates="credentials")
    connectors: Mapped[list["ConnectorModel"]] = relationship("ConnectorModel", back_populates="credential", cascade="all, delete-orphan")


class ConnectorModel(Base):
    """SQLAlchemy model for connectors (replaces sources)."""

    __tablename__ = "connectors"
    __table_args__ = (
        Index("ix_connectors_project_name_unique", "project_id", text("lower(name)"), unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pending_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cloud provider error tracking
    last_error: Mapped[str | None] = mapped_column(String(LAST_ERROR_CHARS), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    routing_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    traffic_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rate_limit_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health_check_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Foreign keys
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), ForeignKey("credentials.id"), nullable=False)

    # Relationships
    project: Mapped["ProjectModel"] = relationship("ProjectModel", back_populates="connectors")
    credential: Mapped["CredentialModel"] = relationship("CredentialModel", back_populates="connectors")
    proxies: Mapped[list["ProxyModel"]] = relationship("ProxyModel", back_populates="connector", cascade="all, delete-orphan")


class ProxyModel(Base):
    """SQLAlchemy model for proxies."""

    __tablename__ = "proxies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(20), default=ProxyProtocol.HTTP.value)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connector_id: Mapped[str] = mapped_column(String(36), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # Health, draining or terminating (a ProxyStatus value), which the routing
    # feed carries. Inserted with the builder's status, then written only by
    # the health job's compare-and-set and by drain and terminate, never by an
    # edit (ProxyRepository.set_status_if / set_status).
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProxyStatus.UNKNOWN.value, server_default=ProxyStatus.UNKNOWN.value
    )
    # When the status last changed; null until it first does.
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship to connector
    connector: Mapped["ConnectorModel"] = relationship("ConnectorModel", back_populates="proxies")


class ProxyMetricsModel(Base):
    """SQLAlchemy model for historical proxy metrics (flushed from Redis)."""

    __tablename__ = "proxy_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proxy_id: Mapped[str] = mapped_column(String(36), ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    # Snapshot of metrics at flush time
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class StepLatencyColumns:
    """Each step's average latency over a history row's successful requests (``StepMetricDelta``), in milliseconds.

    NULL in the rows written before steps were kept; the steps a request skipped count as 0 in the average.
    """

    avg_route_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_preflight_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_connect_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_handshake_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_response_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class ProjectMetricsModel(StepLatencyColumns, Base):
    """SQLAlchemy model for historical project-level metrics (flushed from Redis).

    These metrics persist across proxy rotation, providing aggregate metrics
    at the project level that survive when proxies are deleted or replaced.
    """

    __tablename__ = "project_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    # Snapshot of metrics at flush time
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class ConnectorMetricsModel(StepLatencyColumns, Base):
    """Historical metrics per connector, the unit vendors bill and traffic limits apply to (flushed from Redis).

    The rows outlive the connector's proxies, and have the shape and compaction tiers of the other metrics tables.
    """

    __tablename__ = "connector_metrics"
    __table_args__ = (
        Index("ix_connector_metrics_connector_granularity_ts", "connector_id", "granularity", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    connector_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class TunnelPeerMetricsModel(Base):
    """Historical per-device metrics for the tunnel devices of every protocol (flushed from Redis), keyed by device id.

    No foreign key to the device, whose repository deletes its rows; ``project_id``, copied from the device's row at
    flush time, carries retention and the project cascade.
    """

    __tablename__ = "tunnel_peer_metrics"
    __table_args__ = (
        Index("ix_tunnel_peer_metrics_peer_granularity_ts", "peer_id", "granularity", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    peer_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    by_address: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    encrypted_dns_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class HostMetricsModel(Base):
    """Historical per-host metrics: a project's requests by destination host and connector.

    Each completed request counts against its host (the CONNECT target or the URL host, lower case) under the connector
    that carried it, and rows cascade with the connector and the project.
    """

    __tablename__ = "host_metrics"
    __table_args__ = (
        Index("ix_host_metrics_project_ts", "project_id", "timestamp"),
        Index("ix_host_metrics_project_granularity_ts", "project_id", "granularity", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    connector_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    request_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class FailureMetricsModel(Base):
    """Historical failure counts for the Failures page: failed requests by project, kind, vendor status and connector.

    Every failure counts, where the failure log keeps only examples, and a request refused before a proxy was chosen has
    no connector.
    """

    __tablename__ = "failure_metrics"
    __table_args__ = (
        Index("ix_failure_metrics_project_ts", "project_id", "timestamp"),
        Index("ix_failure_metrics_project_granularity_ts", "project_id", "granularity", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    connector_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    granularity: Mapped[int] = mapped_column(Integer, default=60, nullable=False)


class MetricTotalsModel(Base):
    """Each proxy's, project's and tunnel device's counters for all time, one row per entity.

    The metrics flush adds every window here in the transaction that writes its history rows, and the api shows these
    plus the window not flushed yet.
    """

    __tablename__ = "metric_totals"

    # proxy, project or tunnel_peer (its metrics window kind's name), and that entity's id.
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # A sum over the successful requests, as in MetricDelta, so windows add; the average is derived.
    latency_sum_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # The two name-resolution counters only tunnel devices carry; zero otherwise.
    by_address: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    encrypted_dns_blocked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # When the latest window was added.
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class MetricFlushModel(Base):
    """A metrics flush Postgres committed, by its job entry's ID: one recorded already only clears its staging hash.

    Recorded in the transaction that writes the flush's history and totals.
    """

    __tablename__ = "metric_flushes"

    # The flush's job stream entry ID, which also names its staging hash in Redis (METRICS_TAKEN_KEY).
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # When it committed (naive UTC); compaction deletes the rows older than a day.
    flushed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class SystemMetricsModel(Base):
    """Periodic install-wide gauge readings for the admin trend charts, one row per snapshot.

    Gauges rather than counters: a range is downsampled by averaging, and retention alone bounds the rows, so there is
    no ``granularity`` column.
    """

    __tablename__ = "system_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)

    # Storage
    database_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    redis_memory_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    redis_keys: Mapped[int] = mapped_column(BigInteger, default=0)

    # Inventory
    projects: Mapped[int] = mapped_column(Integer, default=0)
    credentials: Mapped[int] = mapped_column(Integer, default=0)
    connectors: Mapped[int] = mapped_column(Integer, default=0)
    connectors_enabled: Mapped[int] = mapped_column(Integer, default=0)
    users: Mapped[int] = mapped_column(Integer, default=0)

    # Pool
    proxies_total: Mapped[int] = mapped_column(Integer, default=0)
    proxies_healthy: Mapped[int] = mapped_column(Integer, default=0)
    proxies_unhealthy: Mapped[int] = mapped_column(Integer, default=0)

    # Breakdowns: {table_name: total_bytes} and {status: count}
    table_sizes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proxy_status_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderDescriptorModel(Base):
    """SQLAlchemy model for admin-authored provider descriptors."""

    __tablename__ = "provider_descriptors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AuditLogModel(Base):
    """One user-driven change, or an extension's access event: who acted, on what, when, and the field diff.

    No foreign keys: entries outlive what they describe, so a deleted entity's history stays readable by its id.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        # One entity's history, newest first by a backward scan.
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
        # The install-wide list and the retention delete.
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    # The entity's name at the time, so a deleted entity's entries stay readable.
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # {field: [before, after]}, secrets masked.
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SystemSettingsModel(Base):
    """The install's admin-edited settings: exactly one row, id 1, written by the first save; no row means the defaults."""

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Days the audit log keeps entries; 0 keeps them forever.
    audit_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365, server_default="365")
    # As pasted; only an extension reads it.
    license_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Days install-wide alerts are kept after they resolve; 0 keeps them forever.
    install_alert_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90, server_default="90")


class AlertModel(Base):
    """One alert: a line of a project's Needs attention card, or of the install's, from when it opened until it
    resolved; a project's are deleted with it."""

    __tablename__ = "alerts"
    __table_args__ = (
        # One open alert per scope and key: what makes the alerts job's insert idempotent.
        Index(
            "uq_alerts_open",
            text("coalesce(project_id, '')"),
            "key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # The resolved alerts of a key, newest first: what a returning condition reopens.
        Index(
            "ix_alerts_resolved",
            text("coalesce(project_id, '')"),
            "key",
            "resolved_at",
            postgresql_where=text("resolved_at IS NOT NULL"),
        ),
        # A project's history, newest first.
        Index("ix_alerts_project_opened", "project_id", "opened_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # None for an install alert.
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    # The line's key (api.attention.checks.Finding.key), stable while its cause lasts.
    key: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # Now while open, when it resolved once resolved; the worst it reached beside it.
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    worst_severity: Mapped[str] = mapped_column(String(10), nullable=False)
    lead: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The line's text after the subject, as the API answers it.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The line's one time (api.models.attention.AttentionWhen), none when both are null.
    when_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    when_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    link: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    sort_name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    # The key of the shown line that makes this one redundant; '' while it shows.
    hidden_by: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When it last came back within the reopen window; null until it does.
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reopened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class WireGuardSettingsModel(Base):
    """The install's WireGuard identity and device-config defaults: exactly one row, id 1.

    One key pair for the whole install, kept here rather than in a file, so a device's config stays valid whichever
    instance answers on the endpoint.
    """

    __tablename__ = "wireguard_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    private_key: Mapped[str] = mapped_column(String(44), nullable=False)
    public_key: Mapped[str] = mapped_column(String(44), nullable=False)
    endpoint_host: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    endpoint_port: Mapped[int] = mapped_column(Integer, nullable=False, default=51820, server_default="51820")
    subnet: Mapped[str] = mapped_column(String(43), nullable=False, default="10.66.0.0/16", server_default="10.66.0.0/16")
    persistent_keepalive: Mapped[int] = mapped_column(Integer, nullable=False, default=25, server_default="25")
    client_mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class WireGuardPeerModel(Base):
    """A device allowed into the tunnel, and the project its traffic belongs to.

    The tunnel address is the credential.
    """

    __tablename__ = "wireguard_peers"
    __table_args__ = (
        Index("ix_wireguard_peers_project_name_unique", "project_id", text("lower(name)"), unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    public_key: Mapped[str] = mapped_column(String(44), nullable=False, unique=True)
    # Kept when Octoprox generated the pair, so the config can be shown again;
    # null when the operator brought their own public key.
    private_key: Mapped[str | None] = mapped_column(String(44), nullable=True)
    preshared_key: Mapped[str | None] = mapped_column(String(44), nullable=True)
    address: Mapped[str] = mapped_column(String(45), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # Routing the device cannot express itself: the -sessid- and -cc-/-st-/-city-
    # a proxy client would put in its username.
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # When the device last completed a handshake and from where, as the
    # instance carrying its session last recorded it. Operational, not
    # definition: written by the status publisher, never by an edit, and
    # kept across restarts of the carrying instance (whose interface
    # counters start from zero).
    last_handshake_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_endpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class OpenVpnSettingsModel(Base):
    """The install's OpenVPN identity and device-profile defaults: exactly one row, id 1.

    A private CA, the server certificate it signed and the tls-crypt key,
    kept here for the same reason as the WireGuard key pair: every instance
    terminating the endpoint must present the same identity.
    """

    __tablename__ = "openvpn_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ca_cert: Mapped[str] = mapped_column(Text, nullable=False)
    ca_key: Mapped[str] = mapped_column(Text, nullable=False)
    server_cert: Mapped[str] = mapped_column(Text, nullable=False)
    server_key: Mapped[str] = mapped_column(Text, nullable=False)
    tls_crypt_key: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_host: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    endpoint_port: Mapped[int] = mapped_column(Integer, nullable=False, default=1194, server_default="1194")
    protocol: Mapped[str] = mapped_column(String(3), nullable=False, default="udp", server_default="udp")
    subnet: Mapped[str] = mapped_column(String(43), nullable=False, default="10.67.0.0/16", server_default="10.67.0.0/16")
    keepalive_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    keepalive_timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    client_mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class OpenVpnPeerModel(Base):
    """A device allowed into the OpenVPN endpoint, and the project its traffic belongs to.

    The tunnel address is the credential, as with WireGuard.
    """

    __tablename__ = "openvpn_peers"
    __table_args__ = (
        Index("ix_openvpn_peers_project_name_unique", "project_id", text("lower(name)"), unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    certificate: Mapped[str] = mapped_column(Text, nullable=False)
    private_key: Mapped[str] = mapped_column(Text, nullable=False)
    serial: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Read off the certificate when it is issued, so listing devices never parses PEM.
    certificate_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    address: Mapped[str] = mapped_column(String(45), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # When the device's last session started and from where, as the carrying
    # instance recorded it (operational, see WireGuardPeerModel).
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_endpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class GeoSettingsModel(Base):
    """The install-wide IP attribution settings, as typed columns: exactly one row, id 1.

    The config file's ``geo.defaults`` seeds a fresh install, and an admin's save writes this row and reaches every
    instance as a ``geo_settings`` change.
    """

    __tablename__ = "geo_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_conflict_rule: Mapped[str] = mapped_column(String(10), nullable=False, default="consensus")
    default_city_radius_km: Mapped[float] = mapped_column(Float, nullable=False, default=25.0, server_default="25")
    echo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    echo_ip_path: Mapped[str] = mapped_column(String(255), nullable=False, default="ip")
    echo_country_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    echo_state_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    echo_city_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    echo_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    health_check_attribution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preflight_session_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    preflight_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    observation_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    exit_ip_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class GeoDatabaseModel(Base):
    """An IP database the install knows about. The file itself lives in ``geo_database_blobs``."""

    __tablename__ = "geo_databases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    format: Mapped[str] = mapped_column(String(20), nullable=False, default="mmdb")
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="upload")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    database_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    build_epoch: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    record_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ip_version: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attribution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    update_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    update_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    update_auth: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    update_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_update_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class GeoDatabaseBlobModel(Base):
    """The bytes of a stored database, kept apart so listing databases never reads them."""

    __tablename__ = "geo_database_blobs"

    database_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("geo_databases.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class IpObservationModel(Base):
    """One sighting of an exit IP and what every source said about it, in the short-lived raw history.

    No foreign keys, so what a deleted proxy or connector did stays traceable for the retention window.
    """

    __tablename__ = "ip_observations"
    # The log's project, connector, proxy and IP filters each have an index on
    # the column alone, which stores a repeated value once; autovacuum keeps
    # pace with the hourly retention delete. See migration 049 for why.
    __table_args__ = (
        Index("ix_ip_observations_project_id", "project_id"),
        Index("ix_ip_observations_connector_id", "connector_id"),
        Index("ix_ip_observations_proxy_id", "proxy_id"),
        Index("ix_ip_observations_ip", "ip"),
        Index("ix_ip_observations_contradicted", "observed_at", postgresql_where=text("country_conflict IS TRUE")),
        {"postgresql_with": {"autovacuum_vacuum_scale_factor": 0.02, "autovacuum_vacuum_insert_scale_factor": 0.02}},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    proxy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    connector_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    claimed_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    endpoint_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolved_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolved_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    claimed_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    claimed_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    resolved_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    city_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class ConnectorExitIpModel(Base):
    """Every distinct exit IP a connector has handed out, and how often: one row per (connector, IP).

    Kept until ``exit_ip_retention_days`` after the IP was last seen (forever by default), and ``sightings`` counts how
    many times the IP was newly handed out, not minutes in use.
    """

    __tablename__ = "connector_exit_ips"

    connector_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connectors.id", ondelete="CASCADE"), primary_key=True
    )
    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sightings: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # The country attribution resolved the last time the IP was seen.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # The rest of the latest observation: which proxy held the exit, how it
    # was seen, what the vendor claimed and how the claim was judged. Kept
    # here so the exit IPs view is one small table, not a join into millions
    # of raw rows.
    proxy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    claimed_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolved_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    claimed_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    claimed_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    resolved_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    city_conflict: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Only the primary key is indexed, and new pages keep a fifth free, so the
    # ingest's updates of last_seen and the latest state rewrite a row in place
    # without index writes. See migration 049 for why.
    __table_args__ = ({"postgresql_with": {"fillfactor": 80}},)


class ExitIpReputationModel(Base):
    """What clients reported about one exit IP on one site of a project, and until when it is avoided there.

    Counts are for the record's life; the weights decay with the project's half-life from ``weighted_at``
    (api.reputation.record.ExitIpReputation).
    """

    __tablename__ = "exit_ip_reputation"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    # Canonical text (api.reputation.site.canonical_ip).
    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    # The exact host a request went to, or an IP literal (api.reputation.site.site_of).
    site: Mapped[str] = mapped_column(String(253), primary_key=True)
    good_reports: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    bad_reports: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    weighted_good: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    weighted_bad: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    # The avoids begun for the site, decayed like the reports: how far the next one doubles.
    weighted_avoids: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    weighted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    avoid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # The day of last_seen_at, what the retention sweep selects by.
    last_seen_day: Mapped[date] = mapped_column(Date, nullable=False)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # New pages keep a fifth free so updates rewrite a row in place, and the
    # indexes besides the key are on columns that change seldom: the day at
    # most once a day per row, the avoid's end only when an avoid begins or
    # lifts. See migration 060 for why.
    __table_args__ = (
        Index("ix_exit_ip_reputation_last_seen_day", "last_seen_day"),
        Index("ix_exit_ip_reputation_avoid_until", "avoid_until"),
        {"postgresql_with": {"fillfactor": 80}},
    )


class ConnectorSiteReputationModel(Base):
    """What clients reported about one connector on one site and requested place, against the requests it carried
    there, and the factor its routing weight is multiplied by for them.

    Counts are for the record's life; the weights decay with the requests the metrics flush adds (the project's
    connector half-life) and by the hour from ``weighted_at`` (api.reputation.record.ConnectorSiteReputation). A report
    makes a record; requests only add to one.
    """

    __tablename__ = "connector_site_reputation"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    connector_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connectors.id", ondelete="CASCADE"), primary_key=True
    )
    site: Mapped[str] = mapped_column(String(253), primary_key=True)
    # The level of the requested place the record is kept at (a LocationTarget key: "US", "US/NY",
    # "US/NY/new_york", "US//austin"), "" for requests that named none and for connectors with IPs.
    place: Mapped[str] = mapped_column(String(128), primary_key=True)
    blocked_reports: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    good_reports: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    weighted_blocked: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    weighted_requests: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    weighted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 0.1 to 1 in steps of 0.05; None while too few requests weigh in to tell.
    factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_day: Mapped[date] = mapped_column(Date, nullable=False)

    # As exit_ip_reputation: a fifth of each page free, and besides the key
    # the day index and one on the connector, which never changes. See
    # migration 060 for why.
    __table_args__ = (
        Index("ix_connector_site_reputation_last_seen_day", "last_seen_day"),
        Index("ix_connector_site_reputation_connector_id", "connector_id"),
        {"postgresql_with": {"fillfactor": 80}},
    )


class ConnectorFactorMoveModel(Base):
    """One move of a dynamic connector's stored factor on one site and level of the requested place, as the metrics
    flush decided it, with the record's lifetime counts once it did; the Over time chart draws them.

    No foreign key to the connector: a deleted connector's moves age out with the history setting, and reads keep the
    project's current connectors alone. A project's go with it.
    """

    __tablename__ = "connector_factor_moves"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    site: Mapped[str] = mapped_column(String(253), primary_key=True)
    # The record's level of the requested place, as connector_site_reputation keeps it; "" for none.
    place: Mapped[str] = mapped_column(String(128), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # The flush's decision time (naive UTC).
    at: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    # None while not judged.
    old_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False)
    blocked_reports: Mapped[int] = mapped_column(Integer, nullable=False)

    # Rows are only inserted and deleted; the index on (project_id, at) serves the retention sweep and a project's
    # first move's time.
    # See migration 064 for why.
    __table_args__ = (Index("ix_connector_factor_moves_project_at", "project_id", "at"),)


class ExitIpAvoidMoveModel(Base):
    """One change of an exit IP's avoid on one site, as a batch of reports applied it: the avoid's end before and after
    (null for none); the Over time chart draws the connectors with IPs from them.

    No foreign key to the exit IP's record: the retention sweep deletes records whose avoid has passed, and their moves
    age out with the history setting. A project's go with it.
    """

    __tablename__ = "exit_ip_avoid_moves"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    site: Mapped[str] = mapped_column(String(253), primary_key=True)
    # Canonical text, as exit_ip_reputation keeps it.
    ip: Mapped[str] = mapped_column(String(45), primary_key=True)
    # When the batch was applied (naive UTC).
    at: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    old_avoid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    new_avoid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Rows are only inserted and deleted; the index on (project_id, at) serves the retention sweep and a project's
    # first move's time.
    # See migration 064 for why.
    __table_args__ = (Index("ix_exit_ip_avoid_moves_project_at", "project_id", "at"),)
