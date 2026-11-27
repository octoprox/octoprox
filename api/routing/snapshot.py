# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing snapshot: one applied feed, indexed for the request path and never changed afterwards.

A gateway builds one per feed update, and the api one per preview, through :meth:`RoutingSnapshot.from_sections`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.domain_filter import is_domain_allowed
from api.core.logging import error_text
from api.feed.models import (
    GLOBAL_SECTION,
    REPUTATION_SECTION,
    RUNTIME_SECTION,
    ConnectorSection,
    GlobalSection,
    ProjectReputation,
    ReputationSection,
    RuntimeSection,
    Section,
)
from api.geo.models import META_GEO_COUNTRY, LocationRules, eligible_under
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
    # The countries it reserves and excludes (routing config); none when that is unreadable.
    reserved: frozenset[str]
    excluded: frozenset[str]
    # The declared countries it does not reserve: what a request naming no country may be rendered in.
    untargeted_countries: tuple[str, ...]
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
        reserved = frozenset(routing.reserved_countries) if routing is not None else frozenset()
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
            reserved=reserved,
            excluded=frozenset(routing.excluded_countries) if routing is not None else frozenset(),
            untargeted_countries=tuple(country for country in declared if country not in reserved),
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

    def names_country(self, country: str) -> bool:
        """Whether the connector takes requests naming ``country`` at all: listed when it lists any, and not excluded."""
        declared = self.declared_countries
        return country not in self.excluded and (not declared or country in declared)

    def has_geo_group(self, country: str) -> bool:
        """Whether some row of the connector carries ``country`` as its slot group, whatever its state."""
        return country in self.geo_countries

    @cached_property
    def dynamic(self) -> bool:
        """Whether the connector serves through a dynamic gateway row, whose exit is chosen per request: exit reputation
        weighs such a connector by its record for the site, never its exit IPs."""
        return any(proxy.is_dynamic_gateway for proxy in self.proxies)

    @cached_property
    def exits(self) -> dict[str, tuple[str, ...]]:
        """Exit IP (``static_exit_ip``) -> the ids of the rows last seen exiting there, whatever their status, in feed
        order: what exit reputation is joined to. Worked out on the first ask, for the life of the view."""
        exits: dict[str, list[str]] = {}
        for proxy in self.proxies:
            if (ip := proxy.static_exit_ip) is not None:
                exits.setdefault(ip, []).append(proxy.id)
        return {ip: tuple(ids) for ip, ids in exits.items()}

    def candidates(self, country: str | None, rules: LocationRules) -> tuple[Proxy, ...]:
        """The rows that can serve a request for ``country`` (None: no country) under ``rules``, in feed order.

        Worked out once per pair for the life of the view; what a gateway keeps out of routing now, and a state or city
        below the country, are the router's to check on top.
        """
        key = (country, rules)
        rows = self._candidates.get(key)
        if rows is None:
            if country and not self.names_country(country):
                rows = ()
            else:
                rows = tuple(proxy for proxy in self.proxies if self._can_serve(proxy, country, rules))
            self._candidates[key] = rows
        return rows

    def can_serve(self, proxy: Proxy, country: str | None, rules: LocationRules, *, pinned: bool = False) -> bool:
        """Whether ``proxy`` is one of the ``candidates`` for ``country``, checked alone rather than by building them.

        ``pinned``, a request naming the row, may use an on-demand country group's slot without naming its country.
        """
        if country and not self.names_country(country):
            return False
        return self._can_serve(proxy, country, rules, pinned=pinned)

    def _can_serve(self, proxy: Proxy, country: str | None, rules: LocationRules, *, pinned: bool = False) -> bool:
        """Whether a row of a connector that serves ``country`` at all is one of its ``candidates``."""
        known = None if proxy.is_dynamic_gateway else proxy.country
        if (
            proxy.status != ProxyStatus.HEALTHY
            or proxy.outside_declared_countries(self.declared_countries)
            or (known is not None and known in self.excluded)
            or not eligible_under(rules, proxy.metadata)
        ):
            return False
        if not country and not pinned and self._exits_in_reserved(known):
            return False
        if proxy.is_dynamic_gateway:
            # The connector's allow-list was applied above; the row itself has no country.
            return True
        if not country:
            # Untargeted traffic keeps off the on-demand groups of an "All countries" pool, unless it named the slot.
            return pinned or not (self.accepts_request_country and proxy.metadata.get(META_GEO_COUNTRY))
        return known == country if known is not None else country in self.declared_countries

    def _exits_in_reserved(self, known: str | None) -> bool:
        """Whether a row of known country ``known`` exits in a reserved country, which untargeted traffic keeps off.

        A row of unknown country, a dynamic gateway among them, counts as one when every country the connector lists is
        reserved; with no list, a gateway renders no country and the vendor picks.
        """
        if not self.reserved:
            return False
        if known is not None:
            return known in self.reserved
        return bool(self.declared_countries) and not self.untargeted_countries


