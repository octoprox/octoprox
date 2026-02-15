"""Proxy pool manager for Octoprox."""

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.health_checker import HealthChecker
from api.core.metrics_flusher import MetricsFlusher
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
from api.models.credential import Credential
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
        self._project_strategies: dict[str, "RoutingStrategy"] = {}
        # Default strategy for backward compatibility
        self._strategy: RoutingStrategy = get_strategy(settings.default_strategy)
        self._health_checker = HealthChecker(self)
        self._metrics_flusher = MetricsFlusher(session_factory, redis_client, settings)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Start the proxy manager and background tasks."""
        self._running = True
        logger.info("Starting proxy manager")

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

    async def stop(self) -> None:
        """Stop the proxy manager and cleanup."""
        self._running = False
        logger.info("Stopping proxy manager")

        self._metrics_flusher.stop()

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
        """Get all credentials."""
        return list(self._credentials.values())

    @property
    def connectors(self) -> list[Connector]:
        """Get all connectors."""
        return list(self._connectors.values())

    @property
    def projects(self) -> list[Project]:
        """Get all projects."""
        return list(self._projects.values())

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
        """Remove a project (deletes from Postgres, cascades to credentials, connectors and proxies)."""
        if project_id not in self._projects:
            return False

        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.delete(project_id)
            await session.commit()

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
        connector_ids_to_remove = [
            cid for cid, c in self._connectors.items() if c.project_id == project_id
        ]
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
        """Get all credentials for a project."""
        return [c for c in self._credentials.values() if c.project_id == project_id]

    def get_credential(self, credential_id: str) -> Credential | None:
        """Get a credential by ID."""
        return self._credentials.get(credential_id)

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
        """Get all connectors using a specific credential."""
        return [c for c in self._connectors.values() if c.credential_id == credential_id]

    # Connector methods
    def get_connectors_for_project(self, project_id: str) -> list[Connector]:
        """Get all connectors for a project."""
        return [c for c in self._connectors.values() if c.project_id == project_id]

    def get_connector(self, connector_id: str) -> Connector | None:
        """Get a connector by ID."""
        return self._connectors.get(connector_id)

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

    async def remove_connector(self, connector_id: str) -> bool:
        """Remove a connector (deletes from Postgres, cascades to proxies)."""
        if connector_id not in self._connectors:
            return False

        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.delete(connector_id)
            await session.commit()

        # Remove from cache
        del self._connectors[connector_id]
        # Also remove associated proxies from cache
        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.connector_id != connector_id
        }
        logger.info("Removed connector", connector_id=connector_id)
        return True

    def get_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project (via connectors)."""
        connector_ids = {c.id for c in self._connectors.values() if c.project_id == project_id}
        return [p for p in self._proxies.values() if p.connector_id in connector_ids]

    def get_healthy_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get healthy proxies for a project."""
        connector_ids = {c.id for c in self._connectors.values() if c.project_id == project_id}
        return [
            p for p in self._proxies.values()
            if p.connector_id in connector_ids and p.status == ProxyStatus.HEALTHY
        ]

    def select_proxy_for_project(
        self, project_id: str, session_id: str | None = None
    ) -> Proxy | None:
        """Select a proxy for a specific project using the project's routing strategy."""
        healthy_proxies = self.get_healthy_proxies_for_project(project_id)
        strategy = self._project_strategies.get(project_id, self._strategy)
        return strategy.select(healthy_proxies, session_id)

    def set_project_strategy(self, project_id: str, strategy_name: str) -> None:
        """Change the routing strategy for a project."""
        self._project_strategies[project_id] = get_strategy(strategy_name)
        logger.info("Changed project routing strategy", project_id=project_id, strategy=strategy_name)

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """Get a proxy by ID."""
        return self._proxies.get(proxy_id)

    async def add_proxy(self, proxy: Proxy) -> None:
        """Add a proxy to the pool (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.create(proxy)
            await session.commit()

        self._proxies[proxy.id] = proxy
        logger.info("Added proxy", proxy_id=proxy.id, host=proxy.host)

    async def update_proxy(self, proxy: Proxy) -> None:
        """Update a proxy in the pool (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.update(proxy)
            await session.commit()

        self._proxies[proxy.id] = proxy
        logger.info("Updated proxy", proxy_id=proxy.id, host=proxy.host)

    async def remove_proxy(self, proxy_id: str) -> bool:
        """Remove a proxy from the pool (deletes from Postgres)."""
        if proxy_id not in self._proxies:
            return False

        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.delete(proxy_id)
            await session.commit()

        del self._proxies[proxy_id]
        logger.info("Removed proxy", proxy_id=proxy_id)
        return True

    def select_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select a proxy using the current routing strategy."""
        return self._strategy.select(self.healthy_proxies, session_id)

    def set_strategy(self, strategy_name: str) -> None:
        """Change the routing strategy."""
        self._strategy = get_strategy(strategy_name)
        logger.info("Changed routing strategy", strategy=strategy_name)

    async def update_proxy_stats(
        self,
        proxy_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Update proxy statistics after a request (stores in Redis)."""
        proxy = self._proxies.get(proxy_id)
        if proxy:
            # Update in-memory cache
            increment_stats(proxy, success, latency_ms, bytes_sent, bytes_received)

            # Persist to Redis (proxy-level)
            await self._redis_client.update_proxy_metrics(
                proxy_id, success, latency_ms, bytes_sent, bytes_received
            )

            # Also update project-level metrics (both in-memory and Redis)
            connector = self._connectors.get(proxy.connector_id)
            if connector:
                project = self._projects.get(connector.project_id)
                if project:
                    # Update Redis (current window)
                    await self._redis_client.update_project_metrics(
                        project.id, success, latency_ms, bytes_sent, bytes_received
                    )
                    # Update in-memory Project object
                    increment_stats(project, success, latency_ms, bytes_sent, bytes_received)

    async def update_proxy_status(
        self,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float = 0.0,
        consecutive_failures: int = 0,
    ) -> None:
        """Update proxy health status (stores in Redis)."""
        proxy = self._proxies.get(proxy_id)
        if proxy:
            proxy.status = status
            proxy.last_check_latency_ms = latency_ms
            proxy.consecutive_failures = consecutive_failures

            await self._redis_client.set_proxy_status(
                proxy_id, status, latency_ms, consecutive_failures
            )

