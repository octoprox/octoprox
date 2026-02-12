"""Proxy pool manager for Octoprox."""

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.health_checker import HealthChecker
from api.core.metrics_flusher import MetricsFlusher
from api.db.redis import RedisClient
from api.db.repository import ProxyRepository, SourceRepository
from api.models.proxy import Proxy, ProxyStatus
from api.models.source import ProxySource
from api.strategies import get_strategy

if TYPE_CHECKING:
    from api.strategies.base import RoutingStrategy

logger = structlog.get_logger()


class ProxyManager:
    """Manages the proxy pool and routing.

    Uses Postgres for persistent storage of proxies and sources.
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
        self._proxies: dict[str, Proxy] = {}
        self._sources: dict[str, ProxySource] = {}
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
        """Load sources and proxies from Postgres."""
        logger.info("Loading data from database")
        async with self._session_factory() as session:
            source_repo = SourceRepository(session)
            proxy_repo = ProxyRepository(session)

            sources = await source_repo.get_all()
            for source in sources:
                self._sources[source.id] = source

            proxies = await proxy_repo.get_all()
            for proxy in proxies:
                self._proxies[proxy.id] = proxy

        logger.info(
            "Loaded from database",
            source_count=len(self._sources),
            proxy_count=len(self._proxies),
        )

    async def _hydrate_from_redis(self) -> None:
        """Hydrate proxy objects with operational data from Redis."""
        logger.info("Hydrating from Redis")
        statuses = await self._redis_client.get_all_proxy_statuses()
        metrics = await self._redis_client.get_all_proxy_metrics()

        for proxy_id, proxy in self._proxies.items():
            if proxy_id in statuses:
                status_data = statuses[proxy_id]
                proxy.status = status_data["status"]
                proxy.last_check_latency_ms = status_data["latency_ms"]
                proxy.consecutive_failures = status_data["consecutive_failures"]

            if proxy_id in metrics:
                m = metrics[proxy_id]
                proxy.request_count = m["request_count"]
                proxy.success_count = m["success_count"]
                proxy.failure_count = m["failure_count"]
                proxy.avg_latency_ms = m["avg_latency_ms"]

        logger.info("Hydrated from Redis", proxy_count=len(self._proxies))

    @property
    def proxies(self) -> list[Proxy]:
        """Get all proxies."""
        return list(self._proxies.values())

    @property
    def healthy_proxies(self) -> list[Proxy]:
        """Get only healthy proxies."""
        return [p for p in self._proxies.values() if p.status == ProxyStatus.HEALTHY]

    @property
    def sources(self) -> list[ProxySource]:
        """Get all proxy sources."""
        return list(self._sources.values())

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

    def get_source(self, source_id: str) -> ProxySource | None:
        """Get a source by ID."""
        return self._sources.get(source_id)

    async def add_source(self, source: ProxySource) -> None:
        """Add a proxy source (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = SourceRepository(session)
            await repo.create(source)
            await session.commit()

        self._sources[source.id] = source
        logger.info("Added source", source_id=source.id, name=source.name)

    async def remove_source(self, source_id: str) -> bool:
        """Remove a proxy source (deletes from Postgres, cascades to proxies)."""
        if source_id not in self._sources:
            return False

        async with self._session_factory() as session:
            repo = SourceRepository(session)
            await repo.delete(source_id)
            await session.commit()

        # Remove from cache
        del self._sources[source_id]
        # Also remove associated proxies from cache
        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.source_id != source_id
        }
        logger.info("Removed source", source_id=source_id)
        return True

    def select_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select a proxy using the current routing strategy."""
        return self._strategy.select(self.healthy_proxies, session_id)

    def set_strategy(self, strategy_name: str) -> None:
        """Change the routing strategy."""
        self._strategy = get_strategy(strategy_name)
        logger.info("Changed routing strategy", strategy=strategy_name)

    async def update_proxy_stats(self, proxy_id: str, success: bool, latency_ms: float) -> None:
        """Update proxy statistics after a request (stores in Redis)."""
        proxy = self._proxies.get(proxy_id)
        if proxy:
            # Update in-memory cache with proper running average
            old_count = proxy.request_count
            proxy.request_count += 1
            if success:
                proxy.success_count += 1
            else:
                proxy.failure_count += 1
            # Compute proper weighted average: new_avg = (old_avg * old_count + new_value) / new_count
            if old_count == 0:
                proxy.avg_latency_ms = latency_ms
            else:
                proxy.avg_latency_ms = (proxy.avg_latency_ms * old_count + latency_ms) / proxy.request_count

            # Persist to Redis
            await self._redis_client.update_proxy_metrics(proxy_id, success, latency_ms)

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

