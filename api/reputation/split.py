# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the api shows it beside the traffic split: how a project's requests to a site from a place would
split between its connectors by their effective weights there, with each one's reputation and the counts behind it.

The records are read from Postgres, the rows come from the preview router as eligible for a request to the site from the
place, and each reputation is made by the rule gateways route by (:class:`~api.reputation.weight.ConnectorReputation`);
see docs/api.md (Traffic split).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from api.db.reputation_repository import ExitReputationRepository
from api.models.cloud_options import COUNTRIES
from api.models.location import LocationTarget
from api.reputation.record import ConnectorSiteReputation, ReputationPolicy, place_levels
from api.reputation.weight import ConnectorReputation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.project import Project
    from api.routing.router import Router
    from api.strategies import ProxyGroup

# The most sites and places a list for a picker offers, the most reported first.
MAX_LISTED = 20
# Country code -> its name, for a place's label.
_COUNTRY_NAMES = {option.code: option.name for option in COUNTRIES if option.code}


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
    """How a requested place reads: a country by its name ("United States"), a state or a city by its parts ("US, NY",
    "US, NY, new_york", "US, austin")."""
    country, _, below = place.partition("/")
    if not below:
        return _COUNTRY_NAMES.get(country, country)
    state, _, city = below.partition("/")
    return ", ".join(part for part in (country, state, city) if part)


@dataclass(frozen=True, slots=True)
class ConnectorShare:
    """One connector's part of a site's requests: its reputation there, the recent counts behind it (a dynamic
    connector's blocked reports and requests at the level used, or at the finest level with a record while none has
    enough requests; a connector with IPs' record for no place), and its effective weight and share in percent."""

    reputation: ConnectorReputation
    blocked: int | None
    requests: int | None
    effective_weight: float
    effective_share: float


