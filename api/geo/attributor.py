# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Turns exit IP sightings into attributed proxies.

The attributor is the only thing that writes attribution onto a proxy. It
learns about exit IPs the way the rest of Octoprox communicates, through
signals:

* ``exit_ip_observed``: the provider syncer after discovery, the health
  checker after a check that reported the caller's address.
* ``proxy_added``: a manually added proxy of a static connector with no
  country yet gets one request through it to the echo endpoint.
* ``exit_location_mismatch``: preflight found a proxy exiting somewhere
  other than expected; a vendor-session slot is rotated, a fixed exit flagged.

It reaches the proxy pool through the small :class:`ProxyStore` protocol,
which the proxy manager satisfies, and resolves each proxy under the source
policy of the project owning its connector.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.signals import exit_ip_changed, exit_ip_observed, exit_location_mismatch, proxy_added
from api.geo.models import (
    META_ENDPOINT_COUNTRY,
    META_LOCATION_CONFLICT,
    ObservationSource,
    SourcePolicy,
    normalize_country,
)
from api.geo.readers import is_ip
from api.geo.service import GeoService
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_DISCOVERED_IP, META_SESSION_ID, is_dynamic_gateway

logger = structlog.get_logger()

# Through-proxy lookups of newly added static proxies running at once.
LOOKUP_CONCURRENCY = 5


class ProxyStore(Protocol):
    """What the attributor needs from the proxy pool."""

    @property
    def proxies(self) -> list[Proxy]: ...

    def get_proxy(self, proxy_id: str) -> Proxy | None: ...

    def get_connector(self, connector_id: str) -> Connector | None: ...

    def get_project(self, project_id: str) -> Project | None: ...

    def resolve_proxy_credentials(self, proxy: Proxy) -> Proxy: ...

    async def update_proxy(self, proxy: Proxy) -> None: ...

    async def update_proxies(self, proxies: list[Proxy]) -> None: ...

    async def remove_proxy(self, proxy_id: str) -> bool: ...


