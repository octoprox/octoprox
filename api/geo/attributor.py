# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Turns exit IP sightings into attributed proxies: the one writer of attribution onto a proxy row, whoever saw the exit.

Each caller brings a :class:`ProxyStore` of the rows its batch names; nothing here touches Postgres or Redis.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

import structlog

from api.core import utc_now
from api.geo.models import (
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_VENDOR_COUNTRY,
    ContradictedAction,
    LocationCandidate,
    LocationVerdict,
    ObservationSource,
    SourcePolicy,
)
from api.geo.readers import is_ip
from api.geo.service import GeoService
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_DISCOVERED_IP, META_SESSION_ID, is_dynamic_gateway

logger = structlog.get_logger()

# Sources that report the same exit again and again; a repeat of a judged exit is not re-applied.
REPEATING_SOURCES = (ObservationSource.HEALTH_CHECK, ObservationSource.PREFLIGHT)
# Rows one page of a re-attribution pass reads, judges and writes, each page
# its own transaction. A page costs three reads and one write transaction,
# and every row on it records a judgement; 500 keeps a transaction to a
# second or two and a pass over 10,000 proxies to twenty of them.
REATTRIBUTE_PAGE_SIZE = 500


class ProxyStore(Protocol):
    """The rows one batch of attribution works on, loaded before the attributor judges them, and where its writes go.

    Each saved row is written only while it holds the stamp it was loaded with; ``rotate`` deletes it, ``drop_verdict`` its
    cached preflight verdict.
    """

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """The loaded row, its credentials resolved (enrich requests through it); None when not loaded or rotated."""
        ...

    def get_connector(self, connector_id: str) -> Connector | None:
        """The loaded row's connector."""
        ...

    def get_project(self, project_id: str) -> Project | None:
        """The loaded connector's project."""
        ...

    def save(self, proxy: Proxy, *, checked_at: datetime | None, rotate: bool = False, drop_verdict: bool = False) -> None:
        """Queue the row's metadata as the attributor left it, judged from a sighting made at ``checked_at``.

        Args:
            checked_at: None for a re-judgement of a row never stamped, which is never older than the row.
            rotate: delete the row after its write.
            drop_verdict: delete its cached preflight verdict, as the exit moved without preflight seeing it.
        """
        ...


class ProxyPages(ProxyStore, Protocol):
    """A store a re-attribution pass reads a page at a time; each page is one batch."""

    async def load_page(
        self, after: str | None, limit: int, *, connector_id: str | None, project_id: str | None
    ) -> list[Proxy]:
        """The next ``limit`` rows after id ``after`` in id order, of one connector, one project or all, as the batch."""
        ...

    async def flush(self) -> None:
        """Write the batch as :class:`ProxyStore` describes, and start an empty one."""
        ...


