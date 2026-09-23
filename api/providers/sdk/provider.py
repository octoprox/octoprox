# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""``DescriptorProvider``: runs a descriptor for one connector/credential pair."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from api.models.connector import Connector, ProxyTarget, normalize_country_list
from api.models.credential import Credential
from api.models.proxy import Proxy
from api.providers.base import ProxyProvider
from api.providers.sdk.descriptor import (
    DEFAULT_EXIT_SAMPLE_PERCENT,
    SESSION_MODE_DYNAMIC,
    ProviderDescriptor,
    ProxyTypeSpec,
)
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
    META_EXIT_SAMPLE_PERCENT,
    META_GEO,
    META_SESSION_ID,
    DynamicSessionStrategy,
    ListModeStrategy,
    PortModeStrategy,
    ProxyBuilder,
    SessionModeStrategy,
    SyncStrategy,
    is_dynamic_gateway,
)
from api.providers.sdk.templating import RenderContext, TemplateRenderer, country_field_key


def _stable_choice(seed: str, options: list[str]) -> str:
    """The option a seed maps to: the same on every instance, and stable when the list is edited.

    Highest-random-weight selection. Adding an option moves only the seeds
    that score highest on it; removing one moves only the seeds that were
    on it. List order does not matter.
    """

    def score(option: str) -> bytes:
        return hashlib.blake2b(f"{seed}|{option}".encode(), digest_size=8).digest()

    return max(options, key=score)


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
        # Dynamic sessions: one gateway row, credentials rendered per request.
        # Only session types can do it; the connector field chooses.
        self._dynamic = (
            self._ptype.mode == "session"
            and str(self._ctx.lookup(self._ptype.session_mode_field) or "") == SESSION_MODE_DYNAMIC
        )
        self._session_ids = SessionIdGenerator(descriptor.session_id)
        self._builder = ProxyBuilder(descriptor, self._ptype, connector.id, TemplateRenderer())
        self._extractor = ValueExtractor()
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

    @property
    def is_dynamic(self) -> bool:
        """Dynamic sessions: no slots, one gateway row rendered per request."""
        return self._dynamic

    @property
    def exit_sample_percent(self) -> int:
        """Share (0-100) of session-less requests preflight echoes to see their exit; dynamic only."""
        raw = self._ctx.lookup(self._ptype.exit_sample_field)
        if raw is None or raw == "":
            return DEFAULT_EXIT_SAMPLE_PERCENT
        try:
            return max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return DEFAULT_EXIT_SAMPLE_PERCENT

    def accepts_request_country(self) -> bool:
        """Whether unlisted countries can be provisioned on demand from ``-cc-`` requests.

        True for session-based geo-targeting types whose connector lists no
        countries ("All countries"): a fresh slot group is created for each
        country clients ask for, without any vendor round trip. Dynamic
        connectors never provision: the country is rendered into the request.
        """
        if self._dynamic:
            return False
        return self._country_key is not None and self._ptype.mode == "session" and not self.countries

    def render_request(self, proxy: Proxy, *, sessid: str | None, country: str | None, scope: str) -> Proxy:
        """The gateway row as one request should use it.

        Returns a copy of ``proxy`` whose credentials carry the vendor session
        for this request and the country it asked for. ``sessid`` is the
        client's ``-sessid-`` value: the same value always derives the same
        vendor session id (``scope`` keeps projects apart), so the client keeps
        its exit across requests and instances. Without one a fresh id is
        minted and the vendor rotates. ``country`` is the ``-cc-`` code; with
        none, a connector listing countries gets one of them (fixed per client
        session, random per rotating request) and an unrestricted connector
        renders no country at all. Host and port templates are re-rendered
        too. Secrets stay runtime placeholders for the proxy manager to fill in.
        """
        seed = f"{scope}:{sessid}" if sessid else None
        session_id = self._session_ids.derive(seed) if seed else self._session_ids.generate()
        ctx = self._ctx
        code: str | None = None
        if self._country_key is not None:
            wanted = country.strip().upper() if country else None
            allowed = self.countries
            if wanted and (not allowed or wanted in allowed):
                code = wanted
            elif not wanted and allowed:
                # A client session keeps one country, or the vendor would move
                # its exit between requests despite the unchanged session id.
                code = _stable_choice(seed, allowed) if seed else random.choice(allowed)
            ctx = ctx.with_country(self._country_key, code)
        ctx = ctx.with_slot(session_id=session_id)
        rendered = proxy.model_copy(deep=True)
        self._builder.rerender(rendered, ctx)
        self._builder.rerender_endpoint(rendered, ctx)
        rendered.metadata[META_EXIT_SAMPLE_PERCENT] = self.exit_sample_percent
        if seed:
            rendered.metadata[META_SESSION_ID] = session_id
        else:
            # A rotating request: nothing about this session outlives it.
            rendered.metadata.pop(META_SESSION_ID, None)
        if code:
            rendered.metadata[META_GEO] = code
        else:
            rendered.metadata.pop(META_GEO, None)
        return rendered

    def _build_strategy(self, ctx: RenderContext) -> SyncStrategy:
        builder = self._builder
        extractor = self._extractor
        if self._ptype.mode == "session":
            if self._dynamic:
                # The gateway row carries no country: the connector's list is an
                # allow-list applied per request, never baked into the stored row.
                if self._country_key is not None:
                    ctx = ctx.with_country(self._country_key, None)
                return DynamicSessionStrategy(builder, ctx, self._session_ids)
            return SessionModeStrategy(builder, ctx, self._session_ids)
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
        if self._country_key is None or country is None or self._dynamic:
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
        if self._dynamic:
            return ProxyTarget(
                total=1, countries=list(self.countries), dynamic=True, exit_sample_percent=self.exit_sample_percent
            )
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
        if self._country_key is None or self._dynamic:
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
        if self._dynamic:
            return await self._strategy.sync(existing_proxies)
        # A connector switched back from dynamic sessions: its gateway row is
        # not a slot and must go, or it would stay routable for every country.
        stale = [p.id for p in existing_proxies if is_dynamic_gateway(p)]
        existing_proxies = [p for p in existing_proxies if not is_dynamic_gateway(p)]
        if self._country_key is None:
            added, removed = await self._strategy.sync(existing_proxies)
            return added, removed + stale
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
        return to_add, to_remove + stale

    async def refresh_ips(self, proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        if self._country_key is None or self._dynamic:
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