class ReputationSplit:
    """How a project's requests to its sites from one place would split between its connectors, under exit reputation:
    the project's records for those sites, and per site the rows routing finds eligible for such a request (domain
    filters, reservations and the place applied as a gateway applies them), before any avoid.

    The effective weights count only where the project avoids; elsewhere they are the configured ones, and each
    connector's reputation is shown for what it would do. ``groups`` hold the rows of a request naming no site and no
    place, which the configured split is made of.
    """

    def __init__(
        self,
        project: Project,
        preview: Router,
        *,
        place: str,
        avoided: Mapping[str, frozenset[str]],
        records: Iterable[ConnectorSiteReputation],
        now: datetime,
    ) -> None:
        self._project = project
        self._preview = preview
        self._place = place
        self._target = place_target(place)
        self._avoided = avoided
        self._now = now
        self._policy = ReputationPolicy.of(project)
        self.groups: dict[str, ProxyGroup] = {
            group.key: group
            for group in preview.group_by_connector(preview.get_routable_proxies_for_project(project.id))
        }
        # Site -> its groups for a request from the place, worked out on the first ask.
        self._by_site: dict[str, dict[str, ProxyGroup]] = {}
        # (connector, site) -> level of a requested place -> its record.
        self._records: dict[tuple[str, str], dict[str, ConnectorSiteReputation]] = {}
        for record in records:
            self._records.setdefault((record.connector_id, record.site), {})[record.place] = record

    @classmethod
    async def read(
        cls,
        session: AsyncSession,
        project: Project,
        preview: Router,
        *,
        sites: Collection[str],
        place: str,
        now: datetime,
    ) -> ReputationSplit:
        """The split for ``sites`` and a request naming ``place``, from the project's records: the exit IPs each site
        avoids, and the connectors' records at the levels of the place and at none. Two statements."""
        repository = ExitReputationRepository(session)
        avoided = await repository.avoided_ips(project.id, sites, now)
        records = await repository.connector_records(project.id, sites, {*place_levels(place), ""})
        return cls(project, preview, place=place, avoided=avoided, records=records, now=now)

    @property
    def applied(self) -> bool:
        """Whether routing weighs the connectors by it: the project's mode avoids."""
        return self._project.exit_reputation.avoids

    def dynamic(self, connector_id: str) -> bool:
        """Whether the connector serves through a dynamic gateway row, as gateways tell."""
        view = self._preview.snapshot.connector_view(connector_id)
        return view is not None and view.dynamic

    def groups_for(self, site: str) -> dict[str, ProxyGroup]:
        """The rows eligible for a request to ``site`` from the place, by connector, as the preview router finds them:
        before any avoid, since a preview carries no exit reputation."""
        groups = self._by_site.get(site)
        if groups is None:
            rows = self._preview.get_routable_proxies_for_project(
                self._project.id, target_host=site, location=self._target
            )
            groups = self._by_site[site] = {group.key: group for group in self._preview.group_by_connector(rows)}
        return groups

    def shares(self, site: str | None, connector_ids: Iterable[str]) -> dict[str, ConnectorShare]:
        """Each connector's part of the requests to ``site`` from the place, by id, none for a connector routing keeps
        off them; for None (a request naming no site), the configured weights alone."""
        groups = self.groups_for(site) if site is not None else self.groups
        reputations: dict[str, tuple[ConnectorReputation, ConnectorSiteReputation | None]] = {}
        weights: dict[str, float] = {}
        for connector_id in connector_ids:
            reputation, shown = self._reputation(connector_id, site) if site is not None else (_NONE, None)
            group = groups.get(connector_id)
            if group is None:
                weights[connector_id] = 0.0
            elif site is not None and self.applied:
                weights[connector_id] = reputation.weight(group.weight)
            else:
                weights[connector_id] = float(group.weight)
            reputations[connector_id] = (reputation, shown)
        total = sum(weights.values())
        shares: dict[str, ConnectorShare] = {}
        for connector_id, (reputation, shown) in reputations.items():
            blocked, requests = self._counts(shown)
            weight = weights[connector_id]
            shares[connector_id] = ConnectorShare(
                reputation=reputation,
                blocked=blocked,
                requests=requests,
                effective_weight=weight,
                effective_share=weight / total * 100 if total else 0.0,
            )
        return shares

    def _reputation(
        self, connector_id: str, site: str
    ) -> tuple[ConnectorReputation, ConnectorSiteReputation | None]:
        """A connector's reputation for ``site``, as gateways would weigh it for this place, and the record whose counts
        show it."""
        by_level = self._records.get((connector_id, site), {})
        if self.dynamic(connector_id):
            factors = {level: record.factor for level, record in by_level.items() if record.factor is not None}
            reputation = ConnectorReputation.by_block_rate(factors, self._place)
            levels = (reputation.place,) if reputation.place is not None else place_levels(self._place)
            return reputation, next((by_level[level] for level in levels if level in by_level), None)
        group = self.groups_for(site).get(connector_id)
        eligible = group.proxies if group is not None else []
        avoided = self._avoided.get(site, frozenset())
        # The rows exiting from an avoided IP, by the view's exits: each row's IP is worked out once per view.
        view = self._preview.snapshot.connector_view(connector_id)
        exits = view.exits if avoided and view is not None else {}
        off = {row for ip in exits.keys() & avoided for row in exits[ip]}
        usable = sum(1 for proxy in eligible if proxy.id not in off) if off else len(eligible)
        return ConnectorReputation.by_usable_ips(usable, len(eligible)), by_level.get("")

    def _counts(self, record: ConnectorSiteReputation | None) -> tuple[int | None, int | None]:
        """A record's blocked reports and requests as the last flush weighed them, decayed by the hours since alone;
        none without a record."""
        if record is None:
            return None, None
        decay = record.decay_factor(self._now, self._policy)
        return round(record.weighted_blocked * decay), round(record.weighted_requests * decay)


# A connector's reputation for a request naming no site: none.
_NONE = ConnectorReputation()
