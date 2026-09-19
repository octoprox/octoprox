# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit-location lookup for manually added proxies.

Static connectors know nothing about where their proxies exit from. The
service subscribes to the in-process ``proxy_added`` signal and, for a proxy
of a static connector without a country, makes one request *through* it to a
JSON endpoint that reports the caller's IP and country (``proxy.geo_lookup``
settings). The result is recorded as ``display_host`` and
``metadata.country``, which ``-cc-`` country routing then matches on. Admins
can also set or clear the country by hand, and re-run the lookup from the
Proxies page.

``proxy_added`` is local to the instance that added the proxy, so exactly one
lookup runs per proxy in a cluster; the update then reaches peers through the
regular cross-instance change feed.


A future improvement is an offline lookup (for example a MaxMind database)
that needs no request through the proxy.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core.config import Settings
from api.core.signals import proxy_added
from api.models.credential import CredentialType
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import IpDiscoverySpec
from api.providers.sdk.sources import IpDiscoverer, ProxiedClientFactory
from api.providers.sdk.strategies import META_COUNTRY, META_DISCOVERED_IP

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()


class GeoLookup:
    """Looks up a proxy's exit IP and country by requesting through it."""

    def __init__(
        self, settings: Settings, client_factory: ProxiedClientFactory | None = None, concurrency: int = 5
    ) -> None:
        self.enabled = settings.geo_lookup_enabled
        self._spec = IpDiscoverySpec(
            url=settings.geo_lookup_url,
            ip_path=settings.geo_lookup_ip_path,
            country_path=settings.geo_lookup_country_path or None,
            timeout_seconds=settings.geo_lookup_timeout_seconds,
        )
        self._discoverer = IpDiscoverer(self._spec, client_factory=client_factory)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._proxy_manager: ProxyManager | None = None
        self._tasks: set[asyncio.Task[None]] = set()  # in-flight lookups

    # --- lifecycle -----------------------------------------------------------------

    async def start(self, proxy_manager: ProxyManager) -> None:
        """Begin looking up static proxies as they are added."""
        self._proxy_manager = proxy_manager
        proxy_added.connect(self._on_proxy_added)
        logger.info("Exit-location lookup started", enabled=self.enabled, url=self._spec.url)

    async def stop(self) -> None:
        """Unsubscribe and cancel lookups still in flight."""
        proxy_added.disconnect(self._on_proxy_added)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def wants_lookup(self, proxy: Proxy) -> bool:
        """Static-connector proxies with no country yet get a lookup; everything else is left alone."""
        if not self.enabled or self._proxy_manager is None:
            return False
        connector = self._proxy_manager.get_connector(proxy.connector_id)
        if connector is None or connector.credential_type != CredentialType.STATIC_PROXY_PROVIDER.value:
            return False
        return proxy.country is None

    async def _on_proxy_added(self, sender: object, proxy_id: str, connector_id: str, **_: object) -> None:
        """proxy_added handler: schedule a lookup without holding up the publisher."""
        if not self.enabled or self._proxy_manager is None:
            return
        proxy = self._proxy_manager.get_proxy(proxy_id)
        if proxy is None or not self.wants_lookup(proxy):
            return
        task = asyncio.create_task(self._enrich_guarded(proxy_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _enrich_guarded(self, proxy_id: str) -> None:
        assert self._proxy_manager is not None
        async with self._semaphore:
            try:
                await self.enrich(self._proxy_manager, proxy_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Exit location lookup errored", proxy_id=proxy_id, error=str(exc))

    # --- lookup --------------------------------------------------------------------

    async def locate(self, proxy: Proxy) -> tuple[str | None, str]:
        """Return ``(exit_ip, country)`` for a proxy with resolved credentials; ip is None on failure."""
        return await self._discoverer.discover_with_country(
            proxy.url, log_context={"proxy_id": proxy.id, "host": proxy.host, "port": proxy.port}
        )

    async def enrich(self, proxy_manager: ProxyManager, proxy_id: str) -> Proxy | None:
        """Look up and persist the exit location of one proxy.

        Returns the updated proxy, or None when the proxy is gone or the
        lookup failed. A manually set country is never overwritten by an
        empty lookup result.
        """
        proxy = proxy_manager.get_proxy(proxy_id)
        if proxy is None:
            return None
        ip, country = await self.locate(proxy_manager.resolve_proxy_credentials(proxy))
        if ip is None:
            logger.info("Exit location lookup failed", proxy_id=proxy_id, host=proxy.host)
            return None
        proxy.display_host = ip
        proxy.metadata[META_DISCOVERED_IP] = ip
        if country:
            proxy.metadata[META_COUNTRY] = country
        await proxy_manager.update_proxy(proxy)
        logger.info("Recorded exit location", proxy_id=proxy_id, ip=ip, country=country or None)
        return proxy
