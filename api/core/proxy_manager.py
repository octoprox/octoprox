# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy pool manager for Octoprox.

Subscribes to signals from HealthChecker and ProxyServer.
Emits proxy lifecycle signals (proxy_added, proxy_removed, proxy_status_changed).
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.auto_scaler import AutoScaler
from api.core.config import Settings
from api.core.demand_tracker import DemandTracker
from api.core.domain_filter import is_domain_allowed
from api.core.health_checker import HealthChecker
from api.core.metrics_flusher import MetricsFlusher
from api.core.provider_syncer import ProxyProviderSyncer
from api.core.signals import (
    connector_error_updated,
    connector_remove_requested,
    health_check_completed,
    proxy_add_requested,
    proxy_added,
    proxy_draining_requested,
    proxy_draining_started,
    proxy_marked_terminating,
    proxy_remove_requested,
    proxy_removed,
    proxy_status_changed,
    proxy_terminating_requested,
    proxy_update_requested,
    request_completed,
)
from api.core.stats import apply_metrics, combine_metrics, increment_stats
from api.db.redis import RedisClient
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.strategies import get_strategy

if TYPE_CHECKING:
    from api.strategies.base import RoutingStrategy

logger = structlog.get_logger()


class ProxyManager:
    """Manages the proxy pool and routing.

    Uses Postgres for persistent storage of proxies, credentials, and connectors.
    Uses Redis for operational data (health status, metrics, sessions).

    Args:
        session_factory: Async session factory for database operations.
        redis_client: Redis client for operational data.
        settings: Application settings.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._settings = settings

        # In-memory cache (loaded from Postgres on start)
        self._projects: dict[str, Project] = {}
        self._proxies: dict[str, Proxy] = {}
        self._credentials: dict[str, Credential] = {}
        self._connectors: dict[str, Connector] = {}
        # Per-project strategies (project_id -> strategy)
        self._project_strategies: dict[str, RoutingStrategy] = {}
        # Default strategy for backward compatibility
        self._strategy: RoutingStrategy = get_strategy(settings.default_strategy)
        self._health_checker = HealthChecker(self)
        self._metrics_flusher = MetricsFlusher(session_factory, redis_client, settings)
        self._demand_tracker = DemandTracker(redis_client)
        self._auto_scaler = AutoScaler(self)
        self._provider_syncer = ProxyProviderSyncer(self)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Start the proxy manager and background tasks."""
        self._running = True
        logger.info("Starting proxy manager")

        # Subscribe to signals
        self._subscribe_to_signals()

        # Load data from Postgres into memory cache
        await self._load_from_database()

        # Hydrate with Redis operational data
        await self._hydrate_from_redis()

        # Start health checker
        task = asyncio.create_task(self._health_checker.run())
        self._tasks.append(task)

        # Start metrics flusher
        task = asyncio.create_task(self._metrics_flusher.run())
        self._tasks.append(task)

        # Start auto-scaler
        task = asyncio.create_task(self._auto_scaler.run())
        self._tasks.append(task)

        # Start provider syncer (handles all proxy provider types)
        task = asyncio.create_task(self._provider_syncer.run())
        self._tasks.append(task)

    def _subscribe_to_signals(self) -> None:
        """Subscribe to signals from other components."""
        # Health check and request signals
        health_check_completed.connect(self._on_health_check_completed)
        request_completed.connect(self._on_request_completed)

        # AutoScaler request signals
        proxy_add_requested.connect(self._on_proxy_add_requested)
        proxy_remove_requested.connect(self._on_proxy_remove_requested)
        proxy_draining_requested.connect(self._on_proxy_draining_requested)
        proxy_terminating_requested.connect(self._on_proxy_terminating_requested)
        connector_remove_requested.connect(self._on_connector_remove_requested)
        connector_error_updated.connect(self._on_connector_error_updated)

        # Provider syncer signals
        proxy_update_requested.connect(self._on_proxy_update_requested)

        # Also subscribe DemandTracker to request_completed signal
        self._demand_tracker.subscribe_to_signals()
        logger.debug("ProxyManager subscribed to signals")

    async def _on_health_check_completed(
        self,
        sender: object,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float,
        consecutive_failures: int,
    ) -> None:
        """Handle health check completed signal from HealthChecker."""
        await self.update_proxy_status(
            proxy_id, status, latency_ms, consecutive_failures
        )

    async def _on_request_completed(
        self,
        sender: object,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
    ) -> None:
        """Handle request completed signal from ProxyServer."""
        await self._handle_request_stats(
            proxy_id, project_id, success, latency_ms, bytes_sent, bytes_received
        )

    async def _on_proxy_add_requested(
        self,
        sender: object,
        proxy: Proxy,
    ) -> None:
        """Handle proxy add request signal from AutoScaler."""
        await self.add_proxy(proxy)

    async def _on_proxy_remove_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy remove request signal from AutoScaler."""
        await self.remove_proxy(proxy_id)

    async def _on_proxy_draining_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy draining request signal from AutoScaler."""
        await self.start_proxy_draining(proxy_id)

    async def _on_proxy_terminating_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy terminating request signal from AutoScaler."""
        await self.mark_proxy_terminating(proxy_id)

    async def _on_connector_remove_requested(
        self,
        sender: object,
        connector_id: str,
    ) -> None:
        """Handle connector remove request signal from AutoScaler."""
        await self.remove_connector(connector_id)

    async def _on_connector_error_updated(
        self,
        sender: object,
        connector_id: str,
        error: str | None,
        consecutive_errors: int,
    ) -> None:
        """Handle connector error updated signal from AutoScaler."""
        await self.update_connector_error(connector_id, error, consecutive_errors)

    async def _on_proxy_update_requested(
        self,
        sender: object,
        proxy: Proxy,
    ) -> None:
        """Handle proxy update request signal from ProxyProviderSyncer."""
        await self.update_proxy(proxy)

    async def _handle_request_stats(
        self,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
    ) -> None:
        """Handle request statistics update (internal implementation)."""
        proxy = self._proxies.get(proxy_id)
        if proxy:
            # Update in-memory cache
            increment_stats(proxy, success, latency_ms, bytes_sent, bytes_received)

            # Persist to Redis (proxy-level)
            await self._redis_client.update_proxy_metrics(
                proxy_id, success, latency_ms, bytes_sent, bytes_received
            )

        # Update project-level metrics
        project = self._projects.get(project_id)
        if project:
            # Update Redis (current window)
            await self._redis_client.update_project_metrics(
                project_id, success, latency_ms, bytes_sent, bytes_received
            )
            # Update in-memory Project object
            increment_stats(project, success, latency_ms, bytes_sent, bytes_received)

    async def stop(self) -> None:
        """Stop the proxy manager and cleanup."""
        self._running = False
        logger.info("Stopping proxy manager")

        self._metrics_flusher.stop()
        self._auto_scaler.stop()

        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _load_from_database(self) -> None:
        """Load projects, credentials, connectors and proxies from Postgres."""
        logger.info("Loading data from database")
        async with self._session_factory() as session:
            project_repo = ProjectRepository(session)
            credential_repo = CredentialRepository(session)
            connector_repo = ConnectorRepository(session)
            proxy_repo = ProxyRepository(session)

            # Load projects and initialize their strategies
            projects = await project_repo.get_all()
            for project in projects:
                self._projects[project.id] = project
                self._project_strategies[project.id] = get_strategy(project.routing_strategy)

            credentials = await credential_repo.get_all()
            for credential in credentials:
                self._credentials[credential.id] = credential

            connectors = await connector_repo.get_all()
            for connector in connectors:
                self._connectors[connector.id] = connector

            proxies = await proxy_repo.get_all()
            for proxy in proxies:
                self._proxies[proxy.id] = proxy

        logger.info(
            "Loaded from database",
            project_count=len(self._projects),
            credential_count=len(self._credentials),
            connector_count=len(self._connectors),
            proxy_count=len(self._proxies),
        )

    async def _hydrate_from_redis(self) -> None:
        """Hydrate proxy and project objects with operational data from Redis and Postgres.

        Combines cumulative totals from Postgres (historical snapshots) with
        the current window from Redis to get accurate total counts.
        """
        logger.info("Hydrating operational data")
        statuses = await self._redis_client.get_all_proxy_statuses()
        redis_proxy_metrics = await self._redis_client.get_all_proxy_metrics()

        # Always load cumulative totals from Postgres
        async with self._session_factory() as session:
            repo = MetricsRepository(session)
            postgres_proxy_metrics = await repo.get_cumulative_metrics_for_all_proxies()
            postgres_project_metrics = await repo.get_cumulative_project_metrics()

        # Hydrate proxy metrics
        for proxy_id, proxy in self._proxies.items():
            if proxy_id in statuses:
                status_data = statuses[proxy_id]
                proxy.status = status_data["status"]
                proxy.last_check_latency_ms = status_data["latency_ms"]
                proxy.consecutive_failures = status_data["consecutive_failures"]

            # Combine Postgres (historical) + Redis (current window)
            pg = postgres_proxy_metrics.get(proxy_id, {})
            rd = redis_proxy_metrics.get(proxy_id, {})
            apply_metrics(proxy, combine_metrics(pg, rd))

        # Hydrate project metrics directly on Project objects
        # Combines Postgres (historical) + Redis (current window)
        redis_project_metrics = await self._redis_client.get_all_project_metrics()
        for project_id, project in self._projects.items():
            pg = postgres_project_metrics.get(project_id, {})
            rd = redis_project_metrics.get(project_id, {})
            apply_metrics(project, combine_metrics(pg, rd))

        logger.info(
            "Hydrated operational data",
            proxy_count=len(self._proxies),
            project_count=len(self._projects),
            from_redis=len(redis_proxy_metrics),
            from_postgres=len(postgres_proxy_metrics),
        )

    @property
    def proxies(self) -> list[Proxy]:
        """Get all proxies."""
        return list(self._proxies.values())

    @property
    def healthy_proxies(self) -> list[Proxy]:
        """Get only healthy proxies."""
        return [p for p in self._proxies.values() if p.status == ProxyStatus.HEALTHY]

    @property
    def credentials(self) -> list[Credential]:
        """Get all credentials, ordered by creation time."""
        return sorted(self._credentials.values(), key=lambda c: c.created_at)

    @property
    def connectors(self) -> list[Connector]:
        """Get all connectors, ordered by creation time."""
        return sorted(self._connectors.values(), key=lambda c: c.created_at)

    @property
    def projects(self) -> list[Project]:
        """Get all projects, ordered by creation time."""
        return sorted(self._projects.values(), key=lambda p: p.created_at)

    def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        return self._projects.get(project_id)

    def get_project_by_username(self, username: str) -> Project | None:
        """Get a project by proxy username (for authentication)."""
        for project in self._projects.values():
            if project.username == username:
                return project
        return None

    async def add_project(self, project: Project) -> None:
        """Add a project (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.create(project)
            await session.commit()

        self._projects[project.id] = project
        self._project_strategies[project.id] = get_strategy(project.routing_strategy)
        logger.info("Added project", project_id=project.id, name=project.name)

    async def update_project(self, project: Project) -> None:
        """Update a project (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.update(project)
            await session.commit()

        self._projects[project.id] = project
        self._project_strategies[project.id] = get_strategy(project.routing_strategy)
        logger.info("Updated project", project_id=project.id, name=project.name)

    async def remove_project(self, project_id: str) -> bool:
        """Remove a project (deletes from Postgres, cascades to credentials, connectors and proxies).

        Also cleans up Redis data (project metrics and all associated proxy data)
        to prevent foreign key violations in the metrics flusher.
        """
        if project_id not in self._projects:
            return False

        # Collect proxy IDs before deletion for Redis cleanup
        connector_ids_to_remove = [
            cid for cid, c in self._connectors.items() if c.project_id == project_id
        ]
        proxy_ids_to_remove = [
            pid for pid, p in self._proxies.items()
            if p.connector_id in connector_ids_to_remove
        ]

        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.delete(project_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for deleted proxies/project (which would cause foreign key violations)
        for proxy_id in proxy_ids_to_remove:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)
        await self._redis_client.reset_project_metrics(project_id)

        # Remove from cache
        del self._projects[project_id]
        if project_id in self._project_strategies:
            del self._project_strategies[project_id]

        # Remove associated credentials from cache
        credential_ids_to_remove = [
            cid for cid, c in self._credentials.items() if c.project_id == project_id
        ]
        for cid in credential_ids_to_remove:
            del self._credentials[cid]

        # Remove associated connectors and proxies from cache
        for cid in connector_ids_to_remove:
            del self._connectors[cid]

        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.connector_id not in connector_ids_to_remove
        }

        logger.info("Removed project", project_id=project_id)
        return True

    # Credential methods
    def get_credentials_for_project(self, project_id: str) -> list[Credential]:
        """Get all credentials for a project, ordered by creation time."""
        credentials = [c for c in self._credentials.values() if c.project_id == project_id]
        return sorted(credentials, key=lambda c: c.created_at)

    def get_credential(self, credential_id: str) -> Credential | None:
        """Get a credential by ID."""
        return self._credentials.get(credential_id)

    def _build_credential_context(self, proxy: Proxy) -> dict[str, str]:
        """Build a context dictionary for resolving credential placeholders.

        The context contains all values that can be substituted into
        proxy username/password placeholders. It merges all string values
        from both the credential config and connector config.

        Args:
            proxy: The proxy to build context for.

        Returns:
            Dictionary with all string config values from credential and
            connector that can be used to resolve placeholders.
        """
        context: dict[str, str] = {}

        # Get connector for this proxy
        connector = self._connectors.get(proxy.connector_id)
        if not connector:
            return context

        # Get credential for this connector
        credential = self._credentials.get(connector.credential_id)
        if not credential:
            return context

        # Add all string values from credential config
        for key, value in credential.config.items():
            if isinstance(value, str) and value:
                context[key] = value

        # Add all string values from connector config (may override credential values)
        if connector.config:
            for key, value in connector.config.items():
                if isinstance(value, str) and value:
                    context[key] = value

        return context

    def resolve_proxy_credentials(self, proxy: Proxy) -> Proxy:
        """Resolve credential placeholders in proxy username/password.

        Creates a copy of the proxy with placeholders like {username},
        {password}, {customer_id}, {zone_password} replaced with actual
        values from the credential/connector chain.

        Args:
            proxy: The proxy with potential placeholders in credentials.

        Returns:
            A copy of the proxy with resolved credentials.
        """
        # Build context for placeholder resolution
        context = self._build_credential_context(proxy)

        if not context:
            # No context available, return proxy as-is
            return proxy

        # Check if any placeholders need resolution
        username = proxy.username
        password = proxy.password
        needs_resolution = False

        if username and "{" in username:
            needs_resolution = True
        if password and "{" in password:
            needs_resolution = True

        if not needs_resolution:
            return proxy

        # Create a copy of the proxy with resolved credentials
        resolved_proxy = proxy.model_copy()

        if username:
            for key, value in context.items():
                username = username.replace(f"{{{key}}}", value)
            resolved_proxy.username = username

        if password:
            for key, value in context.items():
                password = password.replace(f"{{{key}}}", value)
            resolved_proxy.password = password

        return resolved_proxy

    async def add_credential(self, credential: Credential) -> None:
        """Add a credential (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.create(credential)
            await session.commit()

        self._credentials[credential.id] = credential
        logger.info("Added credential", credential_id=credential.id, name=credential.name)

    async def update_credential(self, credential: Credential) -> None:
        """Update a credential (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.update(credential)
            await session.commit()

        self._credentials[credential.id] = credential
        logger.info("Updated credential", credential_id=credential.id, name=credential.name)

    async def remove_credential(self, credential_id: str) -> bool:
        """Remove a credential (deletes from Postgres)."""
        if credential_id not in self._credentials:
            return False

        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.delete(credential_id)
            await session.commit()

        del self._credentials[credential_id]
        logger.info("Removed credential", credential_id=credential_id)
        return True

    def get_connectors_for_credential(self, credential_id: str) -> list[Connector]:
        """Get all connectors using a specific credential, ordered by creation time."""
        connectors = [c for c in self._connectors.values() if c.credential_id == credential_id]
        return sorted(connectors, key=lambda c: c.created_at)

    # Connector methods
    def get_connectors_for_project(self, project_id: str) -> list[Connector]:
        """Get all connectors for a project, ordered by creation time."""
        connectors = [c for c in self._connectors.values() if c.project_id == project_id]
        return sorted(connectors, key=lambda c: c.created_at)

    def get_connector(self, connector_id: str) -> Connector | None:
        """Get a connector by ID."""
        return self._connectors.get(connector_id)

    def is_connector_enabled(self, connector_id: str) -> bool:
        """Check if a connector is enabled."""
        connector = self._connectors.get(connector_id)
        return connector.enabled if connector else False

    async def add_connector(self, connector: Connector) -> None:
        """Add a connector (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.create(connector)
            await session.commit()

        self._connectors[connector.id] = connector
        logger.info("Added connector", connector_id=connector.id, name=connector.name)

    async def update_connector(self, connector: Connector) -> None:
        """Update a connector (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.update(connector)
            await session.commit()

        self._connectors[connector.id] = connector
        logger.info("Updated connector", connector_id=connector.id, name=connector.name)

    async def update_connector_error(
        self,
        connector_id: str,
        error: str | None,
        consecutive_errors: int,
    ) -> None:
        """Update a connector's error state (persists to Postgres).

        Args:
            connector_id: The connector ID to update.
            error: The error message, or None to clear the error.
            consecutive_errors: The count of consecutive errors.
        """
        from api.core import utc_now

        connector = self._connectors.get(connector_id)
        if not connector:
            logger.warning(
                "Cannot update error for unknown connector",
                connector_id=connector_id,
            )
            return

        # Update error fields
        connector.last_error = error
        connector.last_error_at = utc_now() if error else None
        connector.consecutive_errors = consecutive_errors

        # Persist to database
        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.update(connector)
            await session.commit()

        self._connectors[connector.id] = connector

        if error:
            logger.warning(
                "Connector error recorded",
                connector_id=connector_id,
                error=error,
                consecutive_errors=consecutive_errors,
            )
        else:
            logger.info(
                "Connector error cleared",
                connector_id=connector_id,
            )

    async def remove_connector(self, connector_id: str) -> bool:
        """Remove a connector (deletes from Postgres, cascades to proxies).

        Also cleans up Redis data for all associated proxies to prevent
        foreign key violations in the metrics flusher.
        """
        if connector_id not in self._connectors:
            return False

        # Collect proxy IDs before deletion for Redis cleanup
        proxy_ids_to_remove = [
            pid for pid, p in self._proxies.items()
            if p.connector_id == connector_id
        ]

        async with self._session_factory() as session:
            # Database has ON DELETE CASCADE on proxies.connector_id,
            # so deleting the connector will automatically delete its proxies
            repo = ConnectorRepository(session)
            await repo.delete(connector_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for deleted proxies (which would cause foreign key violations)
        for proxy_id in proxy_ids_to_remove:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)

        # Remove from cache
        del self._connectors[connector_id]
        # Also remove associated proxies from cache
        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.connector_id != connector_id
        }
        logger.info("Removed connector", connector_id=connector_id)
        return True

    async def delete_connector_async(self, connector_id: str) -> bool:
        """Delete a connector, handling cloud instances appropriately.

        For cloud connectors (AWS, GCP, Azure): marks all proxies as TERMINATING
        and disables the connector. The auto-scaler will handle instance
        termination and the connector will be cleaned up when all proxies are gone.

        For non-cloud connectors: directly removes the connector and its proxies.

        Args:
            connector_id: The connector ID to delete.

        Returns:
            True if the connector was found and deletion initiated,
            False if connector not found.
        """
        connector = self._connectors.get(connector_id)
        if not connector:
            return False

        credential = self.get_credential(connector.credential_id)

        # Check if this is a cloud provider connector
        if credential and credential.type in (
            CredentialType.AWS,
            CredentialType.GCP,
            CredentialType.AZURE,
        ):
            # Mark all proxies as terminating - auto-scaler will handle termination
            proxies = self.get_proxies_for_connector(connector_id)
            for proxy in proxies:
                await self.mark_proxy_terminating(proxy.id)

            # Disable the connector and mark for deletion so auto-scaler knows to clean it up
            # The connector will be removed once all proxies are terminated
            connector.enabled = False
            connector.pending_deletion = True
            await self.update_connector(connector)

            logger.info(
                "Marked cloud connector for deletion",
                connector_id=connector_id,
                proxy_count=len(proxies),
            )
            return True

        # Non-cloud connector: remove directly
        return await self.remove_connector(connector_id)

    def _get_enabled_connector_ids(
        self, project_id: str, target_host: str | None = None
    ) -> set[str]:
        """Get IDs of enabled connectors for a project.

        Args:
            project_id: The project to get connectors for.
            target_host: If provided, only return connectors whose domain
                routing config allows this host.
        """
        connector_ids: set[str] = set()
        for c in self._connectors.values():
            if c.project_id != project_id or not c.enabled:
                continue
            if target_host:
                routing = c.parsed_routing_config
                if not is_domain_allowed(target_host, routing):
                    continue
            connector_ids.add(c.id)
        return connector_ids

    def get_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project (via enabled connectors only)."""
        connector_ids = self._get_enabled_connector_ids(project_id)
        return [p for p in self._proxies.values() if p.connector_id in connector_ids]

    def get_all_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project, including those from disabled connectors."""
        connector_ids = {c.id for c in self._connectors.values() if c.project_id == project_id}
        return [p for p in self._proxies.values() if p.connector_id in connector_ids]

    def get_healthy_proxies_for_project(
        self, project_id: str, target_host: str | None = None
    ) -> list[Proxy]:
        """Get healthy proxies for a project (from enabled connectors only).

        Args:
            project_id: The project to get proxies for.
            target_host: If provided, only return proxies from connectors whose
                domain routing config allows this host.
        """
        connector_ids = self._get_enabled_connector_ids(project_id, target_host)
        return [
            p for p in self._proxies.values()
            if p.connector_id in connector_ids and p.status == ProxyStatus.HEALTHY
        ]

    def select_proxy_for_project(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
    ) -> Proxy | None:
        """Select a proxy for a specific project using the project's routing strategy.

        Returns a proxy with resolved credentials (placeholders replaced with
        actual values from the credential/connector chain).

        Args:
            project_id: The project to select a proxy for.
            session_id: Session identifier for sticky routing.
            target_host: If provided, only consider proxies from connectors
                whose domain routing config allows this host.
        """
        healthy_proxies = self.get_healthy_proxies_for_project(project_id, target_host)
        strategy = self._project_strategies.get(project_id, self._strategy)
        proxy = strategy.select(healthy_proxies, session_id)
        if proxy:
            return self.resolve_proxy_credentials(proxy)
        return None

    def set_project_strategy(self, project_id: str, strategy_name: str) -> None:
        """Change the routing strategy for a project."""
        self._project_strategies[project_id] = get_strategy(strategy_name)
        logger.info("Changed project routing strategy", project_id=project_id, strategy=strategy_name)

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """Get a proxy by ID."""
        return self._proxies.get(proxy_id)

    async def add_proxy(self, proxy: Proxy) -> None:
        """Add a proxy to the pool (persists to Postgres).

        Emits proxy_added signal after successful addition.
        """
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.create(proxy)
            await session.commit()

        self._proxies[proxy.id] = proxy
        logger.info("Added proxy", proxy_id=proxy.id, host=proxy.host)

        # Emit signal for subscribers
        await proxy_added.send_async(
            self,
            proxy_id=proxy.id,
            connector_id=proxy.connector_id,
        )

    async def update_proxy(self, proxy: Proxy) -> None:
        """Update a proxy in the pool (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.update(proxy)
            await session.commit()

        self._proxies[proxy.id] = proxy
        logger.info("Updated proxy", proxy_id=proxy.id, host=proxy.host)

    async def remove_proxy(self, proxy_id: str) -> bool:
        """Remove a proxy from the pool (deletes from Postgres).

        Emits proxy_removed signal after successful removal.
        Also cleans up Redis data (status and metrics) to prevent
        foreign key violations in the metrics flusher.
        """
        if proxy_id not in self._proxies:
            return False

        proxy = self._proxies[proxy_id]
        connector_id = proxy.connector_id

        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.delete(proxy_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for a deleted proxy (which would cause foreign key violations)
        await self._redis_client.delete_proxy_status(proxy_id)
        await self._redis_client.reset_proxy_metrics(proxy_id)

        del self._proxies[proxy_id]
        logger.info("Removed proxy", proxy_id=proxy_id)

        # Emit signal for subscribers
        await proxy_removed.send_async(
            self,
            proxy_id=proxy_id,
            connector_id=connector_id,
        )

        return True

    def select_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select a proxy using the current routing strategy.

        Returns a proxy with resolved credentials (placeholders replaced with
        actual values from the credential/connector chain).
        """
        proxy = self._strategy.select(self.healthy_proxies, session_id)
        if proxy:
            return self.resolve_proxy_credentials(proxy)
        return None

    def set_strategy(self, strategy_name: str) -> None:
        """Change the routing strategy."""
        self._strategy = get_strategy(strategy_name)
        logger.info("Changed routing strategy", strategy=strategy_name)

    async def update_proxy_status(
        self,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float = 0.0,
        consecutive_failures: int = 0,
    ) -> None:
        """Update proxy health status (stores in Redis).

        Emits proxy_status_changed signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy:
            old_status = proxy.status
            proxy.status = status
            proxy.last_check_latency_ms = latency_ms
            proxy.consecutive_failures = consecutive_failures

            await self._redis_client.set_proxy_status(
                proxy_id, status, latency_ms, consecutive_failures
            )

            # Emit signal if status actually changed
            if old_status != status:
                await proxy_status_changed.send_async(
                    self,
                    proxy_id=proxy_id,
                    old_status=old_status,
                    new_status=status,
                )

    # Demand tracking and scaling methods

    @property
    def demand_tracker(self) -> DemandTracker:
        """Get the demand tracker instance."""
        return self._demand_tracker

    async def get_demand_info(self, project_id: str) -> dict[str, Any]:
        """Get demand level and instance counts for a project.

        Returns:
            Dict with demand_level, requests_per_minute, current/min/max instances,
            and counts of draining/terminating instances.
        """
        project = self._projects.get(project_id)
        if not project:
            return {}

        # Get all proxies for the project
        proxies = self.get_proxies_for_project(project_id)
        healthy_proxies = self.get_healthy_proxies_for_project(project_id)

        # Count proxies by status
        draining_count = sum(1 for p in proxies if p.status == ProxyStatus.DRAINING)
        terminating_count = sum(1 for p in proxies if p.status == ProxyStatus.TERMINATING)

        # Get demand info from tracker
        demand_info = await self._demand_tracker.get_demand_info(
            project_id, len(healthy_proxies)
        )

        # Get min/max from connectors (aggregate across all cloud connectors)
        min_instances = 0
        max_instances = 0
        for connector in self._connectors.values():
            if connector.project_id == project_id and connector.enabled:
                cloud_config = connector.cloud_config
                if cloud_config:
                    min_instances += cloud_config.min_proxies
                    max_instances += cloud_config.max_proxies

        return {
            "demand_level": demand_info["demand_level"].value,
            "requests_per_minute": demand_info["requests_per_minute"],
            "rate_per_proxy": demand_info["rate_per_proxy"],
            "current_instances": len(proxies),
            "healthy_instances": len(healthy_proxies),
            "min_instances": min_instances,
            "max_instances": max_instances,
            "draining_instances": draining_count,
            "terminating_instances": terminating_count,
        }

    async def start_proxy_draining(self, proxy_id: str) -> bool:
        """Mark a proxy as draining - stop routing new requests to it.

        Args:
            proxy_id: The proxy ID to start draining.

        Returns:
            True if successful, False if proxy not found.

        Emits proxy_draining_started signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        proxy.status = ProxyStatus.DRAINING
        # Store draining start time in metadata
        from api.core import utc_now

        proxy.metadata["draining_started_at"] = utc_now().isoformat()

        # Update in Redis
        await self._redis_client.set_proxy_status(
            proxy_id, ProxyStatus.DRAINING, proxy.last_check_latency_ms, 0
        )

        # Persist to database
        await self.update_proxy(proxy)

        logger.info("Started draining proxy", proxy_id=proxy_id)

        # Emit signal for subscribers
        await proxy_draining_started.send_async(
            self,
            proxy_id=proxy_id,
            connector_id=proxy.connector_id,
        )

        return True

    async def mark_proxy_terminating(self, proxy_id: str) -> bool:
        """Mark a proxy as terminating.

        Args:
            proxy_id: The proxy ID to mark as terminating.

        Returns:
            True if successful, False if proxy not found.

        Emits proxy_marked_terminating signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        proxy.status = ProxyStatus.TERMINATING

        # Update in Redis
        await self._redis_client.set_proxy_status(
            proxy_id, ProxyStatus.TERMINATING, 0, 0
        )

        # Persist to database
        await self.update_proxy(proxy)

        logger.info("Marked proxy as terminating", proxy_id=proxy_id)

        # Emit signal for subscribers
        await proxy_marked_terminating.send_async(
            self,
            proxy_id=proxy_id,
            connector_id=proxy.connector_id,
        )

        return True

    async def delete_proxy_async(self, proxy_id: str) -> bool:
        """Delete a proxy, handling cloud instances appropriately.

        For cloud proxies (AWS, GCP, Azure): marks as TERMINATING so the
        auto-scaler will handle instance termination.

        For non-cloud proxies: directly removes the proxy.

        Args:
            proxy_id: The proxy ID to delete.

        Returns:
            True if the proxy was found and deletion initiated,
            False if proxy not found.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        # Get connector and credential to check if this is a cloud provider
        connector = self.get_connector(proxy.connector_id)
        if connector:
            credential = self.get_credential(connector.credential_id)

            # Check if this is a cloud provider connector
            if credential and credential.type in (
                CredentialType.AWS,
                CredentialType.GCP,
                CredentialType.AZURE,
            ):
                # Mark as terminating - auto-scaler will handle the actual termination
                logger.info(
                    "Marking cloud proxy for termination",
                    proxy_id=proxy_id,
                    credential_type=credential.type.value,
                )
                return await self.mark_proxy_terminating(proxy_id)

        # Non-cloud proxy: remove directly
        return await self.remove_proxy(proxy_id)

    def get_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a specific connector."""
        return [p for p in self._proxies.values() if p.connector_id == connector_id]

    def get_active_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get active (non-draining, non-terminating) proxies for a connector."""
        return [
            p for p in self._proxies.values()
            if p.connector_id == connector_id
            and p.status not in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING)
        ]

