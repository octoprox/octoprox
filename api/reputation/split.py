# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the api shows it: how a project's requests to a site from a place would split between its
connectors by their effective weights there, with each one's reputation and the counts behind it, for the place the
traffic split asks about or for every place a site has records for (:class:`SitePlaces`).

The records are read from Postgres, the rows come from the preview router as eligible for a request to the site from the
place, and each reputation is made by the rule gateways route by (:class:`~api.reputation.weight.ConnectorReputation`);
see docs/api.md (Traffic split, Site across places).
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import cached_property
from itertools import takewhile
from types import MappingProxyType
from typing import TYPE_CHECKING

from api.db.reputation_repository import ExitReputationRepository
from api.models.location import COUNTRY_NAMES, LocationTarget, state_name_slug
from api.reputation.record import ConnectorSiteReputation, ReputationPolicy, place_levels
from api.reputation.weight import ConnectorReputation, PlaceState
from api.routing.router import RequestReputation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.connector import Connector
    from api.models.project import Project
    from api.routing.router import Router
    from api.routing.snapshot import ConnectorView
    from api.strategies import ProxyGroup

# How the place of a request naming none reads.
NO_PLACE_LABEL = "No place named"
# Under this a connector's value counts as heavily lowered on its tile.
HEAVILY_LOWERED = 0.5
# At most this many eligible rows at a state or a city, a connector with IPs is never the hardest hit there.
THIN_ROWS = 2


def place_target(place: str) -> LocationTarget | None:
    """The request target a place names as a ``LocationTarget`` key ("US", "US/NY", "US/NY/new_york", "US//austin");
    None for "" (no place). ValueError, with the reason, when the text names no place."""
    parts = place.split("/")
    if len(parts) > 3:
        raise ValueError(f"not a place: {place}")
    country, state, city = (*parts, "", "")[:3]
    return LocationTarget.parse(country or None, state or None, city or None)


def place_key(text: str | None) -> str:
    """A requested place as a ``LocationTarget`` key, in its canonical form; "" for none. ValueError, with the reason,
    when the text names no place."""
    if text is None or not text.strip():
        return ""
    target = place_target(text.strip())
    return target.key if target is not None else ""


def place_label(place: str) -> str:
    """How a requested place reads: a country by its name ("United States"), a state with its country ("New York,
    United States", or "ENG, United Kingdom" without a name on record), a city with its state, else its country
    ("New York, NY", "London, United Kingdom"). A code with no name on record reads as itself."""
    if not place:
        return NO_PLACE_LABEL
    country, _, below = place.partition("/")
    country_name = COUNTRY_NAMES.get(country, country)
    if not below:
        return country_name
    state, _, city = below.partition("/")
    if city:
        return f"{_words(city)}, {state or country_name}"
    name = state_name_slug(country, state)
    return f"{_words(name) if name else state}, {country_name}"


def _words(slug: str) -> str:
    """A place slug as words: "new_york" reads "New York"."""
    return " ".join(word.capitalize() for word in slug.split("_") if word)


class PlaceLevel(StrEnum):
    """How fine a requested place is."""

    NONE = "none"
    COUNTRY = "country"
    STATE = "state"
    CITY = "city"

    @property
    def depth(self) -> int:
        """How many levels the place names, by declaration order: 0 for no place, 3 for a city."""
        return list(PlaceLevel).index(self)

    @classmethod
    def of(cls, place: str) -> PlaceLevel:
        """The level ``place`` (a ``LocationTarget`` key) names."""
        if not place:
            return cls.NONE
        _country, _, below = place.partition("/")
        if not below:
            return cls.COUNTRY
        return cls.CITY if below.partition("/")[2] else cls.STATE


@dataclass(frozen=True, slots=True)
class ConnectorShare:
    """One connector's part of a site's requests from a place: its reputation there and where it stands, the recent
    counts the traffic split shows beside it (decayed to now), the record of the place's own evidence, and its share in
    percent configured, effective (by reputation only where the project avoids) and as Avoid would make it."""

    reputation: ConnectorReputation
    state: PlaceState
    blocked: int | None
    requests: int | None
    own: ConnectorSiteReputation | None
    own_factor: float | None
    effective_weight: float
    effective_share: float
    configured_share: float
    avoid_share: float

    @property
    def takes(self) -> bool:
        """Whether routing lets the connector take the requests."""
        return self.state is not PlaceState.TAKES_NONE

    @property
    def lowered(self) -> bool:
        """Whether its own value there takes its weight under the configured one; an inherited one never counts."""
        return self.state is PlaceState.OWN and self.reputation.lowers


