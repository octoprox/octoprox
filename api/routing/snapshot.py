# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing snapshot: one applied feed, indexed for the request path and never changed afterwards.

A gateway builds one per feed update, and the api one per preview, through :meth:`RoutingSnapshot.from_sections`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.domain_filter import is_domain_allowed
from api.core.logging import error_text
from api.feed.models import (
    GLOBAL_SECTION,
    RUNTIME_SECTION,
    ConnectorSection,
    GlobalSection,
    RuntimeSection,
    Section,
)
from api.geo.models import LocationRules, eligible_under
from api.geo.service import GeoService
from api.models.connector import (
    DEFAULT_ROUTING_WEIGHT,
    Connector,
    RateLimitConfig,
    RoutingConfig,
    TrafficConfig,
)
from api.models.credential import Credential
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.provider import DescriptorProvider
from api.providers.sdk.strategies import META_GEO_COUNTRY, is_dynamic_gateway

if TYPE_CHECKING:
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()


@dataclass(frozen=True, eq=False)
class ConnectorView:
    """One connector section as routing reads it: every config parsed once, nothing changed afterwards."""

    section: ConnectorSection
    # None when the stored routing config is unreadable (hand-edited JSON): the
    # connector then serves no named host, rather than every host.
    routing: RoutingConfig | None
    # Relative share of the project's traffic; the default when the config is unreadable.
    weight: int
    # None without a rate limit, or when the stored value is unreadable.
    rate_limit: RateLimitConfig | None
    traffic: TrafficConfig
    # The countries the connector declares, as the compiler derived them; empty declares none.
    declared_countries: tuple[str, ...]
    # Feed order: (created_at, id).
    proxies: tuple[Proxy, ...]
    # Built from the section's credential for descriptor types. None for static and
    # cloud types, for a plugin with its own provider class, and for a type this
    # process does not know: their rows route as the feed has them.
    provider: DescriptorProvider | None
    # An "All countries" pool: it declares no country, and its provider provisions
    # a slot group for each country clients ask for.
    accepts_request_country: bool
    # The countries of the slot groups the connector holds (META_GEO_COUNTRY of its rows).
    geo_countries: frozenset[str]
    # (country asked or None, the project's location rules) -> the rows that can
    # serve it, filled as requests ask (see ``candidates``). The rows never
    # change, so neither does an entry; the view goes with its section.
    _candidates: dict[tuple[str | None, LocationRules], tuple[Proxy, ...]] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def build(cls, section: ConnectorSection, registry: ProviderRegistry) -> ConnectorView:
        """Index one connector section; the registry supplies the provider type its credential names."""
        connector = section.connector
        routing = cls._routing(connector)
        declared = tuple(section.declared_countries)
        try:
            provider = registry.descriptor_provider(connector, section.credential)
        except Exception as exc:
            # Logged, never raised: the connector's rows route as the feed has them.
            logger.warning(
                "Connector provider could not be built",
                connector_id=connector.id,
                provider=connector.credential_type,
                error=error_text(exc),
            )
            provider = None
        return cls(
            section=section,
            routing=routing,
            weight=routing.weight if routing is not None else DEFAULT_ROUTING_WEIGHT,
            rate_limit=cls._rate_limit(connector),
            traffic=connector.parsed_traffic_config,
            declared_countries=declared,
            proxies=tuple(section.proxies),
            provider=provider,
            accepts_request_country=not declared and provider is not None and provider.accepts_request_country(),
            geo_countries=cls._geo_countries(section.proxies),
        )

    @staticmethod
    def _routing(connector: Connector) -> RoutingConfig | None:
        """The connector's routing config; None, logged, when the stored value is unreadable."""
        try:
            return connector.parsed_routing_config
        except ValidationError as exc:
            logger.warning(
                "Connector routing config is unreadable; it serves no named host",
                connector_id=connector.id,
                errors=exc.error_count(),
            )
            return None

    @staticmethod
    def _rate_limit(connector: Connector) -> RateLimitConfig | None:
        """The connector's rate limit; None without one, and, logged, when the stored value is unreadable."""
        try:
            return connector.parsed_rate_limit_config
        except ValidationError as exc:
            logger.warning(
                "Connector rate limit config is unreadable; sessions are not held to quarantined proxies",
                connector_id=connector.id,
                errors=exc.error_count(),
            )
            return None

    @staticmethod
    def _geo_countries(proxies: Sequence[Proxy]) -> frozenset[str]:
        """The slot-group countries rows carry, compared as stored, like the provider's groups are."""
        return frozenset(
            geo for proxy in proxies if isinstance(geo := proxy.metadata.get(META_GEO_COUNTRY), str) and geo
        )

    @property
    def connector(self) -> Connector:
        return self.section.connector

    @property
    def credential(self) -> Credential | None:
        """The credential dynamic requests are rendered from; None for static and cloud connectors."""
        return self.section.credential

    @property
    def takes_requests(self) -> bool:
        """Enabled and not being deleted. Any other connector serves nothing: not routing, previews or on-demand."""
        return self.connector.enabled and not self.connector.pending_deletion

    def allows_host(self, host: str) -> bool:
        """Whether the connector's domain filter lets ``host`` through."""
        return self.routing is not None and is_domain_allowed(host, self.routing)

    def serves_location(self, target: LocationTarget) -> bool:
        """Whether a dynamic request through this connector can be rendered with every level ``target`` names."""
        return self.provider is not None and self.provider.serves_location(target)

    def has_geo_group(self, country: str) -> bool:
        """Whether some row of the connector carries ``country`` as its slot group, whatever its state."""
        return country in self.geo_countries

    def candidates(self, country: str | None, rules: LocationRules) -> tuple[Proxy, ...]:
        """The rows that can serve a request for ``country`` (None: no country) under ``rules``, in feed order.

        Worked out once per pair for the life of the view; what a gateway keeps out of routing now, and a state or city
        below the country, are the router's to check on top.
        """
        key = (country, rules)
        rows = self._candidates.get(key)
        if rows is None:
            declared = self.declared_countries
            if country and declared and country not in declared:
                rows = ()
            else:
                rows = tuple(proxy for proxy in self.proxies if self._can_serve(proxy, country, rules))
            self._candidates[key] = rows
        return rows

    def _can_serve(self, proxy: Proxy, country: str | None, rules: LocationRules) -> bool:
        """Whether a row of a connector that serves ``country`` at all is one of its ``candidates``."""
        if (
            proxy.status != ProxyStatus.HEALTHY
            or GeoService.outside_declared_countries(proxy, self.declared_countries)
            or not eligible_under(rules, proxy.metadata)
        ):
            return False
        if is_dynamic_gateway(proxy):
            # The connector's allow-list was applied above; the row itself has no country.
            return True
        if not country:
            # Untargeted traffic keeps off the on-demand groups of an "All countries" pool.
            return not (self.accepts_request_country and proxy.metadata.get(META_GEO_COUNTRY))
        known = proxy.country
        return known == country if known is not None else country in self.declared_countries


