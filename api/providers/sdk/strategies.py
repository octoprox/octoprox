# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provisioning strategies: how a connector's desired slots become proxies.

Each strategy implements the same ``sync`` / ``refresh`` contract the provider
syncer drives. :class:`ProxyBuilder` is shared by all of them and is the only
place a :class:`~api.models.proxy.Proxy` row is assembled from a descriptor.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Literal

import structlog

from api.geo.models import META_ENDPOINT_COUNTRY, META_VENDOR_COUNTRY
from api.models.proxy import Proxy, ProxyStatus
from api.providers.base import _sort_proxies_healthy_first
from api.providers.sdk.descriptor import ProviderDescriptor, ProxyTypeSpec, check_port_range
from api.providers.sdk.session_ids import SessionIdGenerator
from api.providers.sdk.sources import IpDiscoverer, KnownIpsSource, ListedProxy, ListSource
from api.providers.sdk.templating import (
    RenderContext,
    TemplateRenderer,
    resolve_runtime_placeholders,
)

logger = structlog.get_logger()

SyncResult = tuple[list[Proxy], list[str]]

META_SESSION_ID = "session_id"
META_PROXY_TYPE = "proxy_type"
META_DISCOVERED_IP = "discovered_ip"
META_HASHED_IP = "hashed_ip"
META_COUNTRY = "country"  # exit country routing reads; upper-case ISO code (see Proxy.country)
# Raw evidence behind META_COUNTRY, kept apart so attribution can judge the
# vendor: META_VENDOR_COUNTRY is what the vendor said (its list or known-IP
# API, or its own discovery endpoint), META_ENDPOINT_COUNTRY what a
# third-party discovery endpoint reported. Both from api.geo.models.
META_GEO = "geo"  # country the slot was provisioned for (per-country slot groups)
# "true" on the single gateway row of a dynamic-sessions connector: its session
# id and country are rendered per request, not provisioned per slot.
META_DYNAMIC_SESSIONS = "dynamic_sessions"


def is_dynamic_gateway(proxy: Proxy) -> bool:
    """Whether ``proxy`` is the gateway row of a dynamic-sessions connector."""
    return proxy.metadata.get(META_DYNAMIC_SESSIONS) == "true"
# On a request rendered for a dynamic gateway: the connector's percentage of
# session-less requests preflight may echo to observe the exit.
META_EXIT_SAMPLE_PERCENT = "exit_sample_percent"
META_LIST_IDENTITY = "list_identity"
META_PROVIDER = "provider"