class ReputationSplit:
    """How a project's requests to its sites from any place would split between its connectors, under exit reputation:
    the project's records for those sites, and per site and place the rows routing finds eligible for such a request
    (domain filters, reservations and the place applied as a gateway applies them), before any avoid.

    The effective weights count only where the project avoids; elsewhere they are the configured ones, and each
    connector's reputation is shown for what it would do. ``groups`` hold the rows of a request naming no site and no
    place, which the configured split is made of.
    """

    def __init__(
        self,
        project: Project,
        preview: Router,
        *,
        avoided: Mapping[str, frozenset[str]],
        records: Iterable[ConnectorSiteReputation],
        now: datetime,
    ) -> None:
        self._project = project
        self._preview = preview
        self._avoided = avoided
        self._now = now
        self._policy = ReputationPolicy.of(project)
        # (site, place) -> the rows eligible for a request to the site from the place, by connector; on the first ask.
        self._by_request: dict[tuple[str, str], dict[str, ProxyGroup]] = {}
        # (connector, site) -> level of a requested place -> its record.
        self._records: dict[tuple[str, str], dict[str, ConnectorSiteReputation]] = {}
        # Site -> connector -> level of a requested place -> its stored factor, as the routing feed carries them.
        self._factors: dict[str, dict[str, dict[str, float]]] = {}
        # (connector, site) -> the connector's rows exiting from an IP the site avoids; on the first ask.
        self._off_rows: dict[tuple[str, str], set[str]] = {}
        for record in records:
            self._records.setdefault((record.connector_id, record.site), {})[record.place] = record
            if record.factor is not None:
                self._factors.setdefault(record.site, {}).setdefault(record.connector_id, {})[record.place] = (
                    record.factor
                )

    @classmethod
    async def read(
        cls,
        session: AsyncSession,
        project: Project,
        preview: Router,
        *,
        sites: Collection[str],
        levels: Collection[str] | None,
        now: datetime,
        connector_ids: Collection[str] | None = None,
    ) -> ReputationSplit:
        """The split for ``sites`` from the project's records: the exit IPs each site avoids, and the connectors' records
        at ``levels`` of the requested places (None: every place), of ``connector_ids`` alone when given. Two
        statements."""
        repository = ExitReputationRepository(session)
        avoided = await repository.avoided_ips(project.id, sites, now)
        records = await repository.connector_records(project.id, sites, levels, connector_ids=connector_ids)
        return cls(project, preview, avoided=avoided, records=records, now=now)

    @classmethod
    async def busiest_listed(
        cls,
        session: AsyncSession,
        project: Project,
        preview: Router,
        *,
        sites: Sequence[str],
        now: datetime,
        connector_ids: Sequence[str],
    ) -> str | None:
        """The first of ``sites`` (the most reported first) with a place to list for ``connector_ids``: a dynamic
        connector's record, or a country :meth:`avoided_places` finds; None when none has one. The sites ahead of the
        first with a dynamic connector's record cost one read of their avoided IPs and a pass per avoided country."""
        repository = ExitReputationRepository(session)
        dynamic = [connector_id for connector_id in connector_ids if _dynamic(preview, connector_id)]
        recorded = await repository.recorded_sites(project.id, dynamic)
        ahead = list(takewhile(lambda site: site not in recorded, sites))
        first = sites[len(ahead)] if len(ahead) < len(sites) else None
        if not ahead or len(dynamic) == len(connector_ids):
            return first
        split = cls(project, preview, avoided=await repository.avoided_ips(project.id, ahead, now), records=(), now=now)
        found = await asyncio.to_thread(
            lambda: next((site for site in ahead if split.avoided_places(site, connector_ids)), None)
        )
        return found or first

    @property
    def applied(self) -> bool:
        """Whether routing weighs the connectors by it: the project's mode avoids."""
        return self._project.exit_reputation.avoids

    @cached_property
    def groups(self) -> dict[str, ProxyGroup]:
        """The rows eligible for a request naming no site and no place, by connector."""
        rows = self._preview.get_routable_proxies_for_project(self._project.id)
        return {group.key: group for group in self._preview.group_by_connector(rows)}

    def dynamic(self, connector_id: str) -> bool:
        """Whether the connector serves through a dynamic gateway row, as gateways tell."""
        return _dynamic(self._preview, connector_id)

    def excluded_reason(self, connector: Connector) -> str | None:
        """Why the connector takes no request naming no site and no place: "disabled", "traffic_limit" (blocked by its
        traffic limit), "reserved_only" (it serves only requests naming a country it reserves) or
        "no_eligible_proxies"; None while it takes them."""
        if not connector.enabled:
            return "disabled"
        if self._preview.is_traffic_blocked(connector.id):
            return "traffic_limit"
        if connector.id in self.groups:
            return None
        return "reserved_only" if connector.id in self._serving_reserved else "no_eligible_proxies"

    @cached_property
    def _serving_reserved(self) -> set[str]:
        """The connectors with a proxy taking requests for a country they reserve, which untargeted ones keep off.

        A reserved country's requests reach only the connectors reserving it, so each of its routable proxies is theirs.
        """
        preview, project_id = self._preview, self._project.id
        reserved = sorted({country for view in preview.snapshot.connector_views(project_id) for country in view.reserved})
        return {
            proxy.connector_id
            for country in reserved
            for proxy in preview.get_routable_proxies_for_project(project_id, location=LocationTarget(country=country))
        }

    def groups_for(self, site: str, place: str) -> dict[str, ProxyGroup]:
        """The rows eligible for a request to ``site`` from ``place``, by connector, as the preview router finds them:
        before any avoid, since a preview carries no exit reputation."""
        groups = self._by_request.get((site, place))
        if groups is None:
            rows = self._preview.get_routable_proxies_for_project(
                self._project.id, target_host=site, location=place_target(place)
            )
            groups = self._by_request[site, place] = {
                group.key: group for group in self._preview.group_by_connector(rows)
            }
        return groups

    def shares(self, site: str | None, place: str, connector_ids: Iterable[str]) -> dict[str, ConnectorShare]:
        """Each connector's part of the requests to ``site`` from ``place``, by id, none for a connector routing keeps
        off them; for None (a request naming no site), the configured weights alone."""
        groups = self.groups_for(site, place) if site is not None else self.groups
        reputations = {
            connector_id: self._reputation(connector_id, site, place) if site is not None else _NONE
            for connector_id in connector_ids
        }
        configured = {
            connector_id: float(groups[connector_id].weight) if connector_id in groups else 0.0
            for connector_id in reputations
        }
        avoid = {connector_id: reputation.weight(configured[connector_id]) for connector_id, reputation in reputations.items()}
        effective = avoid if site is not None and self.applied else configured
        totals = sum(configured.values()), sum(avoid.values()), sum(effective.values())
        shares: dict[str, ConnectorShare] = {}
        for connector_id, reputation in reputations.items():
            records = self._records.get((connector_id, site), _NO_RECORDS) if site is not None else _NO_RECORDS
            own = reputation.own(place, records)
            blocked, requests = self._counts(reputation.shown(place, records))
            shares[connector_id] = ConnectorShare(
                reputation=reputation,
                state=reputation.state(place, records) if connector_id in groups else PlaceState.TAKES_NONE,
                blocked=blocked,
                requests=requests,
                own=own,
                own_factor=reputation.own_factor(place, records),
                effective_weight=effective[connector_id],
                effective_share=_percent(effective[connector_id], totals[2]),
                configured_share=_percent(configured[connector_id], totals[0]),
                avoid_share=_percent(avoid[connector_id], totals[1]),
            )
        return shares

    def reported_places(self, site: str) -> dict[str, int]:
        """Each place a dynamic connector holds a record at for ``site``, with the reports made there through them."""
        reports: dict[str, int] = {}
        for (connector_id, record_site), by_level in self._records.items():
            if record_site != site or not self.dynamic(connector_id):
                continue
            for place, record in by_level.items():
                reports[place] = reports.get(place, 0) + record.blocked_reports + record.good_reports
        return reports

    def avoided_countries(self, site: str) -> set[str]:
        """The countries of the rows of the project's connectors with IPs whose exit IP ``site`` avoids."""
        avoided = self._avoided.get(site, frozenset())
        if not avoided:
            return set()
        countries: set[str] = set()
        for view in self._preview.snapshot.connector_views(self._project.id):
            if view.dynamic:
                continue
            off = self._off(view, site)
            countries.update(proxy.country for proxy in view.proxies if proxy.id in off and proxy.country)
        return countries

    def avoided_places(
        self, site: str, connector_ids: Sequence[str], *, skip: Collection[str] = ()
    ) -> dict[str, dict[str, ConnectorShare]]:
        """Each country but ``skip``'s where a connector with IPs takes the requests to ``site`` and the site avoids
        some of its eligible rows, with every connector's share there."""
        listed: dict[str, dict[str, ConnectorShare]] = {}
        for country in sorted(self.avoided_countries(site) - set(skip)):
            at = self.shares(site, country, connector_ids)
            if any(share.takes and share.reputation.usable_ips != share.reputation.eligible_ips for share in at.values()):
                listed[country] = at
        return listed

    def _reputation(self, connector_id: str, site: str, place: str) -> ConnectorReputation:
        """A connector's reputation for ``site``, as gateways would weigh it for a request from ``place``."""
        view = self._preview.snapshot.connector_view(connector_id)
        group = self.groups_for(site, place).get(connector_id)
        eligible = group.proxies if group is not None else []
        if view is None:
            return ConnectorReputation.by_usable_ips(len(eligible), len(eligible))
        # A dynamic connector is weighed by its factors alone: its rows' exits do not count.
        off = self._off(view, site) if not view.dynamic else set()
        usable = sum(1 for proxy in eligible if proxy.id not in off) if off else len(eligible)
        request = RequestReputation(site, place, factors=self._factors.get(site, _NO_FACTORS))
        return request.of(view, usable, len(eligible) - usable)

    def _off(self, view: ConnectorView, site: str) -> set[str]:
        """The rows of ``view`` exiting from an IP ``site`` avoids, by the view's exits; worked out once per connector
        and site, however many places ask."""
        key = (view.connector.id, site)
        off = self._off_rows.get(key)
        if off is None:
            avoided = self._avoided.get(site, frozenset())
            exits = view.exits
            off = self._off_rows[key] = {row for ip in exits.keys() & avoided for row in exits[ip]} if avoided else set()
        return off

    def _counts(self, record: ConnectorSiteReputation | None) -> tuple[int | None, int | None]:
        """A record's blocked reports and requests as the last flush weighed them, decayed by the hours since alone;
        none without a record."""
        if record is None:
            return None, None
        decay = record.decay_factor(self._now, self._policy)
        return round(record.weighted_blocked * decay), round(record.weighted_requests * decay)


