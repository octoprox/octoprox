# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The gateway's router: which proxy a request goes through, from the applied routing snapshot and what only this gateway knows.

Eligibility comes first, then the project's strategy picks the connector by weight and the proxy inside it.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, NamedTuple, Protocol

import structlog

from api.core import utc_now
from api.core.logging import error_text
from api.feed.sections import resolve_credentials
from api.geo.models import LocationRules, eligible_under
from api.models.connect import ConnectPlace, ConnectRoute
from api.models.connector import DEFAULT_ROUTING_WEIGHT, DEFAULT_TRAFFIC_LIMIT_STATUS
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.strategies import META_SESSION_ID, is_dynamic_gateway
from api.routing.exclusions import LocalExclusions
from api.routing.snapshot import ConnectorView, RoutingSnapshot
from api.strategies import ProxyGroup, RoutingStrategy, get_strategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.routing.inflight import InFlight
    from api.routing.outcomes import LocalOutcomes

logger = structlog.get_logger()

# How long a request for a country no slot group serves yet waits for the
# group to arrive through the feed, in seconds. Each hand-off on the way
# (the job, the insert's change entry, the feed's entry) wakes its reader
# within milliseconds, so the wait is the provision job and the controller's
# compile: about a second with an idle worker, longer when the controller
# compiled within the last feed_compile_interval or compiles a large install;
# past this the request gets the refusal a country nothing serves gets. It is
# also how long an ask for a group stands: a group not there by then is asked
# for again by the next request that misses it.
ON_DEMAND_WAIT_SECONDS = 5.0


class OnDemand(Protocol):
    """How the router gets a country slot group a pool lacks: ask for it, then wait for the feed to bring it."""

    async def request(self, connector_id: str, country: str) -> None:
        """Ask for the connector's slot group for ``country`` (a ``provision`` job)."""
        ...

    async def wait(self, predicate: Callable[[RoutingSnapshot], bool], timeout: float) -> bool:
        """Wait until an applied snapshot satisfies ``predicate``, at most ``timeout`` seconds; whether one did."""
        ...


class _NoOnDemand:
    """For a router that cannot wait for the feed (the api's previews): nothing is asked for, nothing arrives."""

    async def request(self, connector_id: str, country: str) -> None:
        return None

    async def wait(self, predicate: Callable[[RoutingSnapshot], bool], timeout: float) -> bool:
        return False


class _Ask(NamedTuple):
    """One ask for a slot group: when it was made (monotonic seconds) and the request for the job."""

    at: float
    task: asyncio.Task[None]