@dataclass(frozen=True, eq=False)
class _ProjectIndex:
    """The global section's projects, by id and by proxy username; one pending deletion has no username, so its clients are turned away."""

    section: GlobalSection
    by_id: dict[str, Project]
    by_username: dict[str, Project]

    @classmethod
    def build(cls, section: GlobalSection) -> _ProjectIndex:
        return cls(
            section=section,
            by_id={project.id: project for project in section.projects},
            by_username={project.username: project for project in section.projects if not project.pending_deletion},
        )


class _SiteRows:
    """The rows exit reputation avoids for one site of a project, each with the end of its avoid.

    Which are in force at a moment is worked out once and kept until the first of their ends, so a request reads a set
    whatever the number of rows.
    """

    __slots__ = ("_ends", "_live", "_since", "_until")

    def __init__(self) -> None:
        self._ends: dict[str, datetime] = {}
        # The rows in force when last worked out, and the moments that answer holds between.
        self._live: frozenset[str] = frozenset()
        self._since = datetime.max
        self._until = datetime.min

    def add(self, proxy_ids: Iterable[str], end: datetime) -> None:
        """Name the rows ``proxy_ids``, all exiting from one IP, with that IP's ``end`` for the site."""
        for proxy_id in proxy_ids:
            self._ends[proxy_id] = end
        self._until = datetime.min

    def at(self, now: datetime) -> frozenset[str]:
        """The rows whose end is after ``now``."""
        if not self._since <= now < self._until:
            live = {proxy_id: end for proxy_id, end in self._ends.items() if end > now}
            self._live = frozenset(live)
            self._since, self._until = now, min(live.values(), default=datetime.max)
        return self._live


@dataclass(frozen=True, eq=False)
class _ReputationIndex:
    """One project's exit reputation as its requests read it, per site: the rows avoided there (its exit IPs joined to
    its rows), and its dynamic connectors' factors by level of the requested place."""

    # What it was built from: a later snapshot keeps it while the views are the very same objects and the records are
    # equal, since a gateway decodes the whole section anew on every change of it.
    source: ProjectReputation
    views: tuple[ConnectorView, ...]
    avoided: dict[str, _SiteRows]
    # Site -> dynamic connector id -> level of the requested place -> factor.
    factors: dict[str, dict[str, Mapping[str, float]]]

    @classmethod
    def build(cls, source: ProjectReputation, views: Sequence[ConnectorView]) -> _ReputationIndex:
        """The index of the project's section over the rows of its connectors."""
        ips = source.ip_avoids
        avoided: dict[str, _SiteRows] = {}
        for view in views if ips else ():
            exits = view.exits
            if not exits:
                # A dynamic connector's rows hold no exit.
                continue
            # The IPs both hold, found from the smaller side, so a large pool or a long map costs the other's size.
            for ip in exits.keys() & ips.keys():
                rows = exits[ip]
                for site, end in ips[ip].items():
                    avoided.setdefault(site, _SiteRows()).add(rows, end)
        factors: dict[str, dict[str, Mapping[str, float]]] = {}
        for connector_id, by_site in source.connector_factors.items():
            for site, by_place in by_site.items():
                factors.setdefault(site, {})[connector_id] = by_place
        return cls(source, tuple(views), avoided, factors)

    def built_from(self, source: ProjectReputation, views: Sequence[ConnectorView]) -> bool:
        """Whether it was built over these very connector views from records equal to ``source``, so it holds as it is."""
        return (
            len(self.views) == len(views)
            and all(kept is view for kept, view in zip(self.views, views, strict=True))
            and (self.source is source or self.source == source)
        )


