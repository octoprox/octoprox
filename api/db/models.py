# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy database models for Octoprox."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.core import utc_now
from api.db.base import Base
from api.models.proxy import ProxyProtocol, ProxyStatus


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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invite_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invite_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    theme_preference: Mapped[str] = mapped_column(
        String(32), nullable=False, default="light", server_default="light"
    )
    # Set whenever a token is issued (login or invite acceptance). Null until then.
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    health_check_interval: Mapped[int] = mapped_column(Integer, default=60)
    health_check_timeout: Mapped[int] = mapped_column(Integer, default=30)
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
    # Null inherits the install default from geo_settings.
    location_conflict_rule: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Km from a claimed city that still confirms it; null inherits geo_settings.default_city_radius_km.
    location_city_radius_km: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    routing_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    traffic_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    traffic_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rate_limit_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
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


class ProjectMetricsModel(Base):
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


class ConnectorMetricsModel(Base):
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
    # A sum, as in MetricDelta, so windows add; the average is derived.
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


class ProviderAuditModel(Base):
    """SQLAlchemy model for the provider descriptor audit log.

    No foreign key on purpose: history must survive deleting the descriptor.
    """

    __tablename__ = "provider_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    egress_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    hosts_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


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
    # The log reads newest first under one filter at a time, so each index
    # leads with a filter and then the time: a page is a short read however
    # rare the value. Built concurrently by migration 046.
    __table_args__ = (
        Index("ix_ip_observations_project_observed", "project_id", "observed_at"),
        Index("ix_ip_observations_connector_observed", "connector_id", "observed_at"),
        Index("ix_ip_observations_proxy_observed", "proxy_id", "observed_at"),
        Index("ix_ip_observations_ip_observed", "ip", "observed_at"),
        Index("ix_ip_observations_contradicted", "observed_at", postgresql_where=text("country_conflict IS TRUE")),
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
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")


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
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    sightings: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # The country attribution resolved the last time the IP was seen.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # The rest of the latest observation: which proxy held the exit, how it
    # was seen, what the vendor claimed and how the claim was judged. Kept
    # here so the exit IPs view is one indexed table, not a join into millions
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

    # Provider accuracy groups a connector's exits by when they were last seen.
    __table_args__ = (Index("ix_connector_exit_ips_connector_last_seen", "connector_id", "last_seen"),)