@dataclass(frozen=True, eq=False)
class _ProjectIndex:
    """The global section's projects, by id and by proxy username."""

    section: GlobalSection
    by_id: dict[str, Project]
    by_username: dict[str, Project]

    @classmethod
    def build(cls, section: GlobalSection) -> _ProjectIndex:
        return cls(
            section=section,
            by_id={project.id: project for project in section.projects},
            by_username={project.username: project for project in section.projects},
        )


class RoutingSnapshot:
    """One applied feed, indexed for routing; build it with :meth:`from_sections`."""

    def __init__(self, projects: _ProjectIndex, views: dict[str, ConnectorView], runtime: RuntimeSection) -> None:
        self._projects = projects
        self._views = views
        self._runtime = runtime
        # A project's connectors in creation order, so every gateway iterates them alike.
        self._by_project: dict[str, list[ConnectorView]] = {}
        for view in sorted(views.values(), key=lambda v: (v.connector.created_at, v.connector.id)):
            self._by_project.setdefault(view.connector.project_id, []).append(view)
        self._proxies = {proxy.id: proxy for view in views.values() for proxy in view.proxies}

    @classmethod
    def from_sections(
        cls,
        sections: Mapping[str, Section],
        *,
        registry: ProviderRegistry,
        previous: RoutingSnapshot | None = None,
    ) -> RoutingSnapshot:
        """The snapshot of a whole feed, a gateway's update or a project's compile, reusing ``previous``'s indexes.

        A connector section's index is reused while both it and the global section are the very objects ``previous`` was
        built from, since a new global section may have changed the registry its provider came from.
        """
        global_section = sections.get(GLOBAL_SECTION)
        if not isinstance(global_section, GlobalSection):
            global_section = GlobalSection()
        runtime = sections.get(RUNTIME_SECTION)
        if not isinstance(runtime, RuntimeSection):
            runtime = RuntimeSection()
        if previous is not None and previous._projects.section is global_section:
            projects = previous._projects
            reusable = previous._views
        else:
            projects = _ProjectIndex.build(global_section)
            reusable = {}
        views: dict[str, ConnectorView] = {}
        for section in sections.values():
            if not isinstance(section, ConnectorSection):
                continue
            old = reusable.get(section.connector.id)
            views[section.connector.id] = (
                old if old is not None and old.section is section else ConnectorView.build(section, registry)
            )
        return cls(projects, views, runtime)

    # --- projects -----------------------------------------------------------------------------

    def project(self, project_id: str) -> Project | None:
        return self._projects.by_id.get(project_id)

    def project_by_username(self, username: str) -> Project | None:
        """The project a proxy client authenticates as, by its username without routing suffixes."""
        return self._projects.by_username.get(username)

    # --- connectors and proxies ---------------------------------------------------------------

    def connector_view(self, connector_id: str) -> ConnectorView | None:
        return self._views.get(connector_id)

    def connector_views(self, project_id: str) -> Sequence[ConnectorView]:
        """The project's connectors in creation order, whether they take requests or not."""
        return self._by_project.get(project_id, ())

    def view_of(self, proxy_id: str) -> ConnectorView | None:
        """The connector a proxy belongs to; what request metering reads its limits from."""
        proxy = self._proxies.get(proxy_id)
        return self._views.get(proxy.connector_id) if proxy is not None else None

    def proxy(self, proxy_id: str) -> Proxy | None:
        """A proxy as the feed has it, retired or not; the router decides what routing may see."""
        return self._proxies.get(proxy_id)

    @property
    def proxy_ids(self) -> Set[str]:
        """Every proxy in the snapshot: what per-proxy local state is kept for."""
        return self._proxies.keys()

    @property
    def connector_ids(self) -> Set[str]:
        return self._views.keys()

    @property
    def quarantined(self) -> Mapping[str, datetime]:
        """Proxy id -> the end of the quarantine the feed carries for it (naive UTC), active when it was published."""
        return self._runtime.quarantined

    def has_geo_group(self, connector_id: str, country: str) -> bool:
        """Whether the connector holds a slot group for ``country``; what an on-demand wait waits for."""
        view = self._views.get(connector_id)
        return view is not None and view.has_geo_group(country)

    def is_traffic_blocked(self, connector_id: str) -> bool:
        """Whether the controller holds the connector back for its traffic limit now."""
        until = self._runtime.traffic_blocked_until.get(connector_id)
        return until is not None and until > utc_now()