# What a project without exit reputation, or a site nothing is recorded for, has in force.
_NO_ROWS: frozenset[str] = frozenset()
_NO_FACTORS: Mapping[str, Mapping[str, float]] = MappingProxyType({})


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
        # (project id, country) -> the connectors reserving it, in creation order. A disabled connector keeps its
        # reservations, so requests for its countries fail; one being deleted holds none.
        self._reserving: dict[tuple[str, str], list[ConnectorView]] = {}
        for project_id, project_views in self._by_project.items():
            for view in project_views:
                if view.connector.pending_deletion:
                    continue
                for country in view.reserved:
                    self._reserving.setdefault((project_id, country), []).append(view)
        # Project id -> its exit reputation as requests read it, built when the feed is applied (see from_sections).
        self._reputation: dict[str, _ReputationIndex] = {}

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
        built from, since a new global section may have changed the registry its provider came from. Each project's exit
        reputation is indexed here, never on a request, and reused while its part of the section is equal and its
        connectors' indexes are the very objects ``previous`` built it from.
        """
        global_section = sections.get(GLOBAL_SECTION)
        if not isinstance(global_section, GlobalSection):
            global_section = GlobalSection()
        runtime = sections.get(RUNTIME_SECTION)
        if not isinstance(runtime, RuntimeSection):
            runtime = RuntimeSection()
        reputation = sections.get(REPUTATION_SECTION)
        if not isinstance(reputation, ReputationSection):
            # A preview's compile carries none.
            reputation = ReputationSection()
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
        snapshot = cls(projects, views, runtime)
        snapshot._index_reputation(reputation, previous)
        return snapshot

    def _index_reputation(self, section: ReputationSection, previous: RoutingSnapshot | None) -> None:
        """Index each project's exit reputation over its connectors, keeping ``previous``'s index of a project while it
        holds as it is."""
        kept = previous._reputation if previous is not None else {}
        for project_id, reputation in section.projects.items():
            views = self.connector_views(project_id)
            index = kept.get(project_id)
            if index is None or not index.built_from(reputation, views):
                index = _ReputationIndex.build(reputation, views)
            elif index.source is not reputation:
                # Equal records decoded anew: keep the index, holding the new copy so the old one is freed.
                index = replace(index, source=reputation)
            self._reputation[project_id] = index

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

    def reserving(self, project_id: str, country: str) -> Sequence[ConnectorView]:
        """The project's connectors reserving ``country``, whether they take requests or not, in creation order."""
        return self._reserving.get((project_id, country), ())

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

    # --- exit reputation ----------------------------------------------------------------------

    def avoided(self, project_id: str, site: str, now: datetime) -> frozenset[str]:
        """The project's rows exit reputation keeps away from ``site`` at ``now``: those whose exit IP is avoided there.

        The feed carries it only for projects whose exit reputation avoids, and only for rows with a known exit.
        """
        index = self._reputation.get(project_id)
        rows = index.avoided.get(site) if index is not None else None
        return rows.at(now) if rows is not None else _NO_ROWS

    def factors(self, project_id: str, site: str) -> Mapping[str, Mapping[str, float]]:
        """The factors of the project's dynamic connectors for ``site``: connector id -> level of the requested place
        ("" for none) -> factor. The feed carries them only for projects whose exit reputation avoids."""
        index = self._reputation.get(project_id)
        factors = index.factors.get(site) if index is not None else None
        return factors if factors is not None else _NO_FACTORS
