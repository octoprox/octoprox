# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Routing snapshots and routers for tests, built the way the feed builds them.

Definitions stored as Postgres holds them compile into sections by the
compiler's rules, and snapshots come from :meth:`RoutingSnapshot.from_sections`
alone, so every routing test runs the real indexing; sections are deep copies,
so edit a definition, then build again.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache
from typing import Any

from api.feed.models import (
    GLOBAL_SECTION,
    RUNTIME_SECTION,
    ConnectorSection,
    GlobalSection,
    RuntimeSection,
    Section,
    connector_section,
)
from api.feed.sections import resolve_credentials, routing_credential
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry, build_registry
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot


@cache
def default_registry() -> ProviderRegistry:
    """The code types and the built-in descriptors, as every process registers them; built once per test run."""
    return build_registry(include_plugins=False)


def healthy(proxy: Proxy) -> Proxy:
    """``proxy`` with the status a health check would have stored for a working exit."""
    proxy.status = ProxyStatus.HEALTHY
    return proxy


class FeedBuilder:
    """Definitions in place, and the feed, snapshot or router they compile to."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry if registry is not None else default_registry()
        self.projects: dict[str, Project] = {}
        self.credentials: dict[str, Credential] = {}
        self.connectors: dict[str, Connector] = {}
        # Stored rows: placeholders unresolved, as the proxies table holds them.
        self.proxies: dict[str, Proxy] = {}
        # Proxy id -> end of its quarantine, as the controller reads the quarantine ZSET.
        self.quarantined: dict[str, datetime] = {}
        # Connector id -> end of its traffic block, as the traffic decider decided.
        self.blocked_until: dict[str, datetime] = {}

    def add(self, *items: Project | Credential | Connector | Proxy) -> FeedBuilder:
        """Store definitions, replacing any of the same id."""
        for item in items:
            if isinstance(item, Project):
                self.projects[item.id] = item
            elif isinstance(item, Credential):
                self.credentials[item.id] = item
            elif isinstance(item, Connector):
                self.connectors[item.id] = item
            else:
                self.proxies[item.id] = item
        return self

    def sections(self) -> dict[str, Section]:
        """Every section of the feed the definitions compile to."""
        sections: dict[str, Section] = {
            GLOBAL_SECTION: GlobalSection(
                projects=[self.projects[pid].model_copy(deep=True) for pid in sorted(self.projects)]
            )
        }
        compiled = [
            connector_id
            for connector_id in sorted(self.connectors)
            if self.registry.get(self.connectors[connector_id].credential_type) is not None
        ]
        for connector_id in compiled:
            sections[connector_section(connector_id)] = self.connector_section(connector_id)
        held = {proxy_id for proxy_id, proxy in self.proxies.items() if proxy.connector_id in compiled}
        sections[RUNTIME_SECTION] = RuntimeSection(
            quarantined={pid: until for pid, until in sorted(self.quarantined.items()) if pid in held},
            traffic_blocked_until={cid: until for cid, until in sorted(self.blocked_until.items()) if cid in compiled},
        )
        return sections

    def connector_section(self, connector_id: str) -> ConnectorSection:
        """One connector's section, as the compiler builds it."""
        connector = self.connectors[connector_id].model_copy(deep=True)
        credential = self.credentials.get(connector.credential_id)
        credential = credential.model_copy(deep=True) if credential is not None else None
        descriptor = self.registry.descriptor_provider(connector, credential)
        rows = sorted(
            (proxy for proxy in self.proxies.values() if proxy.connector_id == connector_id),
            key=lambda proxy: (proxy.created_at, proxy.id),
        )
        return ConnectorSection(
            connector=connector,
            credential=routing_credential(credential, descriptor) if credential and descriptor else None,
            declared_countries=descriptor.countries if descriptor is not None else connector.countries,
            proxies=[resolve_credentials(proxy.model_copy(deep=True), credential, connector) for proxy in rows],
        )

    def snapshot(self, previous: RoutingSnapshot | None = None) -> RoutingSnapshot:
        return RoutingSnapshot.from_sections(self.sections(), registry=self.registry, previous=previous)

    def router(self, **options: Any) -> Router:
        """A router over the current definitions; ``options`` are the router's keyword arguments."""
        return Router(self.snapshot(), **options)