class ProxyAttributor:
    """Applies attribution to proxies, one batch at a time; holds no proxy state of its own.

    Args:
        lookup_enabled: whether a static proxy added with no country is looked up through itself (``geo_lookup_enabled``).
    """

    def __init__(self, geo_service: GeoService, *, lookup_enabled: bool = True) -> None:
        self._geo_service = geo_service
        self.lookup_enabled = lookup_enabled

    # --- rules on a row ------------------------------------------------------------

    @staticmethod
    def is_repeat(proxy: Proxy, ip: str, source: ObservationSource) -> bool:
        """Whether a sighting only repeats what the row says: a health check or preflight of the exit it was judged on.

        A change of the project's decisions re-judges its rows through a re-attribution instead.
        """
        return (
            source in REPEATING_SOURCES
            and proxy.metadata.get(META_DISCOVERED_IP) == ip
            and META_LOCATION_CONFLICT in proxy.metadata
        )

    @staticmethod
    def is_refused_slot(proxy: Proxy, project: Project | None) -> bool:
        """Whether a judged row is a pooled session slot its project will not use: contradicted under ``refuse``.

        Deleting it lets the provider sync provision a fresh session; fixed exits, open verdicts and skipped slots are
        kept.
        """
        if project is None or is_dynamic_gateway(proxy) or not proxy.metadata.get(META_SESSION_ID):
            return False
        return (
            proxy.metadata.get(META_LOCATION_VERDICT) == LocationVerdict.CONTRADICTED
            and project.location_contradicted == ContradictedAction.REFUSE
        )

    @staticmethod
    def needs_exit_lookup(proxy: Proxy, connector: Connector | None) -> bool:
        """Whether a new row's exit is looked up through it: a static proxy with no country set by hand.

        Every other row learns its exit from its vendor or its sightings.
        """
        return (
            connector is not None
            and connector.credential_type == CredentialType.STATIC_PROXY_PROVIDER.value
            and proxy.country is None
        )

    @staticmethod
    def checked_at(proxy: Proxy) -> datetime | None:
        """When the row's exit was last seen (``location_checked_at``, naive UTC); None when never, or unreadable."""
        raw = proxy.metadata.get(META_LOCATION_CHECKED_AT)
        if not isinstance(raw, str):
            return None
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return stamp.astimezone(UTC).replace(tzinfo=None) if stamp.tzinfo is not None else stamp

    # --- policy --------------------------------------------------------------------

    @staticmethod
    def project_of(store: ProxyStore, connector_id: str | None) -> Project | None:
        """The project owning ``connector_id``, as the store loaded it."""
        connector = store.get_connector(connector_id) if connector_id else None
        return store.get_project(connector.project_id) if connector is not None else None

    def policy_for(self, project: Project | None) -> SourcePolicy:
        """The source policy of ``project``, else the install default."""
        default = self._geo_service.default_policy
        return project.source_policy(default) if project is not None else default

    # --- sightings -----------------------------------------------------------------

    def observe(
        self,
        store: ProxyStore,
        proxy_id: str,
        ip: str,
        *,
        source: ObservationSource,
        endpoint: LocationTarget | None = None,
        session_id: str | None = None,
        candidates: Sequence[LocationCandidate] | None = None,
        observed_at: datetime | None = None,
    ) -> Proxy | None:
        """Attribute one sighting of ``ip`` behind a proxy of ``store``'s batch, and save the row if it changed.

        A repeat (:meth:`is_repeat`) leaves the row as it is, though a repeated preflight still records the sighting;
        returns the proxy, or None when ``ip`` is not an IP or the proxy is not in the batch.

        Args:
            session_id: the client session the sighting came from, stamped on the observation.
            candidates: the databases' answers for ``ip`` at the sighting (a gateway's preflight); not looked up again.
            observed_at: when the exit was seen, by default now; it stamps the row and bounds its write.
        """
        if not is_ip(ip):
            return None
        proxy = store.get_proxy(proxy_id)
        if proxy is None:
            return None
        observed_at = observed_at or utc_now()
        project = self.project_of(store, proxy.connector_id)
        if is_dynamic_gateway(proxy):
            # A dynamic-sessions gateway is not probed, so this is a sighting of
            # one vendor session's exit (Detect, or a plugin): record it for
            # accuracy and unique exits and write nothing on the row.
            self._geo_service.record_sighting(
                proxy,
                ip,
                source=source,
                policy=self.policy_for(project),
                endpoint=endpoint,
                project_id=project.id if project else None,
                session_id=proxy.metadata.get(META_SESSION_ID),
                candidates=candidates,
                observed_at=observed_at,
            )
            return proxy
        if self.is_repeat(proxy, ip, source):
            if source == ObservationSource.PREFLIGHT:
                self._geo_service.record_sighting(
                    proxy,
                    ip,
                    source=source,
                    policy=self.policy_for(project),
                    endpoint=endpoint,
                    project_id=project.id if project else None,
                    session_id=session_id,
                    candidates=candidates,
                    observed_at=observed_at,
                )
            return proxy
        self._apply(
            store, proxy, project, ip, source=source, endpoint=endpoint, session_id=session_id,
            candidates=candidates, observed_at=observed_at, always_save=False,
        )
        return proxy

    def _apply(
        self,
        store: ProxyStore,
        proxy: Proxy,
        project: Project | None,
        ip: str,
        *,
        source: ObservationSource,
        endpoint: LocationTarget | None,
        session_id: str | None,
        candidates: Sequence[LocationCandidate] | None,
        observed_at: datetime,
        always_save: bool,
    ) -> None:
        """Judge and record one sighting, then save the row when it changed, must rotate, or ``always_save``.

        A move of the exit drops the cached preflight verdict, unless preflight saw it and has just cached the new one.
        """
        previous = proxy.metadata.get(META_DISCOVERED_IP)
        _, changed = self._geo_service.apply_observation(
            proxy,
            ip,
            source=source,
            policy=self.policy_for(project),
            endpoint=endpoint,
            project_id=project.id if project else None,
            session_id=session_id,
            candidates=candidates,
            observed_at=observed_at,
        )
        rotate = self.is_refused_slot(proxy, project)
        if not (changed or rotate or always_save):
            return
        moved = isinstance(previous, str) and bool(previous) and previous != ip
        store.save(
            proxy, checked_at=observed_at, rotate=rotate, drop_verdict=moved and source != ObservationSource.PREFLIGHT
        )
        if rotate:
            self._log_rotation(proxy, project)

    @staticmethod
    def _log_rotation(proxy: Proxy, project: Project | None) -> None:
        logger.info(
            "Replacing contradicted vendor session",
            proxy_id=proxy.id,
            project_id=project.id if project else None,
            claimed=proxy.metadata.get(META_VENDOR_COUNTRY),
            resolved=proxy.country,
        )

    # --- lookups through the proxy -------------------------------------------------

    def wants_lookup(self, store: ProxyStore, proxy: Proxy) -> bool:
        """Whether a proxy just added gets a lookup: one that needs it (:meth:`needs_exit_lookup`), lookups on."""
        return self.lookup_enabled and self.needs_exit_lookup(proxy, store.get_connector(proxy.connector_id))

    async def locate(self, proxy: Proxy) -> tuple[str | None, LocationTarget | None]:
        """``(exit_ip, place the echo reported)`` for a proxy with resolved credentials; ip is None on failure."""
        return await self._geo_service.discoverer().discover_with_place(
            proxy.url, log_context={"proxy_id": proxy.id, "host": proxy.host, "port": proxy.port}
        )

    async def enrich(
        self, store: ProxyStore, proxy_id: str, *, source: ObservationSource = ObservationSource.GEO_LOOKUP
    ) -> Proxy | None:
        """Request through a proxy of ``store``'s batch to learn its exit, then attribute it and save the row, changed or not.

        The judged row, a rotated slot's too (Detect shows it); None when the proxy is not in the batch or the request failed.
        """
        proxy = store.get_proxy(proxy_id)
        if proxy is None:
            return None
        ip, endpoint = await self.locate(proxy)
        if ip is None:
            logger.info("Exit location lookup failed", proxy_id=proxy_id, host=proxy.host)
            return None
        self._apply(
            store, proxy, self.project_of(store, proxy.connector_id), ip, source=source, endpoint=endpoint,
            session_id=None, candidates=None, observed_at=utc_now(), always_save=True,
        )
        logger.info("Recorded exit location", proxy_id=proxy_id, ip=ip, country=proxy.country)
        return proxy

    # --- re-attribution ------------------------------------------------------------

    async def reattribute(
        self, pages: ProxyPages, *, connector_id: str | None = None, project_id: str | None = None
    ) -> tuple[int, int]:
        """Re-judge the exit of every proxy of one connector, one project or all, page by page; how many scanned and changed.

        Nothing is requested through the proxies, and each page is written against the stamps it was read with.
        """
        scanned = changed = 0
        after: str | None = None
        while True:
            page = await pages.load_page(after, REATTRIBUTE_PAGE_SIZE, connector_id=connector_id, project_id=project_id)
            if not page:
                break
            page_scanned, page_changed = self._rejudge(pages, page)
            await pages.flush()
            scanned += page_scanned
            changed += page_changed
            if len(page) < REATTRIBUTE_PAGE_SIZE:
                break
            after = page[-1].id
        logger.info(
            "Proxies re-attributed", scanned=scanned, changed=changed, connector_id=connector_id, project_id=project_id
        )
        return scanned, changed

    def _rejudge(self, store: ProxyStore, page: Sequence[Proxy]) -> tuple[int, int]:
        """Re-judge one page; returns how many rows were scanned and how many changed and stay."""
        scanned = changed = 0
        for proxy in page:
            ip = proxy.metadata.get(META_DISCOVERED_IP) or proxy.display_host
            if not isinstance(ip, str) or not is_ip(ip):
                continue
            scanned += 1
            project = self.project_of(store, proxy.connector_id)
            # Read before judging: a re-judgement keeps the stamp, and its write
            # is refused when a sighting moved the row on since this page was read.
            stamp = self.checked_at(proxy)
            _, did_change = self._geo_service.rejudge(
                proxy, ip, policy=self.policy_for(project), endpoint=GeoService.endpoint_place_of(proxy)
            )
            rotate = self.is_refused_slot(proxy, project)
            if did_change or rotate:
                store.save(proxy, checked_at=stamp, rotate=rotate)
            if rotate:
                self._log_rotation(proxy, project)
            elif did_change:
                changed += 1
        return scanned, changed
