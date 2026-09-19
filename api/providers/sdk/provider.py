# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""``DescriptorProvider``: runs a descriptor for one connector/credential pair."""

from __future__ import annotations

from dataclasses import dataclass

from api.models.connector import Connector, ProxyTarget, normalize_country_list
from api.models.credential import Credential
from api.models.proxy import Proxy
from api.providers.base import ProxyProvider
from api.providers.sdk.descriptor import ProviderDescriptor, ProxyTypeSpec
from api.providers.sdk.egress import EgressGuard, EgressPolicy
from api.providers.sdk.extract import ValueExtractor
from api.providers.sdk.http import ClientFactory, HttpCallExecutor
from api.providers.sdk.session_ids import SessionIdGenerator
from api.providers.sdk.sources import (
    IpDiscoverer,
    KnownIpsSource,
    ListSource,
    ProxiedClientFactory,
)
from api.providers.sdk.strategies import (
    META_GEO,
    ListModeStrategy,
    PortModeStrategy,
    ProxyBuilder,
    SessionModeStrategy,
    SyncStrategy,
)
from api.providers.sdk.templating import RenderContext, TemplateRenderer, country_field_key


@dataclass(frozen=True)
class SdkRuntime:
    """Process-wide knobs for descriptor execution (from settings)."""

    egress_policy: EgressPolicy = EgressPolicy()
    http_timeout_seconds: float = 60.0
    max_response_bytes: int = 0
    client_factory: ClientFactory | None = None
    proxied_client_factory: ProxiedClientFactory | None = None

    def executor(self, descriptor: ProviderDescriptor) -> HttpCallExecutor:
        return HttpCallExecutor(
            descriptor,
            egress=EgressGuard(self.egress_policy),
            timeout_seconds=self.http_timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            client_factory=self.client_factory,
        )