class ProxyBuilder:
    """Assembles proxy rows for one proxy type from render contexts."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        ptype: ProxyTypeSpec,
        connector_id: str,
        renderer: TemplateRenderer | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._ptype = ptype
        self._connector_id = connector_id
        self._renderer = renderer or TemplateRenderer()

    @property
    def ptype(self) -> ProxyTypeSpec:
        return self._ptype

    def port(self, ctx: RenderContext) -> int:
        """Gateway port for ``ctx``: the literal port, or the rendered port template."""
        spec = self._ptype.port
        if spec is None:
            return 0
        if isinstance(spec, int):
            return spec
        text = self._renderer.render_string(spec, ctx).strip()
        try:
            number = int(text)
        except ValueError as exc:
            raise ValueError(
                f"proxy type '{self._ptype.key}': port template '{spec}' rendered to '{text}', not a number"
            ) from exc
        return check_port_range(number)

    def build(self, ctx: RenderContext, *, port: int | None = None, status: ProxyStatus) -> Proxy:
        """Create a proxy for a gateway-style (session/port) type."""
        host = self._renderer.render(self._ptype.host, ctx, "proxy")
        proxy = Proxy(
            host=host,
            port=port if port is not None else self.port(ctx),
            protocol=self._ptype.protocol,
            username=self._renderer.render(self._ptype.username, ctx, "proxy") or None,
            password=self._renderer.render(self._ptype.password, ctx, "proxy") or None,
            connector_id=self._connector_id,
            status=status,
            tags=list(self._ptype.tags),
            metadata=self.metadata(ctx),
        )
        return proxy

    def build_listed(self, listed: ListedProxy, ctx: RenderContext) -> Proxy:
        """Create a proxy for a list-mode entry (credentials come from the vendor)."""
        username = listed.username
        password = listed.password
        if self._ptype.username is not None:
            username = (
                self._renderer.render(self._ptype.username, ctx.with_item(listed.raw), "proxy")
                or None
            )
        if self._ptype.password is not None:
            password = (
                self._renderer.render(self._ptype.password, ctx.with_item(listed.raw), "proxy")
                or None
            )
        metadata = self.metadata(ctx.with_item(listed.raw))
        metadata[META_LIST_IDENTITY] = listed.identity
        if listed.country:
            metadata[META_COUNTRY] = listed.country.strip().upper()
            metadata[META_VENDOR_COUNTRY] = metadata[META_COUNTRY]
        return Proxy(
            host=listed.host,
            port=listed.port,
            protocol=listed.protocol,
            username=username,
            password=password,
            connector_id=self._connector_id,
            status=ProxyStatus.HEALTHY,
            tags=list(self._ptype.tags),
            metadata=metadata,
        )

    def rerender(self, proxy: Proxy, ctx: RenderContext) -> None:
        """Re-render username/password after per-slot variables (e.g. discovered IP) changed."""
        proxy.username = self._renderer.render(self._ptype.username, ctx, "proxy") or None
        proxy.password = self._renderer.render(self._ptype.password, ctx, "proxy") or None

    def rerender_endpoint(self, proxy: Proxy, ctx: RenderContext) -> None:
        """Re-render host and port too, for a session type whose gateway templates depend on the request.

        Not for port mode: there the slot's port is ``base + index``, which the
        port template alone does not know.
        """
        proxy.host = self._renderer.render(self._ptype.host, ctx, "proxy")
        proxy.port = self.port(ctx)

    def metadata(self, ctx: RenderContext) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            META_PROVIDER: self._descriptor.id,
            META_PROXY_TYPE: self._ptype.key,
        }
        for key, template in self._ptype.metadata.items():
            value = self._renderer.render_string(template, ctx, "full")
            if value != "":
                metadata[key] = value
        if ctx.session_id is not None:
            metadata[META_SESSION_ID] = ctx.session_id
        if ctx.slot_country:
            metadata[META_GEO] = ctx.slot_country
        return metadata

    def slot_count(self, ctx: RenderContext) -> int:
        """Desired number of slots from the type's ``count_field`` (default 1)."""
        raw = ctx.lookup(self._ptype.count_field)
        if raw is None or raw == "":
            return 1
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return 1

    def resolved_url(self, proxy: Proxy, ctx: RenderContext) -> str:
        """Proxy URL with runtime placeholders resolved, for discovery requests."""
        values: dict[str, Any] = {**ctx.credential, **ctx.connector}
        username = resolve_runtime_placeholders(proxy.username, values)
        password = resolve_runtime_placeholders(proxy.password, values)
        auth = f"{username}:{password}@" if username and password else ""
        return f"{proxy.protocol.value}://{auth}{proxy.host}:{proxy.port}"


class SyncStrategy(ABC):
    """Contract the provider syncer drives (see ``SyncableProvider``)."""

    @abstractmethod
    def is_session_based(self) -> bool:
        """Session-based types have nothing to refresh periodically."""

    def needs_periodic_sync(self) -> bool:
        """Whether the periodic refresh should also reconcile additions."""
        return False

    @abstractmethod
    async def sync(self, existing: list[Proxy]) -> SyncResult:
        """Return ``(proxies_to_add, proxy_ids_to_remove)``."""

    @abstractmethod
    async def refresh(self, proxies: list[Proxy]) -> SyncResult:
        """Return ``(changed_proxies, proxy_ids_to_remove)``.

        Only proxies whose definition actually changed are returned, so the
        syncer writes and announces nothing for a stable pool.
        """