def _dynamic(preview: Router, connector_id: str) -> bool:
    """Whether the connector serves through a dynamic gateway row, as ``preview``'s snapshot tells."""
    view = preview.snapshot.connector_view(connector_id)
    return view is not None and view.dynamic


def _percent(weight: float, total: float) -> float:
    """``weight``'s share of ``total`` in percent; none of nothing."""
    return weight / total * 100 if total else 0.0


# A connector's reputation for a request naming no site: none.
_NONE = ConnectorReputation()
_NO_RECORDS: Mapping[str, ConnectorSiteReputation] = MappingProxyType({})
_NO_FACTORS: Mapping[str, Mapping[str, float]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class PlaceRow:
    """One place a site has records for: the reports dynamic connectors took there and each connector's share of the
    site's requests from it."""

    place: str
    reports: int
    shares: Mapping[str, ConnectorShare]

    @property
    def level(self) -> PlaceLevel:
        return PlaceLevel.of(self.place)

    @property
    def parent(self) -> str | None:
        """The next coarser level ("US" for "US/NY" and "US//austin", "US/NY" for "US/NY/new_york"); None for a country
        and for no place."""
        levels = place_levels(self.place)
        return levels[1] if len(levels) > 1 else None

    @property
    def lowered(self) -> bool:
        """Whether some connector's own value here is under 1."""
        return any(share.lowered for share in self.shares.values())

    @property
    def not_judged(self) -> bool:
        """Whether some connector taking the requests has reports here too few to judge."""
        return any(share.state is PlaceState.NOT_JUDGED for share in self.shares.values())

    @property
    def lowest(self) -> tuple[float, bool] | None:
        """The lowest value in force among the connectors taking the requests, and whether it is inherited; an own one
        first on a tie. None when no connector takes them."""
        return min(
            (
                (share.reputation.value, share.state is PlaceState.INHERITED)
                for share in self.shares.values()
                if share.takes
            ),
            default=None,
        )


@dataclass(slots=True)
class ConnectorTile:
    """One connector across a site's places: its own values per level, lowered (under 1) of those measured, and its
    listed places by where it stands; takes-none places are not counted."""

    lowered_countries: int = 0
    countries: int = 0
    lowered_states: int = 0
    states: int = 0
    lowered_cities: int = 0
    cities: int = 0
    heavy: int = 0
    lowered: int = 0
    inherited: int = 0
    full: int = 0
    not_judged: int = 0

    def count(self, level: PlaceLevel, share: ConnectorShare) -> None:
        """Count the connector's ``share`` at a listed place of ``level``."""
        if share.state is PlaceState.OWN:
            self._own(level, share.lowered)
            value = share.reputation.value
            if value < HEAVILY_LOWERED:
                self.heavy += 1
            elif share.lowered:
                self.lowered += 1
            else:
                self.full += 1
        elif share.state is PlaceState.INHERITED:
            self.inherited += 1
        elif share.state is PlaceState.NOT_JUDGED:
            self.not_judged += 1
        elif share.state is PlaceState.NO_REPORTS:
            self.full += 1

    def _own(self, level: PlaceLevel, lowered: bool) -> None:
        """Count an own value at ``level``."""
        if level is PlaceLevel.COUNTRY:
            self.countries += 1
            self.lowered_countries += lowered
        elif level is PlaceLevel.STATE:
            self.states += 1
            self.lowered_states += lowered
        elif level is PlaceLevel.CITY:
            self.cities += 1
            self.lowered_cities += lowered


class SitePlaces:
    """One site across every place its records name, for each connector: a row per place a dynamic connector has a
    record at (no place only when one of them has a record there), and per country where a connector with IPs has an
    eligible row the site avoids, in tree order; with each connector's tile and the hardest hit.

    Pure computation over a read split: one eligibility pass per place, so it is built beside the event loop, sharing
    the GIL with it.
    """

    def __init__(self, split: ReputationSplit, site: str, connector_ids: Sequence[str]) -> None:
        self.split = split
        self.site = site
        self._ids = list(connector_ids)
        # Each connector for a request naming no place: a connector with IPs' value on its tile.
        self.no_place = split.shares(site, "", self._ids)
        self.rows = self._rows()

    def _rows(self) -> list[PlaceRow]:
        """Every listed place in tree order: no place first, then the countries, each followed by its states and cities,
        the most reported first at each level and by key on a tie."""
        reports = self.split.reported_places(self.site)
        keys = {level for place in reports for level in place_levels(place)}
        shares = {key: self.no_place if key == "" else self.split.shares(self.site, key, self._ids) for key in keys}
        shares.update(self.split.avoided_places(self.site, self._ids, skip=keys))
        rows = {key: PlaceRow(key, reports.get(key, 0), at) for key, at in shares.items()}
        children: dict[str | None, list[PlaceRow]] = {}
        for row in rows.values():
            children.setdefault(row.parent, []).append(row)
        for listed in children.values():
            listed.sort(key=lambda row: (row.place != "", -row.reports, row.place))
        ordered: list[PlaceRow] = []
        stack = list(reversed(children.get(None, [])))
        while stack:
            row = stack.pop()
            ordered.append(row)
            stack.extend(reversed(children.get(row.place, [])))
        return ordered

    @cached_property
    def cities(self) -> dict[str, tuple[int, int]]:
        """Each listed country with listed cities: those with some connector's own value under 1, and all of them."""
        counts: dict[str, tuple[int, int]] = {}
        for row in self.rows:
            if row.level is PlaceLevel.CITY:
                country = row.place.partition("/")[0]
                lowered, total = counts.get(country, (0, 0))
                counts[country] = (lowered + row.lowered, total + 1)
        return counts

    def tile(self, connector_id: str) -> ConnectorTile:
        """The connector across the listed places."""
        tile = ConnectorTile()
        for row in self.rows:
            tile.count(row.level, row.shares[connector_id])
        return tile

    @property
    def serving(self) -> list[str]:
        """The connectors taking the requests from some listed place."""
        return [cid for cid in self._ids if any(row.shares[cid].takes for row in self.rows)]

    @property
    def lowered(self) -> list[str]:
        """The connectors whose own value is under 1 at some listed place."""
        return [cid for cid in self._ids if any(row.shares[cid].lowered for row in self.rows)]

    @property
    def hardest(self) -> tuple[str, str, float] | None:
        """The lowest own value under 1 at a listed place, as (connector, place, value); on a tie the finer place first
        (a city before its state), then the more reported. A connector with IPs is skipped at a state or a city where it
        has at most ``THIN_ROWS`` eligible rows: one avoided row there reads ×0.5 or ×0."""
        found = [
            (share.reputation.value, -row.level.depth, -row.reports, index, connector_id, row.place)
            for index, row in enumerate(self.rows)
            for connector_id, share in row.shares.items()
            if share.lowered and not self._thin(connector_id, row.level, share)
        ]
        if not found:
            return None
        value, _depth, _reports, _index, connector_id, place = min(found)
        return connector_id, place, value

    def _thin(self, connector_id: str, level: PlaceLevel, share: ConnectorShare) -> bool:
        """Whether a connector with IPs has too few eligible rows at a state or a city for its value to tell."""
        if self.split.dynamic(connector_id) or level not in (PlaceLevel.STATE, PlaceLevel.CITY):
            return False
        return (share.reputation.eligible_ips or 0) <= THIN_ROWS
