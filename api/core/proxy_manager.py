"""Proxy pool manager for Octoprox."""

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core.config import settings
from api.core.health_checker import HealthChecker
from api.models.proxy import Proxy, ProxyStatus
from api.models.source import ProxySource
from api.strategies import get_strategy

if TYPE_CHECKING:
    from api.strategies.base import RoutingStrategy

logger = structlog.get_logger()


class ProxyManager:
    """Manages the proxy pool and routing."""
    
    def __init__(self) -> None:
        self._proxies: dict[str, Proxy] = {}
        self._sources: dict[str, ProxySource] = {}
        self._strategy: RoutingStrategy = get_strategy(settings.default_strategy)
        self._health_checker = HealthChecker(self)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
    
    async def start(self) -> None:
        """Start the proxy manager and background tasks."""
        self._running = True
        logger.info("Starting proxy manager")
        
        # Start health checker
        task = asyncio.create_task(self._health_checker.run())
        self._tasks.append(task)
    
    async def stop(self) -> None:
        """Stop the proxy manager and cleanup."""
        self._running = False
        logger.info("Stopping proxy manager")
        
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
    
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
    
    def add_proxy(self, proxy: Proxy) -> None:
        """Add a proxy to the pool."""
        self._proxies[proxy.id] = proxy
        logger.info("Added proxy", proxy_id=proxy.id, host=proxy.host)
    
    def remove_proxy(self, proxy_id: str) -> bool:
        """Remove a proxy from the pool."""
        if proxy_id in self._proxies:
            del self._proxies[proxy_id]
            logger.info("Removed proxy", proxy_id=proxy_id)
            return True
        return False
    
    def get_source(self, source_id: str) -> ProxySource | None:
        """Get a source by ID."""
        return self._sources.get(source_id)
    
    def add_source(self, source: ProxySource) -> None:
        """Add a proxy source."""
        self._sources[source.id] = source
        logger.info("Added source", source_id=source.id, name=source.name)
    
    def remove_source(self, source_id: str) -> bool:
        """Remove a proxy source."""
        if source_id in self._sources:
            del self._sources[source_id]
            logger.info("Removed source", source_id=source_id)
            return True
        return False
    
    def select_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select a proxy using the current routing strategy."""
        return self._strategy.select(self.healthy_proxies, session_id)
    
    def set_strategy(self, strategy_name: str) -> None:
        """Change the routing strategy."""
        self._strategy = get_strategy(strategy_name)
        logger.info("Changed routing strategy", strategy=strategy_name)
    
    def update_proxy_stats(self, proxy_id: str, success: bool, latency_ms: float) -> None:
        """Update proxy statistics after a request."""
        proxy = self._proxies.get(proxy_id)
        if proxy:
            proxy.request_count += 1
            if success:
                proxy.success_count += 1
            else:
                proxy.failure_count += 1
            # Update average latency
            if proxy.avg_latency_ms == 0:
                proxy.avg_latency_ms = latency_ms
            else:
                proxy.avg_latency_ms = (proxy.avg_latency_ms + latency_ms) / 2