class SessionModeStrategy(SyncStrategy):
    """Gateway + a fresh session id per slot; the vendor rotates the exit IP."""

    def __init__(
        self, builder: ProxyBuilder, ctx: RenderContext, session_ids: SessionIdGenerator
    ) -> None:
        self._builder = builder
        self._ctx = ctx
        self._session_ids = session_ids

    def is_session_based(self) -> bool:
        return True

    async def sync(self, existing: list[Proxy]) -> SyncResult:
        target = self._builder.slot_count(self._ctx)
        current = len(existing)
        if current < target:
            to_add = [
                self._builder.build(
                    self._ctx.with_slot(session_id=self._session_ids.generate(), index=index),
                    status=ProxyStatus.HEALTHY,
                )
                for index in range(current, target)
            ]
            return to_add, []
        if current > target:
            ordered = _sort_proxies_healthy_first(existing)
            return [], [p.id for p in ordered[target:]]
        return [], []

    async def refresh(self, proxies: list[Proxy]) -> SyncResult:
        return [], []


class DynamicSessionStrategy(SyncStrategy):
    """One gateway row; the session id and country are rendered per request.

    The row exists so routing, metrics and rate limiting keep a proxy to
    attach to. It is never health-checked (no single exit speaks for a
    rotating pool) and is always healthy. Its stored credentials carry a
    random session id only so the row is a complete, valid proxy for the
    Detect button and the provider test; ``DescriptorProvider.render_request``
    replaces them for client traffic. Nothing about the row depends on a
    country, so the connector's country list is an allow-list applied at
    routing.
    """

    def __init__(
        self, builder: ProxyBuilder, ctx: RenderContext, session_ids: SessionIdGenerator
    ) -> None:
        self._builder = builder
        self._ctx = ctx
        self._session_ids = session_ids

    def is_session_based(self) -> bool:
        return True

    def build_gateway(self) -> Proxy:
        proxy = self._builder.build(
            self._ctx.with_slot(session_id=self._session_ids.generate()),
            status=ProxyStatus.HEALTHY,
        )
        proxy.metadata[META_DYNAMIC_SESSIONS] = "true"
        return proxy

    async def sync(self, existing: list[Proxy]) -> SyncResult:
        gateways = [p for p in existing if is_dynamic_gateway(p)]
        if gateways:
            keep = _sort_proxies_healthy_first(gateways)[0]
            return [], [p.id for p in existing if p.id != keep.id]
        # Switching a pool connector to dynamic: every slot row goes, one gateway row arrives.
        return [self.build_gateway()], [p.id for p in existing]

    async def refresh(self, proxies: list[Proxy]) -> SyncResult:
        return [], []


