"""SQLAlchemy database models for Octoprox."""

from datetime import datetime
from typing import Any

from api.core import utc_now

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db.base import Base
from api.models.proxy import ProxyProtocol


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    credentials: Mapped[list["CredentialModel"]] = relationship("CredentialModel", back_populates="project", cascade="all, delete-orphan")
    connectors: Mapped[list["ConnectorModel"]] = relationship("ConnectorModel", back_populates="project", cascade="all, delete-orphan")


class CredentialModel(Base):
    """SQLAlchemy model for provider credentials."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Foreign key to project
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)

    # Relationships
    project: Mapped["ProjectModel"] = relationship("ProjectModel", back_populates="credentials")
    connectors: Mapped[list["ConnectorModel"]] = relationship("ConnectorModel", back_populates="credential", cascade="all, delete-orphan")


class ConnectorModel(Base):
    """SQLAlchemy model for connectors (replaces sources)."""

    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pending_deletion: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Foreign keys
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
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
    connector_id: Mapped[str] = mapped_column(String(36), ForeignKey("connectors.id"), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
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
    bytes_sent: Mapped[int] = mapped_column(Integer, default=0)
    bytes_received: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="unknown")


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
    bytes_sent: Mapped[int] = mapped_column(Integer, default=0)
    bytes_received: Mapped[int] = mapped_column(Integer, default=0)