class DescriptorProvider(ProxyProvider):
    """Provisions proxies for a connector according to a :class:`ProviderDescriptor`.

    Implements the ``SyncableProvider`` contract by delegating to the strategy
    matching the resolved proxy type's mode.
    """

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        connector: Connector,
        credential: Credential | None,
        runtime: SdkRuntime | None = None,
    ) -> None:
        super().__init__(connector, credential)
        if credential is None:
            raise ValueError(f"{descriptor.name} provider requires a credential")
        self._descriptor = descriptor
        self._runtime = runtime or SdkRuntime()
        self._ptype: ProxyTypeSpec = descriptor.resolve_proxy_type(credential.config, connector.config)
        self._ctx = RenderContext(
            credential=dict(credential.config),
            connector=dict(connector.config),
            secret_keys=frozenset(descriptor.secret_keys()),
        )
        # How the connector's country list is honoured depends on the proxy type:
        #  - the credentials reference the country field (residential and
        #    mobile sessions such as Oxylabs "cc-{connector.country_code}",
        #    Bright Data ISP picking IPs per country from its zone list): one
        #    slot group per country, each rendered with that country;
        #  - the credentials cannot carry a country but the type discovers IPs
        #    (Oxylabs and Decodo ISP/datacenter, where each port is pinned to
        #    an IP): a single group, where discovery keeps only IPs located in
        #    the listed countries, the count per country.
        country_field = descriptor.country_field()
        self._country_field_key: str | None = country_field.key if country_field else None
        self._country_key: str | None = country_field_key(descriptor, self._ptype)
        self._filter_countries: list[str] | None = (
            self.countries if self._country_key is None and self._country_field_key and self._ptype.mode == "port" else None
        ) or None
        self._strategy = self._build_strategy(self._ctx)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def proxy_type(self) -> ProxyTypeSpec:
        return self._ptype

    @property
    def country_key(self) -> str | None:
        """Connector config key the credentials geo-target with, or None."""
        return self._country_key

    @property
    def countries(self) -> list[str]:
        """Countries configured on the connector's country field (upper-case ISO codes)."""
        key = self._country_key or self._country_field_key
        if key is None:
            return []
        try:
            return normalize_country_list(self.connector.config.get(key))
        except ValueError:
            return []

    @property
    def filter_countries(self) -> list[str] | None:
        """Countries discovery is restricted to, for types that cannot geo-target their credentials."""
        return self._filter_countries

    def accepts_request_country(self) -> bool:
        """Whether unlisted countries can be provisioned on demand from ``-cc-`` requests.

        True for session-based geo-targeting types whose connector lists no
        countries ("All countries"): a fresh slot group is created for each
        country clients ask for, without any vendor round trip.
        """
        return self._country_key is not None and self._ptype.mode == "session" and not self.countries

    def _build_strategy(self, ctx: RenderContext) -> SyncStrategy:
        renderer = TemplateRenderer()
        extractor = ValueExtractor()
        builder = ProxyBuilder(self._descriptor, self._ptype, self.connector.id, renderer)
        if self._ptype.mode == "session":
            return SessionModeStrategy(builder, ctx, SessionIdGenerator(self._descriptor.session_id))
        executor = self._runtime.executor(self._descriptor)
        if self._ptype.mode == "port":
            assert self._ptype.discovery is not None
            discoverer = IpDiscoverer(
                self._ptype.discovery, extractor, self._runtime.proxied_client_factory
            )
            known_ips = (
                KnownIpsSource(self._ptype.known_ips, executor, extractor)
                if self._ptype.known_ips is not None
                else None
            )
            return PortModeStrategy(builder, ctx, discoverer, known_ips, countries=self._filter_countries)
        assert self._ptype.source is not None
        return ListModeStrategy(builder, ctx, ListSource(self._ptype.source, executor, extractor))

    def _strategy_for(self, country: str | None) -> SyncStrategy:
        """Strategy rendering one country's slot group (``None`` = the ungeo-targeted group)."""
        if self._country_key is None or country is None:
            return self._strategy
        return self._build_strategy(self._ctx.with_country(self._country_key, country))

    # --- per-country slot groups -----------------------------------------------------

    def _slot_country(self, proxy: Proxy, targets: list[str | None]) -> str | None:
        """Which country group an existing proxy belongs to.

        New rows carry ``metadata.geo``. Rows provisioned before per-country
        groups existed are adopted through the provider's ``country_code``
        metadata or, for a single-country connector, into that country.
        """
        geo = proxy.metadata.get(META_GEO)
        if isinstance(geo, str) and geo.strip():
            return geo.strip().upper()
        legacy = proxy.metadata.get("country_code")
        if isinstance(legacy, str) and legacy.strip() and legacy.strip().upper() in targets:
            return legacy.strip().upper()
        configured = self.countries
        if len(configured) == 1 and configured[0] in targets:
            return configured[0]
        return None

    def _target_countries(self, existing: list[Proxy]) -> list[str | None]:
        """Slot groups to keep in sync: the configured countries, or the ungeo-targeted
        group plus every country already provisioned on demand."""
        configured = self.countries
        if configured:
            return list(configured)
        targets: list[str | None] = [None]
        for proxy in existing:
            geo = proxy.metadata.get(META_GEO)
            if isinstance(geo, str) and geo.strip() and geo.strip().upper() not in targets:
                targets.append(geo.strip().upper())
        return targets

    def _group(self, existing: list[Proxy], targets: list[str | None]) -> dict[str | None, list[Proxy]]:
        groups: dict[str | None, list[Proxy]] = {}
        for proxy in existing:
            groups.setdefault(self._slot_country(proxy, targets), []).append(proxy)
        return groups

    def proxy_target(self, existing_proxies: list[Proxy]) -> ProxyTarget:
        """Intended pool size given the proxies that exist, mirroring how sync groups them."""
        per = self._slot_count()
        if self._ptype.mode == "list":
            # The vendor list decides; num_proxies is at most a cap.
            return ProxyTarget()
        if self._country_key is not None:
            targets = self._target_countries(existing_proxies)
            configured = self.countries
            countries = [t for t in targets if t is not None]
            return ProxyTarget(
                total=per * len(targets),
                per_country=per,
                countries=countries,
                on_demand=[] if configured else countries,
            )
        if self._filter_countries:
            return ProxyTarget(total=per * len(self._filter_countries), per_country=per, countries=list(self._filter_countries))
        return ProxyTarget(total=per)

    def _slot_count(self) -> int:
        raw = self._ctx.lookup(self._ptype.count_field)
        if raw is None or raw == "":
            return 1
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return 1

    async def provision_country(self, existing_proxies: list[Proxy], country: str) -> list[Proxy]:
        """Create the slot group for ``country`` (up to the configured count). Returns proxies to add."""
        if self._country_key is None:
            return []
        code = country.strip().upper()
        targets = self._target_countries(existing_proxies)
        if code not in targets:
            targets.append(code)
        group = self._group(existing_proxies, targets).get(code, [])
        to_add, _ = await self._strategy_for(code).sync(group)
        return to_add

    # --- SyncableProvider -----------------------------------------------------------

    def is_session_based(self) -> bool:
        return self._strategy.is_session_based()

    def needs_periodic_sync(self) -> bool:
        """List types always reconcile after refresh; so do port types constrained to countries."""
        if self._strategy.needs_periodic_sync():
            return True
        return self._ptype.mode == "port" and bool(self.countries)

    async def sync_proxies(self, existing_proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        if self._country_key is None:
            return await self._strategy.sync(existing_proxies)
        targets = self._target_countries(existing_proxies)
        groups = self._group(existing_proxies, targets)
        to_add: list[Proxy] = []
        to_remove: list[str] = []
        for country in targets:
            added, removed = await self._strategy_for(country).sync(groups.get(country, []))
            to_add.extend(added)
            to_remove.extend(removed)
        for country, proxies in groups.items():
            if country not in targets:
                to_remove.extend(p.id for p in proxies)
        return to_add, to_remove

    async def refresh_ips(self, proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        if self._country_key is None:
            return await self._strategy.refresh(proxies)
        targets = self._target_countries(proxies)
        groups = self._group(proxies, targets)
        updated: list[Proxy] = []
        to_remove: list[str] = []
        for country, group in groups.items():
            if country not in targets:
                to_remove.extend(p.id for p in group)
                continue
            changed, removed = await self._strategy_for(country).refresh(group)
            updated.extend(changed)
            to_remove.extend(removed)
        return updated, to_remove
