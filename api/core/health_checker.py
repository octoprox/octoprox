"""Health checker for proxy pool."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import structlog

from api.core.config import settings
from api.models.proxy import ProxyProtocol, ProxyStatus

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager
    from api.models.proxy import Proxy

logger = structlog.get_logger()


class HealthChecker:
    """Performs health checks on proxies in the pool."""
    
    def __init__(self, proxy_manager: ProxyManager) -> None:
        self._proxy_manager = proxy_manager
        self._interval = settings.health_check_interval
        self._timeout = settings.health_check_timeout
    
    async def run(self) -> None:
        """Run the health check loop."""
        logger.info("Starting health checker", interval=self._interval)
        
        while True:
            try:
                await self._check_all_proxies()
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                logger.info("Health checker stopped")
                break
            except Exception as e:
                logger.error("Health check error", error=str(e))
                await asyncio.sleep(self._interval)
    
    async def _check_all_proxies(self) -> None:
        """Check health of all proxies."""
        proxies = self._proxy_manager.proxies
        if not proxies:
            return
        
        logger.debug("Running health checks", proxy_count=len(proxies))
        
        tasks = [self._check_proxy(proxy) for proxy in proxies]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    def _get_proxy_mounts(self, proxy: Proxy) -> dict[str, httpx.AsyncHTTPTransport]:
        """Create proxy mounts for httpx client."""
        proxy_url = proxy.url

        if proxy.protocol in (ProxyProtocol.HTTP, ProxyProtocol.HTTPS):
            transport = httpx.AsyncHTTPTransport(proxy=proxy_url)
            return {
                "http://": transport,
                "https://": transport,
            }
        else:
            # SOCKS proxies not supported for health checks
            raise ValueError(f"Unsupported proxy protocol: {proxy.protocol}")

    async def _check_proxy(self, proxy: Proxy) -> None:
        """Check health of a single proxy."""
        start_time = time.monotonic()

        try:
            mounts = self._get_proxy_mounts(proxy)
            async with httpx.AsyncClient(
                mounts=mounts,
                timeout=self._timeout,
            ) as client:
                response = await client.get("https://httpbin.org/ip")
                
                latency_ms = (time.monotonic() - start_time) * 1000
                
                if response.status_code == 200:
                    proxy.status = ProxyStatus.HEALTHY
                    proxy.consecutive_failures = 0
                    proxy.last_check_latency_ms = latency_ms
                    logger.debug(
                        "Proxy healthy",
                        proxy_id=proxy.id,
                        latency_ms=round(latency_ms, 2),
                    )
                else:
                    self._mark_unhealthy(proxy, f"HTTP {response.status_code}")
                    
        except httpx.TimeoutException:
            self._mark_unhealthy(proxy, "Timeout")
        except httpx.ProxyError as e:
            self._mark_unhealthy(proxy, f"Proxy error: {e}")
        except Exception as e:
            self._mark_unhealthy(proxy, f"Error: {e}")
    
    def _mark_unhealthy(self, proxy: Proxy, reason: str) -> None:
        """Mark a proxy as unhealthy."""
        proxy.consecutive_failures += 1
        
        if proxy.consecutive_failures >= 3:
            proxy.status = ProxyStatus.UNHEALTHY
            logger.warning(
                "Proxy marked unhealthy",
                proxy_id=proxy.id,
                reason=reason,
                failures=proxy.consecutive_failures,
            )
        else:
            proxy.status = ProxyStatus.DEGRADED
            logger.debug(
                "Proxy degraded",
                proxy_id=proxy.id,
                reason=reason,
                failures=proxy.consecutive_failures,
            )