class PortModeStrategy(SyncStrategy):
    """One exit IP per slot, reached through a gateway port or a pinned-IP username.

    ``sequential``: slot *i* is ``base_port + i`` and the IP behind it is
    discovered through the proxy. ``fixed``: every slot uses the same port and
    the IP is pinned via ``{discovered_ip}`` in the username, sourced from the
    vendor's known-IP API when configured and discovered otherwise.

    With ``countries`` set (a connector that lists countries the credentials
    cannot geo-target with, e.g. Oxylabs ISP), the slot count applies per
    country: ports are scanned (or known IPs picked) and only IPs located in
    one of the wanted countries are kept, until every country has its count
    or discovery starts returning failures or IPs already held.
    """

    def __init__(
        self,
        builder: ProxyBuilder,
        ctx: RenderContext,
        discoverer: IpDiscoverer,
        known_ips: KnownIpsSource | None = None,
        countries: list[str] | None = None,
    ) -> None:
        self._builder = builder
        self._ctx = ctx
        self._discoverer = discoverer
        self._known_ips = known_ips
        self._spec = builder.ptype
        self._base_port = builder.port(ctx)
        self._countries: list[str] = [
            c.strip().upper() for c in (countries or []) if c and c.strip()
        ]

    def is_session_based(self) -> bool:
        return False

    @property
    def _sequential(self) -> bool:
        return self._spec.port_strategy == "sequential"

    @property
    def _discovery_is_claim(self) -> bool:
        """A vendor-operated discovery URL speaks for the vendor; a third-party echo is evidence."""
        discovery = self._spec.discovery
        return discovery is None or discovery.vendor_operated

    # --- sync -----------------------------------------------------------------

    async def sync(self, existing: list[Proxy]) -> SyncResult:
        target = self._builder.slot_count(self._ctx)
        if self._countries:
            return await self._sync_by_country(existing, target)
        if self._sequential:
            return await self._sync_sequential(existing, target)
        return await self._sync_fixed(existing, target)

    # --- country-filtered sync ------------------------------------------------------

    @staticmethod
    def _proxy_country(proxy: Proxy) -> str | None:
        value = proxy.metadata.get(META_COUNTRY)
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    @property
    def _allowed_countries(self) -> set[str] | None:
        """Countries a proxy of this strategy may exit from, or None when unconstrained.

        Either the discovery filter (types that cannot geo-target their
        credentials) or, for a per-country slot group, the group's country.
        """
        if self._countries:
            return set(self._countries)
        if self._ctx.slot_country:
            return {self._ctx.slot_country.strip().upper()}
        return None

    def needs_periodic_sync(self) -> bool:
        # With a country constraint, refresh may drop proxies that moved
        # elsewhere; the reconciliation right after it discovers replacements.
        return self._allowed_countries is not None

    def _moved_out(self, proxy: Proxy, country: str) -> bool:
        """True when a refreshed location falls outside the allowed countries."""
        allowed = self._allowed_countries
        code = country.strip().upper() if country else ""
        if allowed is None or not code or code in allowed:
            return False
        logger.info(
            "Proxy exit location moved outside the configured countries, removing",
            proxy_id=proxy.id,
            ip=proxy.metadata.get(META_DISCOVERED_IP),
            country=code,
            allowed=sorted(allowed),
        )
        return True

    async def _sync_by_country(self, existing: list[Proxy], per_country: int) -> SyncResult:
        """Keep ``per_country`` proxies for each wanted country; discover the rest."""
        wanted: dict[str, int] = {country: per_country for country in self._countries}
        keep: list[Proxy] = []
        to_remove: list[str] = []
        for proxy in _sort_proxies_healthy_first(existing):
            country = self._proxy_country(proxy)
            if country is not None and wanted.get(country, 0) > 0:
                wanted[country] -= 1
                keep.append(proxy)
            else:
                # Located outside the wanted countries (or surplus): let it go.
                to_remove.append(proxy.id)
        if not any(wanted.values()):
            return [], to_remove
        existing_ips = self._existing_ips(keep)
        if self._sequential:
            to_add = await self._scan_ports_for_countries(
                {p.port for p in keep}, existing_ips, wanted
            )
        else:
            to_add = await self._fill_fixed_for_countries(len(keep), existing_ips, wanted)
        return to_add, to_remove

    async def _scan_ports_for_countries(
        self, used_ports: set[int], existing_ips: set[str], wanted: dict[str, int]
    ) -> list[Proxy]:
        """Walk the gateway ports upwards, keeping only IPs in wanted countries.

        Stops when every country has its count, after the configured run of
        failures or duplicate IPs (the account's allocation is exhausted), or
        at ``max_scan_ports``.
        """
        discovery = self._spec.discovery
        assert discovery is not None
        proxies: list[Proxy] = []
        consecutive_failures = 0
        consecutive_duplicates = 0
        scanned = 0
        port = self._base_port
        while any(wanted.values()) and scanned < discovery.max_scan_ports:
            if port in used_ports:
                port += 1
                continue
            scanned += 1
            index = port - self._base_port
            proxy = self._build_slot(index, port, ProxyStatus.INITIALIZING)
            ip, country = await self._discover(proxy, index, port)
            port += 1
            if ip is None:
                consecutive_failures += 1
                if consecutive_failures >= discovery.max_consecutive_failures:
                    logger.warning(
                        "Too many consecutive failed ports, stopping country scan",
                        port=port - 1,
                        wanted=wanted,
                    )
                    break
                continue
            consecutive_failures = 0
            if ip in existing_ips:
                consecutive_duplicates += 1
                if consecutive_duplicates >= discovery.max_consecutive_duplicates:
                    logger.warning(
                        "Ports keep returning IPs already held, stopping country scan",
                        port=port - 1,
                        wanted=wanted,
                    )
                    break
                continue
            consecutive_duplicates = 0
            code = country.strip().upper() if country else ""
            if not code or wanted.get(code, 0) <= 0:
                logger.debug(
                    "Skipping port outside wanted countries",
                    port=port - 1,
                    ip=ip,
                    country=code or None,
                )
                continue
            existing_ips.add(ip)
            self._assign_ip(proxy, index, port - 1, ip, code, vendor=self._discovery_is_claim)
            wanted[code] -= 1
            proxies.append(proxy)
        if any(wanted.values()):
            logger.warning("Country scan ended short of target", remaining=wanted, scanned=scanned)
        return proxies

    async def _fill_fixed_for_countries(
        self, start_index: int, existing_ips: set[str], wanted: dict[str, int]
    ) -> list[Proxy]:
        """Fixed-port strategy: pick wanted-country IPs from the vendor list, else retry discovery."""
        discovery = self._spec.discovery
        assert discovery is not None
        proxies: list[Proxy] = []
        index = start_index
        if self._known_ips is not None:
            known = await self._known_ips.fetch(self._ctx)
            if known:
                for entry in known:
                    code = entry.country.strip().upper()
                    if entry.ip in existing_ips or wanted.get(code, 0) <= 0:
                        continue
                    proxy = self._build_slot(index, self._base_port, ProxyStatus.INITIALIZING)
                    self._assign_ip(proxy, index, self._base_port, entry.ip, code, vendor=True)
                    existing_ips.add(entry.ip)
                    wanted[code] -= 1
                    proxies.append(proxy)
                    index += 1
                if any(wanted.values()):
                    logger.warning(
                        "Vendor IP list does not cover the wanted countries", remaining=wanted
                    )
                return proxies
            logger.warning("Known-IP API unavailable, falling back to per-slot discovery")
        consecutive_failures = 0
        attempts_left = discovery.max_scan_ports
        while any(wanted.values()) and attempts_left > 0:
            attempts_left -= 1
            proxy = self._build_slot(index, self._base_port, ProxyStatus.INITIALIZING)
            ip, country = await self._discover(proxy, index, self._base_port)
            if ip is None:
                consecutive_failures += 1
                if consecutive_failures >= discovery.max_consecutive_failures:
                    break
                continue
            consecutive_failures = 0
            code = country.strip().upper() if country else ""
            if ip in existing_ips or not code or wanted.get(code, 0) <= 0:
                continue
            existing_ips.add(ip)
            self._assign_ip(
                proxy, index, self._base_port, ip, code, vendor=self._discovery_is_claim
            )
            wanted[code] -= 1
            proxies.append(proxy)
            index += 1
        return proxies

    async def _sync_sequential(self, existing: list[Proxy], target: int) -> SyncResult:
        target_ports = set(range(self._base_port, self._base_port + target))
        existing_ports = {p.port for p in existing}
        to_remove = [p.id for p in existing if p.port not in target_ports]
        missing_ports = sorted(target_ports - existing_ports)
        existing_ips = self._existing_ips(existing)
        slots = [(port - self._base_port, port) for port in missing_ports]
        to_add = await self._fill_slots(slots, existing_ips)
        return to_add, to_remove

    async def _sync_fixed(self, existing: list[Proxy], target: int) -> SyncResult:
        current = len(existing)
        if current > target:
            ordered = _sort_proxies_healthy_first(existing)
            return [], [p.id for p in ordered[target:]]
        slots = [(index, self._base_port) for index in range(current, target)]
        to_add = await self._fill_slots(slots, self._existing_ips(existing))
        return to_add, []

    async def _fill_slots(
        self, slots: list[tuple[int, int]], existing_ips: set[str]
    ) -> list[Proxy]:
        if not slots:
            return []
        if self._known_ips is not None:
            from_api = await self._fill_from_known_ips(slots, existing_ips)
            if from_api is not None:
                return from_api
            logger.warning("Known-IP API unavailable, falling back to per-slot discovery")
        return await self._fill_by_discovery(slots, existing_ips)

    async def _fill_from_known_ips(
        self, slots: list[tuple[int, int]], existing_ips: set[str]
    ) -> list[Proxy] | None:
        assert self._known_ips is not None
        known = await self._known_ips.fetch(self._ctx)
        if not known:
            return None
        available = [entry for entry in known if entry.ip not in existing_ips]
        if len(available) < len(slots):
            logger.warning(
                "Not enough IPs available from provider",
                needed=len(slots),
                available=len(available),
            )
        proxies: list[Proxy] = []
        for (index, port), entry in zip(slots, available, strict=False):
            proxy = self._build_slot(index, port, ProxyStatus.INITIALIZING)
            self._assign_ip(proxy, index, port, entry.ip, entry.country, vendor=True)
            existing_ips.add(entry.ip)
            proxies.append(proxy)
        return proxies

    async def _fill_by_discovery(
        self, slots: list[tuple[int, int]], existing_ips: set[str]
    ) -> list[Proxy]:
        discovery = self._spec.discovery
        assert discovery is not None
        proxies: list[Proxy] = []
        consecutive_failures = 0
        consecutive_duplicates = 0
        for index, port in slots:
            proxy = self._build_slot(index, port, ProxyStatus.INITIALIZING)
            outcome = await self._discover_slot(proxy, index, port, existing_ips)
            if outcome == "added":
                proxies.append(proxy)
                consecutive_failures = 0
                consecutive_duplicates = 0
                continue
            if outcome == "duplicate":
                # A fixed gateway port always maps to the same exit IP, so skip
                # the port and move on; give up once the vendor keeps handing
                # back IPs we already hold.
                consecutive_duplicates += 1
                if consecutive_duplicates >= discovery.max_consecutive_duplicates:
                    logger.warning(
                        "Too many consecutive duplicate IPs, stopping discovery",
                        index=index,
                        port=port,
                    )
                    break
                continue
            consecutive_failures += 1
            if consecutive_failures >= discovery.max_consecutive_failures:
                logger.warning(
                    "Too many consecutive failed slots, stopping discovery", index=index, port=port
                )
                break
        return proxies

    async def _discover_slot(
        self, proxy: Proxy, index: int, port: int, existing_ips: set[str]
    ) -> Literal["added", "duplicate", "failed"]:
        """Discover one slot's IP, retrying duplicates only where a retry can change the answer."""
        discovery = self._spec.discovery
        assert discovery is not None
        attempts = 1 if self._sequential else discovery.max_retries_per_slot
        saw_duplicate = False
        for attempt in range(attempts):
            ip, country = await self._discover(proxy, index, port)
            if ip is None:
                return "failed"
            if ip in existing_ips:
                saw_duplicate = True
                logger.warning(
                    "Duplicate IP discovered", index=index, port=port, ip=ip, attempt=attempt + 1
                )
                continue
            existing_ips.add(ip)
            self._assign_ip(proxy, index, port, ip, country, vendor=self._discovery_is_claim)
            return "added"
        return "duplicate" if saw_duplicate else "failed"

    # --- refresh -------------------------------------------------------------

    async def refresh(self, proxies: list[Proxy]) -> SyncResult:
        if not proxies:
            return [], []
        if self._known_ips is not None:
            via_api = await self._refresh_from_known_ips(proxies)
            if via_api is not None:
                return via_api
            logger.warning("Known-IP API unavailable for refresh, falling back to discovery")
        return await self._refresh_by_discovery(proxies)

    async def _refresh_from_known_ips(self, proxies: list[Proxy]) -> SyncResult | None:
        assert self._known_ips is not None
        known = await self._known_ips.fetch(self._ctx)
        if not known:
            return None
        by_ip = {entry.ip: entry.country for entry in known}
        updated: list[Proxy] = []
        to_remove: list[str] = []
        seen: set[str] = set()
        for proxy in proxies:
            ip = proxy.metadata.get(META_DISCOVERED_IP)
            if not ip or ip not in by_ip:
                logger.info(
                    "Proxy IP no longer offered by provider, removing", proxy_id=proxy.id, ip=ip
                )
                to_remove.append(proxy.id)
                continue
            if ip in seen:
                to_remove.append(proxy.id)
                continue
            seen.add(ip)
            country = by_ip[ip]
            if self._moved_out(proxy, country):
                to_remove.append(proxy.id)
                continue
            # Compared against the recorded claim only: META_COUNTRY is what
            # attribution resolved and may legitimately differ from the vendor.
            if country and proxy.metadata.get(META_VENDOR_COUNTRY) != country:
                proxy.metadata[META_COUNTRY] = country
                proxy.metadata[META_VENDOR_COUNTRY] = country
                updated.append(proxy)  # only changed rows are written back and announced
        return updated, to_remove

    async def _refresh_by_discovery(self, proxies: list[Proxy]) -> SyncResult:
        """Re-probe every proxy's exit, a few at a time, and report only what changed.

        Probes run concurrently (``discovery.refresh_concurrency``) so a large
        connector refreshes in seconds rather than one timeout at a time;
        results are then applied in the original order so duplicate handling
        stays deterministic. Proxies whose probe failed are left as they are.
        """
        discovery = self._spec.discovery
        assert discovery is not None
        semaphore = asyncio.Semaphore(discovery.refresh_concurrency)

        async def probe(proxy: Proxy) -> tuple[int, str | None, str]:
            index = self._slot_index(proxy)
            async with semaphore:
                ip, country = await self._discover(proxy, index, proxy.port)
            return index, ip, country

        results = await asyncio.gather(*(probe(p) for p in proxies))

        updated: list[Proxy] = []
        to_remove: list[str] = []
        seen: set[str] = set()
        for proxy, (index, ip, country) in zip(proxies, results, strict=True):
            if ip is None:
                continue  # probe failed; keep what we know
            if ip in seen:
                logger.warning("Duplicate IP on refresh, removing proxy", proxy_id=proxy.id, ip=ip)
                to_remove.append(proxy.id)
                continue
            seen.add(ip)
            if self._moved_out(proxy, country):
                to_remove.append(proxy.id)
                continue
            claim = self._discovery_is_claim
            country_key = META_VENDOR_COUNTRY if claim else META_ENDPOINT_COUNTRY
            if ip != proxy.metadata.get(META_DISCOVERED_IP):
                logger.info(
                    "Proxy IP changed",
                    proxy_id=proxy.id,
                    old_ip=proxy.metadata.get(META_DISCOVERED_IP),
                    new_ip=ip,
                )
                self._assign_ip(
                    proxy,
                    index,
                    proxy.port,
                    ip,
                    country or proxy.metadata.get(META_COUNTRY, ""),
                    vendor=claim,
                )
                updated.append(proxy)
            elif country and proxy.metadata.get(country_key) != country:
                # Compared against what this endpoint said last time, never
                # against META_COUNTRY: that is attribution's answer and may
                # legitimately differ. A proxy from before the key existed is
                # written once so the claim is on record.
                proxy.metadata[META_COUNTRY] = country
                proxy.metadata[country_key] = country
                updated.append(proxy)
            if (
                claim
                and proxy.metadata.pop(META_ENDPOINT_COUNTRY, None) is not None
                and proxy not in updated
            ):
                # Recorded as evidence before the descriptor said the URL is the vendor's; relabelled.
                updated.append(proxy)
        return updated, to_remove

    # --- helpers -----------------------------------------------------------------

    def _build_slot(self, index: int, port: int, status: ProxyStatus) -> Proxy:
        return self._builder.build(
            self._ctx.with_slot(index=index, port=port), port=port, status=status
        )

    async def _discover(self, proxy: Proxy, index: int, port: int) -> tuple[str | None, str]:
        url = self._builder.resolved_url(proxy, self._ctx)
        return await self._discoverer.discover_with_country(
            url, log_context={"proxy_id": proxy.id, "index": index, "port": port}
        )

    def _assign_ip(
        self, proxy: Proxy, index: int, port: int, ip: str, country: str, *, vendor: bool = False
    ) -> None:
        """Record a slot's exit IP and country.

        ``vendor`` says the country is the vendor's claim (its list or known-IP
        API, or a discovery URL the vendor operates) rather than a third-party
        endpoint's observation; attribution treats the two differently.
        """
        proxy.display_host = ip
        proxy.metadata[META_DISCOVERED_IP] = ip
        if not self._sequential:
            proxy.metadata[META_HASHED_IP] = ip
        if country:
            # Vendors report codes in either case; stored values are always upper-case ISO codes.
            code = country.strip().upper()
            proxy.metadata[META_COUNTRY] = code
            proxy.metadata[META_VENDOR_COUNTRY if vendor else META_ENDPOINT_COUNTRY] = code
            proxy.metadata.pop(META_ENDPOINT_COUNTRY if vendor else META_VENDOR_COUNTRY, None)
        proxy.status = ProxyStatus.HEALTHY
        if not self._sequential:
            self._builder.rerender(
                proxy, self._ctx.with_slot(index=index, port=port, discovered_ip=ip)
            )

    def _slot_index(self, proxy: Proxy) -> int:
        if self._sequential:
            return proxy.port - self._base_port
        raw = proxy.metadata.get("index")
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _existing_ips(proxies: list[Proxy]) -> set[str]:
        return {
            p.metadata[META_DISCOVERED_IP] for p in proxies if p.metadata.get(META_DISCOVERED_IP)
        }


