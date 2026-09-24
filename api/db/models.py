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
from api.models.proxy import ProxyProtocol


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
    location_policy: Mapped[str] = mapped_column(String(10), nullable=False, default="off", server_default="off")
    location_preflight: Mapped[str] = mapped_column(String(10), nullable=False, default="off", server_default="off")
    # Null inherits the install default from geo_settings.
    location_sources: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    location_conflict_rule: Mapped[str | None] = mapped_column(String(10), nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

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
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
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
    rate_limit_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
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
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
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


class SystemMetricsModel(Base):
    """Periodic install-wide gauge readings, for the admin trend charts.

    Distinct from the other two metrics tables in three ways, all consequences
    of these being gauges rather than counters:

    * One row per snapshot for the whole install, not per entity - so the
      volume is small enough that retention alone replaces compaction tiers.
    * Downsampling a range AVERAGES these columns. Summing a database size
      across a bucket would be meaningless.
    * There is no ``granularity`` column, because rows are never rewritten
      into coarser ones.
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


class GeoSettingsModel(Base):
    """The install-wide IP attribution settings: exactly one row, id 1.

    Typed columns rather than a settings blob, so the schema says what exists.
    The config file's ``geo.defaults`` seeds a fresh install; an admin's save
    writes this row and reaches every instance through ``geo_settings_changed``.
    """

    __tablename__ = "geo_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    default_conflict_rule: Mapped[str] = mapped_column(String(10), nullable=False, default="consensus")
    echo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    echo_ip_path: Mapped[str] = mapped_column(String(255), nullable=False, default="ip")
    echo_country_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    """One sighting of an exit IP and what every source said about it.

    No foreign keys on purpose: this is the short-lived raw history and it
    must survive a proxy or connector being removed, so what a deleted
    connector did stays traceable for the retention window. The two
    aggregates below are keyed by connector and cascade with it instead.
    """

    __tablename__ = "ip_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    proxy_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    connector_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    claimed_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    endpoint_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolved_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    resolved_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disagreement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class ConnectorExitIpModel(Base):
    """Every distinct exit IP a connector has handed out, and how often.

    One row per (connector, IP), upserted by the observation flusher. Unlike
    the raw observations this is kept until ``exit_ip_retention_days`` after
    the IP was last seen (forever by default), so "how many distinct exits
    has this pool given us" survives the raw retention window. ``sightings``
    counts how many times the IP was newly handed out, not minutes in use.
    Rows go with their connector: the flusher drops sightings of connectors
    deleted since they were made, so the cascade never fails a batch.
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
    conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disagreement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Provider accuracy groups a connector's exits by when they were last seen.
    __table_args__ = (Index("ix_connector_exit_ips_connector_last_seen", "connector_id", "last_seen"),)
