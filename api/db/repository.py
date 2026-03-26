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
    ProxyMetricsModel,
    ProxyModel,
    UserModel,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import MitmBrowser, MitmEngine, MitmMode, Project
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
            type=credential.type.value if isinstance(credential.type, CredentialType) else credential.type,
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
            type=CredentialType(model.type),
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
            credential_type=CredentialType(model.credential.type),
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
        """Get all proxies."""
        result = await self._session.execute(select(ProxyModel))
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
        """Get all proxies for a connector."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.connector_id == connector_id)
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
        from sqlalchemy import func

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
    ) -> list[dict[str, Any]]:
        """Get historical metrics for a project."""
        query = select(ProjectMetricsModel).where(
            ProjectMetricsModel.project_id == project_id
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
        ``bucket_seconds`` width, then SUMs counters and AVGs latency.
        """
        bucket = text(str(bucket_seconds))
        # Bucket expression: floor(epoch / bucket_seconds) * bucket_seconds
        ts_col = func.extract("epoch", ProjectMetricsModel.timestamp)
        bucket_epoch = (func.floor(ts_col / bucket) * bucket)
        bucket_ts = func.to_timestamp(bucket_epoch).label("bucket_ts")

        query = (
            select(
                bucket_ts,
                func.sum(ProjectMetricsModel.request_count).label("request_count"),
                func.sum(ProjectMetricsModel.success_count).label("success_count"),
                func.sum(ProjectMetricsModel.failure_count).label("failure_count"),
                func.avg(ProjectMetricsModel.avg_latency_ms).label("avg_latency_ms"),
                func.sum(ProjectMetricsModel.bytes_sent).label("bytes_sent"),
                func.sum(ProjectMetricsModel.bytes_received).label("bytes_received"),
            )
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.timestamp >= since)
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
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