class Router:
    """Selects the proxy for each request from the applied snapshot; one per gateway, swapped on every feed apply.

    Everything but the snapshot may be left out, routing then as a fresh gateway without Redis would, for the api's previews.

    Args:
        snapshot: what to route on until the first ``swap``.
        redis_client: where sticky bindings live; None keeps them to this process.
        default_strategy: the strategy of a project the snapshot does not hold.
    """

    def __init__(
        self,
        snapshot: RoutingSnapshot,
        *,
        exclusions: LocalExclusions | None = None,
        inflight: InFlight | None = None,
        outcomes: LocalOutcomes | None = None,
        on_demand: OnDemand | None = None,
        redis_client: RedisClient | None = None,
        default_strategy: str = "round_robin",
    ) -> None:
        self._snapshot = snapshot
        self._exclusions = exclusions if exclusions is not None else LocalExclusions()
        self._inflight = inflight
        self._outcomes = outcomes
        self._on_demand: OnDemand = on_demand if on_demand is not None else _NoOnDemand()
        self._redis = redis_client
        self._default = get_strategy(default_strategy, inflight=inflight, outcomes=outcomes)
        # Project id -> (the routing_strategy it was built for, the strategy), kept across swaps.
        self._strategies: dict[str, tuple[str, RoutingStrategy]] = {}
        # (connector id, country) -> this gateway's last ask for that slot group,
        # until the group arrives or a newer ask replaces it.
        self._asks: dict[tuple[str, str], _Ask] = {}

    @property
    def snapshot(self) -> RoutingSnapshot:
        """The snapshot routing reads now."""
        return self._snapshot

    def swap(self, snapshot: RoutingSnapshot) -> None:
        """Route on ``snapshot`` from now on, keeping each project's strategy state wherever it still applies.

        Per-connector state goes with the connectors the snapshot no longer has, and an ask for a slot group once the
        group is here or the ask is too old to hold back a new one.
        """
        previous, self._snapshot = self._snapshot, snapshot
        for project_id in [pid for pid in self._strategies if snapshot.project(pid) is None]:
            del self._strategies[project_id]
        for connector_id in previous.connector_ids - snapshot.connector_ids:
            self._default.forget_group(connector_id)
            for _, strategy in self._strategies.values():
                strategy.forget_group(connector_id)
        now = time.monotonic()
        self._asks = {
            key: ask
            for key, ask in self._asks.items()
            if not snapshot.has_geo_group(*key) and (not ask.task.done() or now - ask.at < ON_DEMAND_WAIT_SECONDS)
        }

    async def settle(self) -> None:
        """Wait for the writes each strategy has under way; the gateway calls it once its listeners are closed."""
        for _, strategy in list(self._strategies.values()):
            await strategy.settle()
        await self._default.settle()

    # --- Lookups ----------------------------------------------------------------------------

    def get_project(self, project_id: str) -> Project | None:
        return self._snapshot.project(project_id)

    def get_project_by_username(self, username: str) -> Project | None:
        """The project a proxy client authenticates as; ``username`` carries no routing suffixes."""
        return self._snapshot.project_by_username(username)

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """A proxy of the applied feed; None when the feed lacks it or this gateway retired it.

        The exit verifier reads None as "the slot left the pool while it was
        being checked", so a slot preflight retired answers None at once.
        """
        if self._exclusions.is_retired(proxy_id):
            return None
        return self._snapshot.proxy(proxy_id)

    def declared_countries(self, connector_id: str) -> list[str]:
        """The countries a connector declares; empty when it declares none or is unknown."""
        view = self._snapshot.connector_view(connector_id)
        return list(view.declared_countries) if view is not None else []

    def strategy_for_project(self, project_id: str) -> RoutingStrategy:
        """The routing strategy in force for a project: the one the feed names, or the default for a project the snapshot does not hold."""
        project = self._snapshot.project(project_id)
        if project is None:
            return self._default
        held = self._strategies.get(project_id)
        if held is not None and held[0] == project.routing_strategy:
            return held[1]
        strategy = get_strategy(project.routing_strategy, inflight=self._inflight, outcomes=self._outcomes)
        self._strategies[project_id] = (project.routing_strategy, strategy)
        return strategy

    def is_traffic_blocked(self, connector_id: str) -> bool:
        """Whether the connector's traffic limit holds it back now; running transfers ask this too."""
        return self._snapshot.is_traffic_blocked(connector_id)

    # --- Selection --------------------------------------------------------------------------

    async def select_proxy_for_project(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
        location: LocationTarget | None = None,
        exclude: frozenset[str] | None = None,
    ) -> Proxy | None:
        """The proxy for one request of the project, credentials filled in; None when nothing serves it.

        A sticky session held to its quarantined proxy (``sticky_quarantine``) gets None, a 429, and only a ``-cc-``
        request, never a state or city, may wait for an on-demand country group.

        Args:
            session_id: the client's ``-sessid-``, which sticky projects route by and a dynamic gateway's session derives from.
            exclude: proxies dropped before the strategy runs (preflight's retry); a session bound to one is re-bound.
        """
        if await self._is_sticky_quarantine_blocked(project_id, session_id):
            return None
        target = location or None
        snapshot = self._snapshot
        routable = self._eligible_proxies(snapshot, project_id, target_host, target, include_quarantined=False)
        if (
            not routable
            and target is not None
            and target.country
            and not target.below_country
            and await self._await_country_group(snapshot, project_id, target_host, target.country)
        ):
            snapshot = self._snapshot
            routable = self._eligible_proxies(snapshot, project_id, target_host, target, include_quarantined=False)
        if exclude:
            routable = [proxy for proxy in routable if proxy.id not in exclude]
        # The connector by routing weight, then the proxy inside it. Sticky
        # reads and writes its Redis binding inside; the others need no I/O.
        selected = await self.strategy_for_project(project_id).select_weighted(
            self._groups(snapshot, routable), session_id, redis_client=self._redis, project_id=project_id
        )
        if selected is None or not is_dynamic_gateway(selected):
            return selected
        return self._render(snapshot, selected, project_id, session_id, target)

    async def are_all_proxies_quarantined(
        self,
        project_id: str,
        target_host: str | None = None,
        session_id: str | None = None,
        location: LocationTarget | None = None,
    ) -> bool:
        """Whether a request found nothing because of rate-limit quarantines (429), not because nothing serves it (502).

        True when the session is held to its quarantined sticky proxy, or every row that would serve it is quarantined;
        a retired slot is gone, not resting.
        """
        if await self._is_sticky_quarantine_blocked(project_id, session_id):
            return True
        snapshot = self._snapshot
        target = location or None
        candidates = self._eligible_proxies(snapshot, project_id, target_host, target, include_quarantined=True)
        return bool(candidates) and all(self._is_quarantined(snapshot, proxy.id) for proxy in candidates)

    def traffic_limit_status(
        self,
        project_id: str,
        target_host: str | None = None,
        location: LocationTarget | None = None,
    ) -> int | None:
        """The status to answer with when only connectors over their traffic limit could serve the request.

        None when some connector is still eligible or none would be even without the blocks (a 502), and 509 when the
        blocked connectors disagree, since it cannot be mistaken for anything else.
        """
        snapshot = self._snapshot
        target = location or None
        if self._eligible_proxies(snapshot, project_id, target_host, target, include_quarantined=True):
            return None
        held_back = self._eligible_proxies(
            snapshot, project_id, target_host, target, include_quarantined=True, include_traffic_blocked=True
        )
        statuses: set[int] = set()
        for connector_id in {proxy.connector_id for proxy in held_back}:
            view = snapshot.connector_view(connector_id)
            if view is not None and snapshot.is_traffic_blocked(connector_id):
                statuses.add(view.traffic.limit_status)
        if not statuses:
            return None
        return statuses.pop() if len(statuses) == 1 else DEFAULT_TRAFFIC_LIMIT_STATUS

    # --- Previews ---------------------------------------------------------------------------

    def get_routable_proxies_for_project(
        self,
        project_id: str,
        target_host: str | None = None,
        location: LocationTarget | None = None,
    ) -> list[Proxy]:
        """The proxies a request may be routed to now, before the strategy picks one, by the eligibility of selection.

        Quarantined and retired rows are out, and without a location so are the on-demand groups of "All countries" pools.
        """
        target = location or None
        return self._eligible_proxies(self._snapshot, project_id, target_host, target, include_quarantined=False)

    def group_by_connector(self, proxies: list[Proxy]) -> list[ProxyGroup]:
        """Proxies grouped per connector, each group carrying its connector's routing weight.

        Groups keep the order in which their connectors first appear; a
        connector the snapshot does not know gets the default weight.
        """
        return self._groups(self._snapshot, proxies)

    def route_preview(self, project_id: str, location: LocationTarget | None) -> list[ConnectRoute]:
        """The connectors a request for ``location`` would be spread over now, each with its share by routing weight.

        Nothing is selected or provisioned; when nothing serves a country, the pools that would provision it are listed,
        marked ``on_demand``, and no destination filter applies.
        """
        snapshot = self._snapshot
        target = location or None
        by_connector: dict[str, list[Proxy]] = {}
        for proxy in self._eligible_proxies(snapshot, project_id, None, target, include_quarantined=False):
            by_connector.setdefault(proxy.connector_id, []).append(proxy)
        wanted = target.country if target is not None and not target.below_country and not by_connector else None
        if wanted is not None:
            views = self._on_demand_pools(snapshot, project_id, None, wanted)
        else:
            enabled = self._enabled_connectors(snapshot, project_id, None)
            views = [view for view in enabled if view.connector.id in by_connector]
        total = sum(max(view.weight, 1) for view in views)
        return [
            ConnectRoute(
                connector_id=view.connector.id,
                connector_name=view.connector.name,
                dynamic=any(is_dynamic_gateway(proxy) for proxy in by_connector.get(view.connector.id, ())),
                on_demand=wanted is not None,
                proxies=len(by_connector.get(view.connector.id, ())),
                share=max(view.weight, 1) / total,
            )
            for view in views
        ]

    def served_places(self, project_id: str, limit: int = 200) -> tuple[list[str], bool, list[ConnectPlace]]:
        """What a client of the project can ask for now, as hints: ``(countries, any_country, places)``.

        ``any_country`` says a country nobody lists is served too, and places are the states and cities healthy fixed
        exits are in, busiest first, at most ``limit``.
        """
        snapshot = self._snapshot
        countries: set[str] = set()
        any_country = False
        places: Counter[tuple[str, str | None, str | None]] = Counter()
        for view in self._enabled_connectors(snapshot, project_id, None):
            countries.update(view.declared_countries)
            any_country = any_country or view.accepts_request_country
            for proxy in view.proxies:
                if proxy.status != ProxyStatus.HEALTHY or self._is_excluded(snapshot, proxy.id):
                    continue
                if is_dynamic_gateway(proxy):
                    any_country = any_country or not view.declared_countries
                    continue
                country = proxy.country
                if country is None:
                    continue
                countries.add(country)
                state, city = proxy.state_code, proxy.city_slug
                if (state or city) and not proxy.metadata.get(META_SESSION_ID):
                    places[(country, state, city)] += 1
        return (
            sorted(countries),
            any_country,
            [
                ConnectPlace(country=country, state=state, city=city, proxies=count)
                for (country, state, city), count in places.most_common(limit)
            ],
        )

    # --- Eligibility ------------------------------------------------------------------------
    #
    # A connector declares the countries its proxies exit from (``countries``
    # on static and cloud connectors, the provider's country field on
    # descriptor connectors). Given a requested country:
    #   - connectors declaring countries serve it only if they declare it;
    #   - a proxy with a known exit country (vendor-reported ``country`` or the
    #     ``geo`` it was provisioned for) must match exactly;
    #   - a proxy with no known country is eligible only through its
    #     connector's list;
    #   - descriptor pools whose credentials carry a country and declare none
    #     ("All countries") get a slot group for the country on demand;
    #   - a dynamic-sessions gateway row serves any country its connector
    #     allows (every country when it declares none): the code is rendered
    #     into the vendor credentials per request, so nothing is provisioned.
    # Without a requested country every proxy is eligible, except the on-demand
    # groups of "All countries" pools, which keep serving only the clients that
    # asked for their country.
    # A state or city below the country is served only by dynamic-sessions
    # gateways whose descriptor renders it and by fixed exits whose known
    # region and city match; nothing is provisioned for it.

    def _enabled_connectors(
        self,
        snapshot: RoutingSnapshot,
        project_id: str,
        target_host: str | None,
        *,
        include_traffic_blocked: bool = False,
    ) -> list[ConnectorView]:
        """The project's connectors that take requests and let ``target_host`` through.

        A connector held back by its traffic limit is left out unless
        ``include_traffic_blocked`` asks for it, to tell "over the limit" from
        "nothing configured" when a request finds no proxy.
        """
        return [
            view
            for view in snapshot.connector_views(project_id)
            if view.takes_requests
            and (not target_host or view.allows_host(target_host))
            and (include_traffic_blocked or not snapshot.is_traffic_blocked(view.connector.id))
        ]

    def _eligible_proxies(
        self,
        snapshot: RoutingSnapshot,
        project_id: str,
        target_host: str | None,
        target: LocationTarget | None,
        *,
        include_quarantined: bool,
        include_traffic_blocked: bool = False,
    ) -> list[Proxy]:
        """Healthy proxies a request for ``target`` may use, after domain and location filtering and the project's rules.

        A state or city keeps the dynamic gateways that render it and the fixed exits known to be there.
        """
        wanted = target.country if target is not None else None
        project = snapshot.project(project_id)
        rules = project.location_rules if project is not None else LocationRules()
        excluded = self._excluded(snapshot, include_quarantined=include_quarantined)
        eligible: list[Proxy] = []
        for view in self._enabled_connectors(
            snapshot, project_id, target_host, include_traffic_blocked=include_traffic_blocked
        ):
            for proxy in view.candidates(wanted or None, rules):
                if proxy.id in excluded:
                    continue
                if target is not None and target.below_country and not self._serves_place(view, proxy, rules, target):
                    continue
                eligible.append(proxy)
        return eligible

    def _excluded(self, snapshot: RoutingSnapshot, *, include_quarantined: bool) -> set[str]:
        """The proxies no request may use now: retired on this gateway, and quarantined unless included."""
        excluded = self._exclusions.retired_ids()
        if not include_quarantined:
            now = utc_now()
            excluded |= self._exclusions.quarantined_ids()
            excluded.update(proxy_id for proxy_id, until in snapshot.quarantined.items() if until > now)
        return excluded

    @staticmethod
    def _serves_place(view: ConnectorView, proxy: Proxy, rules: LocationRules, target: LocationTarget) -> bool:
        """Whether a row serves the state or city ``target`` names: rendered there, or known to be there."""
        if not eligible_under(rules, proxy.metadata, target):
            return False
        return view.serves_location(target) if is_dynamic_gateway(proxy) else proxy.location_matches(target)

    def _is_quarantined(self, snapshot: RoutingSnapshot, proxy_id: str) -> bool:
        """Whether a rate-limit quarantine holds the proxy now: this gateway's own trip, or the feed's."""
        if self._exclusions.quarantined_until(proxy_id) is not None:
            return True
        until = snapshot.quarantined.get(proxy_id)
        return until is not None and until > utc_now()

    def _is_excluded(self, snapshot: RoutingSnapshot, proxy_id: str) -> bool:
        """Whether the proxy is out of routing on this gateway now: retired, or quarantined."""
        return self._exclusions.is_retired(proxy_id) or self._is_quarantined(snapshot, proxy_id)

    @staticmethod
    def _groups(snapshot: RoutingSnapshot, proxies: list[Proxy]) -> list[ProxyGroup]:
        groups: dict[str, ProxyGroup] = {}
        for proxy in proxies:
            group = groups.get(proxy.connector_id)
            if group is None:
                view = snapshot.connector_view(proxy.connector_id)
                weight = view.weight if view is not None else DEFAULT_ROUTING_WEIGHT
                group = groups[proxy.connector_id] = ProxyGroup(key=proxy.connector_id, weight=weight)
            group.proxies.append(proxy)
        return list(groups.values())

    # --- On-demand country groups -----------------------------------------------------------

    def _on_demand_pools(
        self, snapshot: RoutingSnapshot, project_id: str, target_host: str | None, country: str
    ) -> list[ConnectorView]:
        """The "All countries" pools holding no row of a ``country`` slot group: the misses the feed may fill."""
        return [
            view
            for view in self._enabled_connectors(snapshot, project_id, target_host)
            if view.accepts_request_country and not view.has_geo_group(country)
        ]

    async def _await_country_group(
        self, snapshot: RoutingSnapshot, project_id: str, target_host: str | None, country: str
    ) -> bool:
        """Ask for the missing slot groups of ``country`` and wait for one to arrive; whether one did.

        Requests that miss a group while an ask for it stands share the ask and its outcome, and one that failed refuses
        them all at once.
        """
        pools = [view.connector.id for view in self._on_demand_pools(snapshot, project_id, target_host, country)]
        if not pools:
            return False
        try:
            # Shielded: a request that goes away must not cancel the ask the others share.
            await asyncio.gather(*(asyncio.shield(self._ask(connector_id, country)) for connector_id in pools))
        except Exception:
            # Logged once per failed ask, where it fails (``_asked``).
            return False
        arrived = await self._on_demand.wait(
            lambda applied: any(applied.has_geo_group(connector_id, country) for connector_id in pools),
            ON_DEMAND_WAIT_SECONDS,
        )
        if not arrived:
            logger.debug("Country slot group did not arrive in time", project_id=project_id, country=country)
        return arrived

    def _ask(self, connector_id: str, country: str) -> asyncio.Task[None]:
        """The ask for one slot group that stands: the one under way or made within a wait, else a new one."""
        key = (connector_id, country)
        now = time.monotonic()
        ask = self._asks.get(key)
        if ask is None or (ask.task.done() and now - ask.at >= ON_DEMAND_WAIT_SECONDS):
            ask = self._asks[key] = _Ask(now, asyncio.create_task(self._on_demand.request(connector_id, country)))
            ask.task.add_done_callback(partial(self._asked, key))
        return ask.task

    def _asked(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        """An ask is done: one that failed is logged and forgotten, so the next miss asks again at once."""
        error = None if task.cancelled() else task.exception()
        if error is None and not task.cancelled():
            return
        held = self._asks.get(key)
        if held is not None and held.task is task:
            del self._asks[key]
        if error is not None:
            logger.warning(
                "Country slot group could not be requested",
                connector_id=key[0],
                country=key[1],
                error=error_text(error),
            )

    # --- Sticky quarantine, strategies, rendering -------------------------------------------

    async def _is_sticky_quarantine_blocked(self, project_id: str, session_id: str | None) -> bool:
        """Whether the session is held to its quarantined proxy instead of moving to another: projects with ``sticky_quarantine``.

        The project's strategy says which proxy it holds the session to (only sticky holds any, wherever the binding was
        made); a retired slot holds no session.
        """
        if session_id is None:
            return False
        strategy = self.strategy_for_project(project_id)
        if not any(
            view.rate_limit is not None and view.rate_limit.sticky_quarantine
            for view in self._snapshot.connector_views(project_id)
        ):
            return False
        bound = await strategy.held_proxy(session_id, redis_client=self._redis, project_id=project_id)
        snapshot = self._snapshot
        proxy = snapshot.proxy(bound) if bound else None
        view = snapshot.connector_view(proxy.connector_id) if proxy is not None else None
        return not (
            proxy is None
            or view is None
            or proxy.status != ProxyStatus.HEALTHY
            or self._exclusions.is_retired(proxy.id)
            or not self._is_quarantined(snapshot, proxy.id)
            or view.rate_limit is None
            or not view.rate_limit.sticky_quarantine
        )

    @staticmethod
    def _render(
        snapshot: RoutingSnapshot,
        proxy: Proxy,
        project_id: str,
        session_id: str | None,
        location: LocationTarget | None,
    ) -> Proxy:
        """The gateway row as one request uses it: the vendor session and place rendered, credentials filled."""
        view = snapshot.connector_view(proxy.connector_id)
        provider = view.provider if view is not None else None
        if view is None or provider is None or not provider.is_dynamic:
            # The row says dynamic but its connector no longer does (mid-sync): use it as the feed has it.
            return proxy
        rendered = provider.render_request(proxy, sessid=session_id, location=location, scope=project_id)
        # Rendering puts the secrets back as placeholders; the section's credential fills them.
        return resolve_credentials(rendered, view.credential, view.connector)
