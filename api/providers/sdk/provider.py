# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""``DescriptorProvider``: runs a descriptor for one connector/credential pair."""

from __future__ import annotations

from dataclasses import dataclass

from api.models.connector import Connector
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
    ListModeStrategy,
    PortModeStrategy,
    ProxyBuilder,
    SessionModeStrategy,
    SyncStrategy,
)
from api.providers.sdk.templating import RenderContext, TemplateRenderer


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
        self._strategy = self._build_strategy()

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def proxy_type(self) -> ProxyTypeSpec:
        return self._ptype

    def _build_strategy(self) -> SyncStrategy:
        renderer = TemplateRenderer()
        extractor = ValueExtractor()
        builder = ProxyBuilder(self._descriptor, self._ptype, self.connector.id, renderer)
        if self._ptype.mode == "session":
            return SessionModeStrategy(builder, self._ctx, SessionIdGenerator(self._descriptor.session_id))
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
            return PortModeStrategy(builder, self._ctx, discoverer, known_ips)
        assert self._ptype.source is not None
        return ListModeStrategy(builder, self._ctx, ListSource(self._ptype.source, executor, extractor))

    # --- SyncableProvider -----------------------------------------------------------

    def is_session_based(self) -> bool:
        return self._strategy.is_session_based()

    def needs_periodic_sync(self) -> bool:
        return self._strategy.needs_periodic_sync()

    async def sync_proxies(self, existing_proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        return await self._strategy.sync(existing_proxies)

    async def refresh_ips(self, proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        return await self._strategy.refresh(proxies)
