# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repository layer for database operations."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import (
    ConnectorModel,
    CredentialModel,
    ProjectMetricsModel,
    ProjectModel,
    ProviderAuditModel,
    ProviderDescriptorModel,
    ProxyMetricsModel,
    ProxyModel,
    UserModel,
)
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import MitmBrowser, MitmEngine, MitmMode, Project
from api.models.provider import ProviderAuditEntry, ProviderRecord
from api.models.proxy import Proxy, ProxyProtocol
from api.models.user import User, UserRole


class ProjectRepository:
    """Repository for project database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Project]:
        """Get all projects."""
        result = await self._session.execute(select(ProjectModel))
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, project_id: str) -> Project | None:
        """Get project by ID."""
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_username(self, username: str) -> Project | None:
        """Get project by username (for proxy authentication)."""
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.username == username)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, project: Project) -> Project:
        """Create a new project."""
        model = ProjectModel(
            id=project.id,
            name=project.name,
            description=project.description,
            username=project.username,
            password=project.password,
            routing_strategy=project.routing_strategy,
            health_check_interval=project.health_check_interval,
            health_check_timeout=project.health_check_timeout,
            connection_timeout=project.connection_timeout,
            max_retries=project.max_retries,
            tls_mitm_mode=project.tls_mitm_mode,
            tls_mitm_engine=project.tls_mitm_engine,
            tls_mitm_browser=project.tls_mitm_browser,
            metrics_retention_days=project.metrics_retention_days,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return project

    async def update(self, project: Project) -> Project:
        """Update an existing project."""
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.id == project.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = project.name
            model.description = project.description
            model.username = project.username
            model.password = project.password
            model.routing_strategy = project.routing_strategy
            model.health_check_interval = project.health_check_interval
            model.health_check_timeout = project.health_check_timeout
            model.connection_timeout = project.connection_timeout
            model.max_retries = project.max_retries
            model.tls_mitm_mode = project.tls_mitm_mode
            model.tls_mitm_engine = project.tls_mitm_engine
            model.tls_mitm_browser = project.tls_mitm_browser
            model.metrics_retention_days = project.metrics_retention_days
            model.updated_at = utc_now()
            await self._session.flush()
        return project

    async def delete(self, project_id: str) -> bool:
        """Delete a project (cascades to sources and proxies)."""
        result = await self._session.execute(
            delete(ProjectModel).where(ProjectModel.id == project_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def get_connector_count(self, project_id: str) -> int:
        """Get the number of connectors for a project."""
        result = await self._session.execute(
            select(func.count(ConnectorModel.id)).where(ConnectorModel.project_id == project_id)
        )
        return result.scalar() or 0

    async def get_proxy_count(self, project_id: str) -> int:
        """Get the number of proxies for a project (via connectors)."""
        result = await self._session.execute(
            select(func.count(ProxyModel.id))
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
        )
        return result.scalar() or 0

    def _to_domain(self, model: ProjectModel) -> Project:
        """Convert database model to domain model."""
        return Project(
            id=model.id,
            name=model.name,
            description=model.description,
            username=model.username,
            password=model.password,
            routing_strategy=model.routing_strategy,
            health_check_interval=model.health_check_interval,
            health_check_timeout=model.health_check_timeout,
            connection_timeout=model.connection_timeout,
            max_retries=model.max_retries,
            tls_mitm_mode=MitmMode(model.tls_mitm_mode),
            tls_mitm_engine=MitmEngine(model.tls_mitm_engine) if model.tls_mitm_engine else None,
            tls_mitm_browser=MitmBrowser(model.tls_mitm_browser) if model.tls_mitm_browser else None,
            metrics_retention_days=model.metrics_retention_days,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CredentialRepository:
    """Repository for credential database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Credential]:
        """Get all credentials, ordered by creation time."""
        result = await self._session.execute(
            select(CredentialModel).order_by(CredentialModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_project(self, project_id: str) -> list[Credential]:
        """Get all credentials for a project, ordered by creation time."""
        result = await self._session.execute(
            select(CredentialModel)
            .where(CredentialModel.project_id == project_id)
            .order_by(CredentialModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, credential_id: str) -> Credential | None:
        """Get credential by ID."""
        result = await self._session.execute(
            select(CredentialModel).where(CredentialModel.id == credential_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, credential: Credential) -> Credential:
        """Create a new credential."""
        model = CredentialModel(
            id=credential.id,
            name=credential.name,
            type=credential.type,
            project_id=credential.project_id,
            config=credential.config,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return credential

    async def update(self, credential: Credential) -> Credential:
        """Update an existing credential."""
        result = await self._session.execute(
            select(CredentialModel).where(CredentialModel.id == credential.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = credential.name
            model.config = credential.config
            model.updated_at = utc_now()
            await self._session.flush()
        return credential

    async def delete(self, credential_id: str) -> bool:
        """Delete a credential."""
        result = await self._session.execute(
            delete(CredentialModel).where(CredentialModel.id == credential_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: CredentialModel) -> Credential:
        """Convert database model to domain model."""
        return Credential(
            id=model.id,
            name=model.name,
            type=model.type,
            project_id=model.project_id,
            config=model.config,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ConnectorRepository:
    """Repository for connector database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Connector]:
        """Get all connectors, ordered by creation time."""
        from sqlalchemy.orm import selectinload
        result = await self._session.execute(
            select(ConnectorModel)
            .options(selectinload(ConnectorModel.credential))
            .order_by(ConnectorModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_project(self, project_id: str) -> list[Connector]:
        """Get all connectors for a project, ordered by creation time."""
        from sqlalchemy.orm import selectinload
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.project_id == project_id)
            .options(selectinload(ConnectorModel.credential))
            .order_by(ConnectorModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, connector_id: str) -> Connector | None:
        """Get connector by ID."""
        from sqlalchemy.orm import selectinload
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.id == connector_id)
            .options(selectinload(ConnectorModel.credential))
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, connector: Connector) -> Connector:
        """Create a new connector."""
        model = ConnectorModel(
            id=connector.id,
            name=connector.name,
            credential_id=connector.credential_id,
            project_id=connector.project_id,
            config=connector.config,
            routing_config=connector.routing_config,
            rate_limit_config=connector.rate_limit_config,
            enabled=connector.enabled,
            pending_deletion=connector.pending_deletion,
            last_error=connector.last_error,
            last_error_at=connector.last_error_at,
            consecutive_errors=connector.consecutive_errors,
            created_at=connector.created_at,
            updated_at=connector.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return connector

    async def update(self, connector: Connector) -> Connector:
        """Update an existing connector."""
        result = await self._session.execute(
            select(ConnectorModel).where(ConnectorModel.id == connector.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = connector.name
            model.credential_id = connector.credential_id
            model.config = connector.config
            model.routing_config = connector.routing_config
            model.rate_limit_config = connector.rate_limit_config
            model.enabled = connector.enabled
            model.pending_deletion = connector.pending_deletion
            model.last_error = connector.last_error
            model.last_error_at = connector.last_error_at
            model.consecutive_errors = connector.consecutive_errors
            model.updated_at = utc_now()
            await self._session.flush()
        return connector

    async def delete(self, connector_id: str) -> bool:
        """Delete a connector."""
        result = await self._session.execute(
            delete(ConnectorModel).where(ConnectorModel.id == connector_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def get_proxy_count(self, connector_id: str) -> int:
        """Get the number of proxies for a connector."""
        result = await self._session.execute(
            select(func.count(ProxyModel.id)).where(ProxyModel.connector_id == connector_id)
        )
        return result.scalar() or 0

    def _to_domain(self, model: ConnectorModel) -> Connector:
        """Convert database model to domain model."""
        return Connector(
            id=model.id,
            name=model.name,
            credential_id=model.credential_id,
            credential_type=model.credential.type,
            project_id=model.project_id,
            config=model.config,
            routing_config=model.routing_config or {},
            rate_limit_config=model.rate_limit_config or {},
            enabled=model.enabled,
            pending_deletion=model.pending_deletion,
            last_error=model.last_error,
            last_error_at=model.last_error_at,
            consecutive_errors=model.consecutive_errors,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProxyRepository:
    """Repository for proxy database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Proxy]:
        """Get all proxies, in creation order."""
        result = await self._session.execute(
            select(ProxyModel).order_by(ProxyModel.created_at, ProxyModel.id)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, proxy_id: str) -> Proxy | None:
        """Get proxy by ID."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.id == proxy_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a connector, in creation order."""
        result = await self._session.execute(
            select(ProxyModel)
            .where(ProxyModel.connector_id == connector_id)
            .order_by(ProxyModel.created_at, ProxyModel.id)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def create(self, proxy: Proxy) -> Proxy:
        """Create a new proxy."""
        model = ProxyModel(
            id=proxy.id,
            host=proxy.host,
            port=proxy.port,
            protocol=proxy.protocol.value if isinstance(proxy.protocol, ProxyProtocol) else proxy.protocol,
            username=proxy.username,
            password=proxy.password,
            display_host=proxy.display_host,
            connector_id=proxy.connector_id,
            tags=proxy.tags,
            metadata_=proxy.metadata,
            created_at=proxy.created_at,
            updated_at=proxy.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return proxy

    async def update(self, proxy: Proxy) -> Proxy:
        """Update an existing proxy."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.id == proxy.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.host = proxy.host
            model.port = proxy.port
            model.protocol = proxy.protocol.value if isinstance(proxy.protocol, ProxyProtocol) else proxy.protocol
            model.username = proxy.username
            model.password = proxy.password
            model.tags = proxy.tags
            model.metadata_ = proxy.metadata
            model.updated_at = utc_now()
            await self._session.flush()
        return proxy

    async def delete(self, proxy_id: str) -> bool:
        """Delete a proxy."""
        result = await self._session.execute(
            delete(ProxyModel).where(ProxyModel.id == proxy_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all proxies for a connector."""
        result = await self._session.execute(
            delete(ProxyModel).where(ProxyModel.connector_id == connector_id)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: ProxyModel) -> Proxy:
        """Convert database model to domain model."""
        return Proxy(
            id=model.id,
            host=model.host,
            port=model.port,
            protocol=ProxyProtocol(model.protocol),
            username=model.username,
            password=model.password,
            display_host=model.display_host,
            connector_id=model.connector_id,
            tags=model.tags or [],
            metadata=model.metadata_ or {},
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


def _strip_tz(dt: datetime) -> datetime:
    """Strip timezone info from a datetime for naive-datetime DB columns.

    PostgreSQL's ``to_timestamp()`` returns ``TIMESTAMP WITH TIME ZONE``,
    but our columns use ``TIMESTAMP WITHOUT TIME ZONE``.
    """
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _bucket_expressions(
    model: type[ProjectMetricsModel] | type[ProxyMetricsModel],
    bucket_seconds: int,
) -> tuple[Any, Any]:
    """Build bucket-epoch and bucket-timestamp expressions for time bucketing.

    Returns (bucket_epoch, bucket_ts) where bucket_epoch is a numeric
    expression suitable for GROUP BY and bucket_ts is a timestamp label.
    """
    bucket = text(str(bucket_seconds))
    ts_col = func.extract("epoch", model.timestamp)
    bucket_epoch = func.floor(ts_col / bucket) * bucket
    bucket_ts = func.to_timestamp(bucket_epoch).label("bucket_ts")
    return bucket_epoch, bucket_ts


def _metrics_aggregate_columns(
    model: type[ProjectMetricsModel] | type[ProxyMetricsModel],
) -> list[Any]:
    """Return the standard aggregate SELECT columns for a metrics model.

    Columns: request_count, success_count, failure_count, avg_latency_ms
    (weighted), bytes_sent, bytes_received.
    """
    weighted_latency = (
        func.sum(model.avg_latency_ms * model.request_count)
        / func.nullif(func.sum(model.request_count), 0)
    ).label("avg_latency_ms")

    return [
        func.sum(model.request_count).label("request_count"),
        func.sum(model.success_count).label("success_count"),
        func.sum(model.failure_count).label("failure_count"),
        weighted_latency,
        func.sum(model.bytes_sent).label("bytes_sent"),
        func.sum(model.bytes_received).label("bytes_received"),
    ]


class MetricsRepository:
    """Repository for metrics database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_metrics_snapshot(
        self,
        proxy_id: str,
        request_count: int,
        success_count: int,
        failure_count: int,
        avg_latency_ms: float,
        status: str,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Save a metrics snapshot to the database."""
        model = ProxyMetricsModel(
            proxy_id=proxy_id,
            timestamp=utc_now(),
            request_count=request_count,
            success_count=success_count,
            failure_count=failure_count,
            avg_latency_ms=avg_latency_ms,
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
            status=status,
        )
        self._session.add(model)
        await self._session.flush()

    async def get_metrics_history(
        self,
        proxy_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get historical metrics for a proxy."""
        query = select(ProxyMetricsModel).where(
            ProxyMetricsModel.proxy_id == proxy_id
        )
        if since:
            query = query.where(ProxyMetricsModel.timestamp >= since)
        query = query.order_by(ProxyMetricsModel.timestamp.desc()).limit(limit)

        result = await self._session.execute(query)
        models = result.scalars().all()
        return [
            {
                "timestamp": m.timestamp,
                "request_count": m.request_count,
                "success_count": m.success_count,
                "failure_count": m.failure_count,
                "avg_latency_ms": m.avg_latency_ms,
                "status": m.status,
                "bytes_sent": m.bytes_sent,
                "bytes_received": m.bytes_received,
            }
            for m in models
        ]

    async def get_cumulative_metrics_for_all_proxies(self) -> dict[str, dict[str, Any]]:
        """Get cumulative metrics (sum of all snapshots) for each proxy.

        Returns a dict mapping proxy_id to its total metrics across all snapshots.
        Used to restore metrics on startup - these totals should be combined with
        the current Redis window.
        """
        # Sum counts and compute weighted average for latency
        # weighted_avg = sum(avg_latency * request_count) / sum(request_count)
        query = (
            select(
                ProxyMetricsModel.proxy_id,
                func.sum(ProxyMetricsModel.request_count).label("total_requests"),
                func.sum(ProxyMetricsModel.success_count).label("total_successes"),
                func.sum(ProxyMetricsModel.failure_count).label("total_failures"),
                func.sum(
                    ProxyMetricsModel.avg_latency_ms * ProxyMetricsModel.request_count
                ).label("weighted_latency_sum"),
                func.sum(ProxyMetricsModel.bytes_sent).label("total_bytes_sent"),
                func.sum(ProxyMetricsModel.bytes_received).label("total_bytes_received"),
            )
            .group_by(ProxyMetricsModel.proxy_id)
        )

        result = await self._session.execute(query)
        rows = result.all()

        metrics = {}
        for row in rows:
            total_requests = int(row.total_requests or 0)
            weighted_latency_sum = float(row.weighted_latency_sum or 0)
            avg_latency = weighted_latency_sum / total_requests if total_requests > 0 else 0.0

            metrics[row.proxy_id] = {
                "request_count": total_requests,
                "success_count": int(row.total_successes or 0),
                "failure_count": int(row.total_failures or 0),
                "avg_latency_ms": float(avg_latency),
                "bytes_sent": int(row.total_bytes_sent or 0),
                "bytes_received": int(row.total_bytes_received or 0),
            }

        return metrics

    # Project-level metrics methods

    async def save_project_metrics_snapshot(
        self,
        project_id: str,
        request_count: int,
        success_count: int,
        failure_count: int,
        avg_latency_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Save a project-level metrics snapshot to the database.

        These metrics persist across proxy rotation.
        """
        model = ProjectMetricsModel(
            project_id=project_id,
            timestamp=utc_now(),
            request_count=request_count,
            success_count=success_count,
            failure_count=failure_count,
            avg_latency_ms=avg_latency_ms,
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
        )
        self._session.add(model)
        await self._session.flush()

    async def get_project_metrics_history(
        self,
        project_id: str,
        since: datetime | None = None,
        limit: int = 100,
        granularity: int = 60,
    ) -> list[dict[str, Any]]:
        """Get historical metrics for a project at a specific granularity."""
        query = select(ProjectMetricsModel).where(
            ProjectMetricsModel.project_id == project_id,
            ProjectMetricsModel.granularity == granularity,
        )
        if since:
            query = query.where(ProjectMetricsModel.timestamp >= since)
        query = query.order_by(ProjectMetricsModel.timestamp.desc()).limit(limit)

        result = await self._session.execute(query)
        models = result.scalars().all()
        return [
            {
                "timestamp": m.timestamp,
                "request_count": m.request_count,
                "success_count": m.success_count,
                "failure_count": m.failure_count,
                "avg_latency_ms": m.avg_latency_ms,
                "bytes_sent": m.bytes_sent,
                "bytes_received": m.bytes_received,
            }
            for m in models
        ]

    async def get_project_metrics_history_aggregated(
        self,
        project_id: str,
        since: datetime,
        bucket_seconds: int,
    ) -> list[dict[str, Any]]:
        """Get historical metrics aggregated into fixed-size time buckets.

        Uses floor division on the epoch to group rows into buckets of
        ``bucket_seconds`` width, then SUMs counters and computes weighted
        average latency.  Rows with granularity <= bucket_seconds are included,
        so pre-compacted rows coexist with recent raw rows.
        """
        bucket_epoch, bucket_ts = _bucket_expressions(ProjectMetricsModel, bucket_seconds)
        agg_cols = _metrics_aggregate_columns(ProjectMetricsModel)

        query = (
            select(bucket_ts, *agg_cols)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.timestamp >= since)
            .where(ProjectMetricsModel.granularity <= bucket_seconds)
            .group_by(bucket_epoch)
            .order_by(bucket_epoch.desc())
        )

        result = await self._session.execute(query)
        rows = result.all()
        return [
            {
                "timestamp": row.bucket_ts,
                "request_count": row.request_count,
                "success_count": row.success_count,
                "failure_count": row.failure_count,
                "avg_latency_ms": float(row.avg_latency_ms or 0),
                "bytes_sent": row.bytes_sent,
                "bytes_received": row.bytes_received,
            }
            for row in rows
        ]

    async def get_cumulative_project_metrics(self) -> dict[str, dict[str, Any]]:
        """Get cumulative metrics (sum of all snapshots) for each project.

        Returns a dict mapping project_id to its total metrics across all snapshots.
        Used to restore metrics on startup - these totals should be combined with
        the current Redis window.
        """
        # Sum counts and compute weighted average for latency
        # weighted_avg = sum(avg_latency * request_count) / sum(request_count)
        query = (
            select(
                ProjectMetricsModel.project_id,
                func.sum(ProjectMetricsModel.request_count).label("total_requests"),
                func.sum(ProjectMetricsModel.success_count).label("total_successes"),
                func.sum(ProjectMetricsModel.failure_count).label("total_failures"),
                func.sum(
                    ProjectMetricsModel.avg_latency_ms * ProjectMetricsModel.request_count
                ).label("weighted_latency_sum"),
                func.sum(ProjectMetricsModel.bytes_sent).label("total_bytes_sent"),
                func.sum(ProjectMetricsModel.bytes_received).label("total_bytes_received"),
            )
            .group_by(ProjectMetricsModel.project_id)
        )

        result = await self._session.execute(query)
        rows = result.all()

        metrics = {}
        for row in rows:
            total_requests = int(row.total_requests or 0)
            weighted_latency_sum = float(row.weighted_latency_sum or 0)
            avg_latency = weighted_latency_sum / total_requests if total_requests > 0 else 0.0

            metrics[row.project_id] = {
                "request_count": total_requests,
                "success_count": int(row.total_successes or 0),
                "failure_count": int(row.total_failures or 0),
                "avg_latency_ms": float(avg_latency),
                "latency_sum_ms": float(weighted_latency_sum),
                "bytes_sent": int(row.total_bytes_sent or 0),
                "bytes_received": int(row.total_bytes_received or 0),
            }

        return metrics

    # Compaction and retention methods

    async def compact_project_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """Compact project metrics from source to target granularity.

        Aggregates source-granularity rows older than ``older_than`` into
        target-granularity buckets, inserts the compacted rows, and deletes
        the originals — all within the current transaction.

        Returns the number of source rows deleted.
        """
        bucket_epoch, bucket_ts = _bucket_expressions(ProjectMetricsModel, target_granularity)
        agg_cols = _metrics_aggregate_columns(ProjectMetricsModel)

        query = (
            select(bucket_ts, *agg_cols)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.granularity == source_granularity)
            .where(ProjectMetricsModel.timestamp < older_than)
            .group_by(bucket_epoch)
        )

        result = await self._session.execute(query)
        buckets = result.all()
        if not buckets:
            return 0

        # Insert compacted rows
        for row in buckets:
            model = ProjectMetricsModel(
                project_id=project_id,
                timestamp=_strip_tz(row.bucket_ts),
                request_count=row.request_count,
                success_count=row.success_count,
                failure_count=row.failure_count,
                avg_latency_ms=float(row.avg_latency_ms or 0),
                bytes_sent=row.bytes_sent,
                bytes_received=row.bytes_received,
                granularity=target_granularity,
            )
            self._session.add(model)

        # Delete source rows
        del_stmt = (
            delete(ProjectMetricsModel)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.granularity == source_granularity)
            .where(ProjectMetricsModel.timestamp < older_than)
        )
        del_result = await self._session.execute(del_stmt)
        await self._session.flush()
        return int(del_result.rowcount or 0)  # type: ignore[attr-defined]

    async def compact_proxy_metrics(
        self,
        proxy_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """Compact proxy metrics from source to target granularity.

        Same as compact_project_metrics but for the proxy_metrics table.
        Returns the number of source rows deleted.
        """
        bucket_epoch, bucket_ts = _bucket_expressions(ProxyMetricsModel, target_granularity)
        agg_cols = _metrics_aggregate_columns(ProxyMetricsModel)

        base_filter = [
            ProxyMetricsModel.proxy_id == proxy_id,
            ProxyMetricsModel.granularity == source_granularity,
            ProxyMetricsModel.timestamp < older_than,
        ]

        agg_query = (
            select(
                bucket_ts,
                *agg_cols,
                func.max(ProxyMetricsModel.timestamp).label("max_ts"),
            )
            .where(*base_filter)
            .group_by(bucket_epoch)
        )

        result = await self._session.execute(agg_query)
        buckets = result.all()
        if not buckets:
            return 0

        # Get the latest status for each bucket (status from the row with max timestamp)
        max_ts_set = {row.max_ts for row in buckets}
        status_query = (
            select(
                ProxyMetricsModel.timestamp,
                ProxyMetricsModel.status,
            )
            .where(ProxyMetricsModel.proxy_id == proxy_id)
            .where(ProxyMetricsModel.timestamp.in_(max_ts_set))
        )
        status_result = await self._session.execute(status_query)
        status_by_ts = {row.timestamp: row.status for row in status_result.all()}

        for row in buckets:
            status = status_by_ts.get(row.max_ts, "unknown")
            model = ProxyMetricsModel(
                proxy_id=proxy_id,
                timestamp=_strip_tz(row.bucket_ts),
                request_count=row.request_count,
                success_count=row.success_count,
                failure_count=row.failure_count,
                avg_latency_ms=float(row.avg_latency_ms or 0),
                bytes_sent=row.bytes_sent,
                bytes_received=row.bytes_received,
                status=status,
                granularity=target_granularity,
            )
            self._session.add(model)

        del_stmt = (
            delete(ProxyMetricsModel)
            .where(*base_filter)
        )
        del_result = await self._session.execute(del_stmt)
        await self._session.flush()
        return int(del_result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_project_metrics_older_than(
        self,
        project_id: str,
        older_than: datetime,
    ) -> int:
        """Delete all project metrics rows older than the given timestamp."""
        stmt = (
            delete(ProjectMetricsModel)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.timestamp < older_than)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_proxy_metrics_for_project_older_than(
        self,
        project_id: str,
        older_than: datetime,
    ) -> int:
        """Delete proxy metrics for all proxies belonging to a project.

        Uses a subquery to resolve proxy IDs via connectors in a single DELETE.
        """
        proxy_ids_subquery = (
            select(ProxyModel.id)
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
            .scalar_subquery()
        )
        stmt = (
            delete(ProxyMetricsModel)
            .where(ProxyMetricsModel.proxy_id.in_(proxy_ids_subquery))
            .where(ProxyMetricsModel.timestamp < older_than)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def get_proxy_ids_for_project(self, project_id: str) -> list[str]:
        """Get all proxy IDs belonging to a project (via connectors)."""
        query = (
            select(ProxyModel.id)
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
        )
        result = await self._session.execute(query)
        return [row[0] for row in result.all()]


class UserRepository:
    """Repository for user database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[User]:
        """Get all users, ordered by creation time."""
        result = await self._session.execute(
            select(UserModel).order_by(UserModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, user_id: str) -> User | None:
        """Get user by ID."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_username(self, username: str) -> User | None:
        """Get user by username."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.username == username)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_email(self, email: str) -> User | None:
        """Get user by email (only matches non-empty emails)."""
        if not email:
            return None
        result = await self._session.execute(
            select(UserModel).where(UserModel.email == email)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_invite_token(self, token: str) -> User | None:
        """Get user by invite token."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.invite_token == token)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def count(self) -> int:
        """Count total users."""
        result = await self._session.execute(select(func.count(UserModel.id)))
        return result.scalar() or 0

    async def create(self, user: User) -> User:
        """Create a new user."""
        model = UserModel(
            id=user.id,
            username=user.username,
            email=user.email,
            password_hash=user.password_hash,
            role=user.role.value if isinstance(user.role, UserRole) else user.role,
            is_active=user.is_active,
            invite_token=user.invite_token,
            invite_token_expires_at=user.invite_token_expires_at,
            theme_preference=user.theme_preference,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return user

    async def update(self, user: User) -> User:
        """Update an existing user."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.username = user.username
            model.email = user.email
            model.password_hash = user.password_hash
            model.role = user.role.value if isinstance(user.role, UserRole) else user.role
            model.is_active = user.is_active
            model.invite_token = user.invite_token
            model.invite_token_expires_at = user.invite_token_expires_at
            model.theme_preference = user.theme_preference
            model.updated_at = utc_now()
            await self._session.flush()
        return user

    async def delete(self, user_id: str) -> bool:
        """Delete a user."""
        result = await self._session.execute(
            delete(UserModel).where(UserModel.id == user_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: UserModel) -> User:
        """Convert database model to domain model."""
        return User(
            id=model.id,
            username=model.username,
            email=model.email,
            password_hash=model.password_hash,
            role=UserRole(model.role),
            is_active=model.is_active,
            invite_token=model.invite_token,
            invite_token_expires_at=model.invite_token_expires_at,
            theme_preference=model.theme_preference,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProviderDescriptorRepository:
    """Repository for admin-authored provider descriptors."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[ProviderRecord]:
        result = await self._session.execute(
            select(ProviderDescriptorModel).order_by(ProviderDescriptorModel.created_at)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_id(self, provider_id: str) -> ProviderRecord | None:
        result = await self._session.execute(
            select(ProviderDescriptorModel).where(ProviderDescriptorModel.id == provider_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, record: ProviderRecord) -> ProviderRecord:
        model = ProviderDescriptorModel(
            id=record.id,
            name=record.name,
            spec=record.spec,
            enabled=record.enabled,
            version=record.version,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return record

    async def update(self, record: ProviderRecord) -> ProviderRecord:
        result = await self._session.execute(
            select(ProviderDescriptorModel).where(ProviderDescriptorModel.id == record.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = record.name
            model.spec = record.spec
            model.enabled = record.enabled
            model.version = record.version
            model.updated_at = utc_now()
            record.updated_at = model.updated_at
            await self._session.flush()
        return record

    async def delete(self, provider_id: str) -> bool:
        result = await self._session.execute(
            delete(ProviderDescriptorModel).where(ProviderDescriptorModel.id == provider_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: ProviderDescriptorModel) -> ProviderRecord:
        return ProviderRecord(
            id=model.id,
            name=model.name,
            spec=model.spec,
            enabled=model.enabled,
            version=model.version,
            created_by=model.created_by,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProviderAuditRepository:
    """Repository for the provider descriptor audit log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: ProviderAuditEntry) -> ProviderAuditEntry:
        self._session.add(
            ProviderAuditModel(
                id=entry.id,
                provider_id=entry.provider_id,
                action=entry.action,
                actor=entry.actor,
                egress_hosts=entry.egress_hosts,
                hosts_changed=entry.hosts_changed,
                spec=entry.spec,
                created_at=entry.created_at,
            )
        )
        await self._session.flush()
        return entry

    async def get_for_provider(self, provider_id: str, limit: int = 100) -> list[ProviderAuditEntry]:
        result = await self._session.execute(
            select(ProviderAuditModel)
            .where(ProviderAuditModel.provider_id == provider_id)
            .order_by(ProviderAuditModel.created_at.desc())
            .limit(limit)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    def _to_domain(self, model: ProviderAuditModel) -> ProviderAuditEntry:
        return ProviderAuditEntry(
            id=model.id,
            provider_id=model.provider_id,
            action=model.action,  # type: ignore[arg-type]
            actor=model.actor,
            egress_hosts=list(model.egress_hosts or []),
            hosts_changed=model.hosts_changed,
            spec=model.spec,
            created_at=model.created_at,
        )