class ProxyAttributor:
    """Applies attribution to proxies in response to exit IP sightings."""

    def __init__(self, geo_service: GeoService, settings: Settings) -> None:
        self._geo_service = geo_service
        # Whether a manually added static proxy gets a request through it to learn its exit.
        self.enabled = settings.geo_lookup_enabled
        self._semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)
        self._proxy_store: ProxyStore | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # --- lifecycle -----------------------------------------------------------------

    async def start(self, proxy_store: ProxyStore) -> None:
        self._proxy_store = proxy_store
        exit_ip_observed.connect(self._on_exit_ip_observed)
        exit_location_mismatch.connect(self._on_exit_location_mismatch)
        proxy_added.connect(self._on_proxy_added)
        logger.info("Proxy attribution started", static_lookup=self.enabled, echo_url=self.url)

    async def stop(self) -> None:
        exit_ip_observed.disconnect(self._on_exit_ip_observed)
        exit_location_mismatch.disconnect(self._on_exit_location_mismatch)
        proxy_added.disconnect(self._on_proxy_added)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def in_flight(self) -> int:
        """Through-proxy lookups running on this instance."""
        return len(self._tasks)

    @property
    def url(self) -> str:
        return self._geo_service.settings.echo_url

    # --- policy --------------------------------------------------------------------

    def _project_of(self, connector_id: str | None) -> Project | None:
        if self._proxy_store is None or not connector_id:
            return None
        connector = self._proxy_store.get_connector(connector_id)
        return self._proxy_store.get_project(connector.project_id) if connector is not None else None

    def policy_for(self, connector_id: str | None) -> SourcePolicy:
        """The source policy of the project owning ``connector_id``, else the install default."""
        default = self._geo_service.default_policy
        project = self._project_of(connector_id)
        return project.source_policy(default) if project is not None else default

    def project_id_for(self, connector_id: str | None) -> str | None:
        """The project owning ``connector_id``; observations carry it so project views can filter."""
        project = self._project_of(connector_id)
        return project.id if project is not None else None

    # --- observations --------------------------------------------------------------

    async def observe(
        self,
        proxy_id: str,
        ip: str,
        *,
        source: ObservationSource,
        endpoint_country: str | None = None,
    ) -> Proxy | None:
        """Attribute ``ip`` for the proxy and persist it when anything changed.

        The observation is stamped with the project owning the proxy's
        connector. Health checks report the same IP every minute; those are
        applied only when the IP moved or the proxy was never attributed.
        """
        if self._proxy_store is None or not is_ip(ip):
            return None
        proxy = self._proxy_store.get_proxy(proxy_id)
        if proxy is None:
            return None
        if is_dynamic_gateway(proxy):
            # A dynamic-sessions gateway is not probed, so this is a sighting of
            # one vendor session's exit (Detect, or a plugin): record it for
            # accuracy and unique exits and write nothing on the row.
            self._geo_service.record_sighting(
                proxy,
                ip,
                source=source,
                policy=self.policy_for(proxy.connector_id),
                endpoint_country=endpoint_country,
                project_id=self.project_id_for(proxy.connector_id),
                session_id=proxy.metadata.get(META_SESSION_ID),
            )
            return proxy
        previous = proxy.metadata.get(META_DISCOVERED_IP)
        if source == ObservationSource.HEALTH_CHECK and previous == ip and META_LOCATION_CONFLICT in proxy.metadata:
            return proxy
        project_id = self.project_id_for(proxy.connector_id)
        _, changed = self._geo_service.apply_observation(
            proxy,
            ip,
            policy=self.policy_for(proxy.connector_id),
            source=source,
            endpoint_country=endpoint_country,
            project_id=project_id,
        )
        if changed:
            await self._proxy_store.update_proxy(proxy)
        if isinstance(previous, str) and previous and previous != ip:
            # The exit moved: whatever preflight concluded about the old one no longer holds.
            await event_bus.publish(
                exit_ip_changed, self, proxy_id=proxy.id, project_id=project_id, old_ip=previous, new_ip=ip
            )
        return proxy

    async def _on_exit_ip_observed(
        self,
        sender: object,
        proxy_id: str,
        ip: str,
        source: str,
        endpoint_country: str | None = None,
        **_: object,
    ) -> None:
        try:
            kind = ObservationSource(source)
        except ValueError:
            kind = ObservationSource.DISCOVERY
        try:
            await self.observe(proxy_id, ip, source=kind, endpoint_country=endpoint_country)
        except Exception as exc:
            logger.warning("Attribution failed", proxy_id=proxy_id, ip=ip, error=str(exc))

    # --- static proxies: one request through them ----------------------------------

    def wants_lookup(self, proxy: Proxy) -> bool:
        """Static-connector proxies with no country yet get a lookup; everything else is left alone."""
        if not self.enabled or self._proxy_store is None:
            return False
        connector = self._proxy_store.get_connector(proxy.connector_id)
        if connector is None or connector.credential_type != CredentialType.STATIC_PROXY_PROVIDER.value:
            return False
        return proxy.country is None

    async def _on_proxy_added(self, sender: object, proxy_id: str, connector_id: str, **_: object) -> None:
        """proxy_added handler: schedule a lookup without holding up the publisher."""
        if not self.enabled or self._proxy_store is None:
            return
        proxy = self._proxy_store.get_proxy(proxy_id)
        if proxy is None or not self.wants_lookup(proxy):
            return
        task = asyncio.create_task(self._enrich_guarded(proxy_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _enrich_guarded(self, proxy_id: str) -> None:
        async with self._semaphore:
            try:
                await self.enrich(proxy_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Exit location lookup errored", proxy_id=proxy_id, error=str(exc))

    async def locate(self, proxy: Proxy) -> tuple[str | None, str]:
        """``(exit_ip, endpoint_country)`` for a proxy with resolved credentials; ip is None on failure."""
        return await self._geo_service.discoverer().discover_with_country(
            proxy.url, log_context={"proxy_id": proxy.id, "host": proxy.host, "port": proxy.port}
        )

    async def enrich(
        self, proxy_id: str, *, source: ObservationSource = ObservationSource.GEO_LOOKUP
    ) -> Proxy | None:
        """Request through the proxy to learn its exit, then attribute and persist it.

        Returns None when the proxy is gone or the request failed. A manually
        set country is never overwritten by an empty result.
        """
        if self._proxy_store is None:
            return None
        proxy = self._proxy_store.get_proxy(proxy_id)
        if proxy is None:
            return None
        ip, endpoint_country = await self.locate(self._proxy_store.resolve_proxy_credentials(proxy))
        if ip is None:
            logger.info("Exit location lookup failed", proxy_id=proxy_id, host=proxy.host)
            return None
        self._geo_service.apply_observation(
            proxy,
            ip,
            policy=self.policy_for(proxy.connector_id),
            source=source,
            endpoint_country=endpoint_country or None,
            project_id=self.project_id_for(proxy.connector_id),
        )
        await self._proxy_store.update_proxy(proxy)
        logger.info("Recorded exit location", proxy_id=proxy_id, ip=ip, country=proxy.country)
        return proxy

    # --- preflight mismatches --------------------------------------------------------

    async def _on_exit_location_mismatch(
        self,
        sender: object,
        proxy_id: str,
        expected: str,
        observed: str | None = None,
        ip: str | None = None,
        **_: object,
    ) -> None:
        """A vendor-session slot is rotated; a fixed exit is flagged as contradicted.

        A dynamic-sessions gateway is neither: the misplaced session was this
        request's alone and the next request mints another, so the row stays.
        """
        if self._proxy_store is None:
            return
        proxy = self._proxy_store.get_proxy(proxy_id)
        if proxy is None:
            return
        if is_dynamic_gateway(proxy):
            logger.debug("Misplaced dynamic session", proxy_id=proxy_id, expected=expected, observed=observed, ip=ip)
            return
        if proxy.metadata.get(META_SESSION_ID):
            logger.info("Rotating misplaced vendor session", proxy_id=proxy_id, expected=expected, observed=observed, ip=ip)
            await self._proxy_store.remove_proxy(proxy_id)
            return
        if ip and self._geo_service.flag_preflight_mismatch(proxy, ip, expected, normalize_country(observed)):
            await self._proxy_store.update_proxy(proxy)

    # --- offline re-attribution --------------------------------------------------------

    async def reattribute_all(self, connector_id: str | None = None) -> tuple[int, int]:
        """Re-run attribution for every proxy with a known exit IP, without any request.

        Used after a database is added, refreshed or removed. Returns how many
        proxies were re-attributed and how many of them changed as a result.
        Each records a judgement for its exit, changed or not; none records a
        sighting, since nothing was observed.
        """
        if self._proxy_store is None:
            return 0, 0
        changed: list[Proxy] = []
        scanned = 0
        for proxy in self._proxy_store.proxies:
            if connector_id and proxy.connector_id != connector_id:
                continue
            ip = proxy.metadata.get(META_DISCOVERED_IP) or proxy.display_host
            if not isinstance(ip, str) or not is_ip(ip):
                continue
            scanned += 1
            endpoint_country = proxy.metadata.get(META_ENDPOINT_COUNTRY)
            _, did_change = self._geo_service.rejudge(
                proxy,
                ip,
                policy=self.policy_for(proxy.connector_id),
                endpoint_country=endpoint_country if isinstance(endpoint_country, str) else None,
            )
            if did_change:
                changed.append(proxy)
        if changed:
            await self._proxy_store.update_proxies(changed)
        logger.info("Proxies re-attributed", changed=len(changed), scanned=scanned, connector_id=connector_id)
        return scanned, len(changed)