class ListModeStrategy(SyncStrategy):
    """The vendor API returns the endpoints; we mirror the list."""

    def __init__(self, builder: ProxyBuilder, ctx: RenderContext, source: ListSource) -> None:
        self._builder = builder
        self._ctx = ctx
        self._source = source

    def is_session_based(self) -> bool:
        return False

    def needs_periodic_sync(self) -> bool:
        return True

    async def sync(self, existing: list[Proxy]) -> SyncResult:
        listed = await self._fetch_capped()
        wanted = {entry.identity: entry for entry in listed}
        current = {self._identity(p): p for p in existing}
        to_remove = [p.id for identity, p in current.items() if identity not in wanted]
        to_add = [
            self._builder.build_listed(entry, self._ctx)
            for identity, entry in wanted.items()
            if identity not in current
        ]
        return to_add, to_remove

    async def refresh(self, proxies: list[Proxy]) -> SyncResult:
        listed = await self._fetch_capped()
        wanted = {entry.identity: entry for entry in listed}
        updated: list[Proxy] = []
        to_remove: list[str] = []
        for proxy in proxies:
            entry = wanted.get(self._identity(proxy))
            if entry is None:
                to_remove.append(proxy.id)
                continue
            if self._apply_entry(proxy, entry):
                updated.append(proxy)
        return updated, to_remove

    async def _fetch_capped(self) -> list[ListedProxy]:
        listed = await self._source.fetch(self._ctx)
        cap_raw = self._ctx.lookup(self._builder.ptype.count_field)
        if cap_raw not in (None, ""):
            try:
                cap = int(float(cap_raw))
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                listed = listed[:cap]
        return listed

    def _apply_entry(self, proxy: Proxy, entry: ListedProxy) -> bool:
        fresh = self._builder.build_listed(entry, self._ctx)
        changed = (
            proxy.host != fresh.host
            or proxy.port != fresh.port
            or proxy.username != fresh.username
            or proxy.password != fresh.password
            or proxy.protocol != fresh.protocol
        )
        if changed:
            proxy.host = fresh.host
            proxy.port = fresh.port
            proxy.username = fresh.username
            proxy.password = fresh.password
            proxy.protocol = fresh.protocol
        if entry.country and proxy.metadata.get(META_VENDOR_COUNTRY) != entry.country:
            proxy.metadata[META_COUNTRY] = entry.country
            proxy.metadata[META_VENDOR_COUNTRY] = entry.country
            changed = True
        return changed

    @staticmethod
    def _identity(proxy: Proxy) -> str:
        identity = proxy.metadata.get(META_LIST_IDENTITY)
        return str(identity) if identity else f"{proxy.host}:{proxy.port}"
