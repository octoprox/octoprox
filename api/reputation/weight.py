# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The effective-weight rule: what exit reputation multiplies a connector's routing weight by for one request.

Gateways route by it and the api shows it, through these classes, so the two never disagree; see
docs/routing-strategies.md (Exit reputation).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING

from api.reputation.record import ConnectorSiteReputation, place_levels

if TYPE_CHECKING:
    from api.models.project import Project


class ReputationKind(StrEnum):
    """What measures a connector's reputation for a site."""

    # A dynamic connector: one less its reported blocks per request there.
    BLOCK_RATE = "block_rate"
    # A connector with IPs: the share of its eligible exit IPs not avoided there.
    USABLE_IPS = "usable_ips"


class PlaceState(StrEnum):
    """Where a connector's reputation for a site stands at one requested place."""

    # Measured at the place itself: a dynamic connector's factor there, a connector with IPs' rows there.
    OWN = "own"
    # A dynamic connector's factor from a coarser level of the place, the place itself holding none.
    INHERITED = "inherited"
    # A dynamic connector reported at some level of the place, none of which holds enough requests for a factor yet.
    NOT_JUDGED = "not_judged"
    # Nothing reported: the configured weight.
    NO_REPORTS = "no_reports"
    # Routing keeps the connector off the requests: no row of it is eligible.
    TAKES_NONE = "takes_none"


@dataclass(frozen=True, slots=True)
class ReputationRule:
    """How a project weighs its judged dynamic connectors at a place against the best of them: each one's success (its
    own factor) times its ratio to the best's to the power ``strength - 1``, never under ``floor``.

    At strength 1 that is the plain trim, ``max(floor, success)``; connectors with IPs and dynamic ones not judged
    are left as they are.
    """

    strength: int = 1
    floor: float = 0.1

    @classmethod
    def of(cls, project: Project) -> ReputationRule:
        """The rule of ``project``'s Strength and Lowest factor (a percentage)."""
        return _rule(project.exit_reputation_strength, project.exit_reputation_floor)

    def value(self, success: float, best: float) -> float:
        """What a judged dynamic connector's weight is multiplied by, its success beside the ``best`` one's."""
        return max(self.floor, success * (success / best) ** (self.strength - 1))

    def apply(self, reputations: Mapping[str, ConnectorReputation]) -> dict[str, ConnectorReputation]:
        """``reputations`` of the connectors taking one request, by id, each compared with the best judged dynamic one
        among them: the highest success, the lowest id on a tie."""
        best_id: str | None = None
        best = 0.0
        for connector_id, reputation in reputations.items():
            success = reputation.success
            if success is not None and (
                best_id is None or success > best or (success == best and connector_id < best_id)
            ):
                best_id, best = connector_id, success
        if best_id is None:
            return dict(reputations)
        return {
            connector_id: reputation.compared(best, best_id, self) for connector_id, reputation in reputations.items()
        }


@cache
def _rule(strength: int, floor_percent: int) -> ReputationRule:
    """One rule per setting, shared by every request routed under it."""
    return ReputationRule(strength, floor_percent / 100)


@dataclass(frozen=True, slots=True)
class ConnectorReputation:
    """One connector's reputation for a request's site: the value its configured weight is multiplied by (1 when
    nothing says otherwise), what measures it, the level of the requested place a dynamic connector's factor came
    from, and a connector with IPs' usable and eligible rows.

    A judged dynamic connector also keeps its success, the own factor its value came from, and once compared
    (:class:`ReputationRule`) the best one's success at the place and that connector.
    """

    value: float = 1.0
    kind: ReputationKind | None = None
    place: str | None = None
    usable_ips: int | None = None
    eligible_ips: int | None = None
    success: float | None = None
    best: float | None = None
    best_connector_id: str | None = None

    @classmethod
    def by_block_rate(cls, factors: Mapping[str, float], place: str) -> ConnectorReputation:
        """A dynamic connector's, from its factors for the site by level of the requested place: the finest level of
        ``place`` that has one (city, then state, then country; "" for a request naming none, its own and only level),
        else 1. There is no figure for all countries to fall back on. Its value is that factor until the project's
        rule compares it (:meth:`ReputationRule.apply`)."""
        for level in place_levels(place):
            factor = factors.get(level)
            if factor is not None:
                return BlockRateReputation(factor, ReputationKind.BLOCK_RATE, level, success=factor)
        return _NO_FACTOR

    @classmethod
    def by_usable_ips(cls, usable: int, eligible: int) -> ConnectorReputation:
        """A connector with IPs': of its rows eligible for the request, the share whose exit IP is not avoided for the
        site; 1 with none eligible."""
        return UsableIpsReputation(
            usable / eligible if eligible else 1.0, ReputationKind.USABLE_IPS, None, usable, eligible
        )

    def weight(self, configured: float) -> float:
        """The effective weight of a connector configured at ``configured``."""
        return configured * self.value

    @property
    def lowers(self) -> bool:
        """Whether it takes the weight under the configured one."""
        return self.value < 1.0

    def compared(self, best: float, best_connector_id: str, rule: ReputationRule) -> ConnectorReputation:
        """It beside the best judged dynamic connector at the place, by ``rule``: as it is, but for a judged dynamic
        connector."""
        return self

    # What the api shows beside it, from the connector's records for the site by level of the requested place.

    def state(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> PlaceState:
        """Where it stands at ``place`` for a connector that takes the request; nothing measured, without a site."""
        return PlaceState.NO_REPORTS

    def own(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        """The record ``place``'s own evidence is counted in for the connector; none without a site."""
        return None

    def own_factor(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> float | None:
        """The factor stored at ``place`` itself, which only a dynamic connector's routing reads."""
        return None

    def shown(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        """The record whose counts the traffic split shows beside the value; none without a site."""
        return None


@dataclass(frozen=True, slots=True)
class BlockRateReputation(ConnectorReputation):
    """A dynamic connector's: its factor at the finest level of the requested place that has one."""

    def compared(self, best: float, best_connector_id: str, rule: ReputationRule) -> ConnectorReputation:
        """The rule's value of its success beside ``best``; one not judged keeps its weight."""
        if self.success is None:
            return self
        return BlockRateReputation(
            rule.value(self.success, best),
            self.kind,
            self.place,
            success=self.success,
            best=best,
            best_connector_id=best_connector_id,
        )

    def state(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> PlaceState:
        """Own or inherited by the level the factor came from; without one, not judged while some level of ``place``
        holds a record."""
        if self.place is not None:
            return PlaceState.OWN if self.place == place else PlaceState.INHERITED
        if any(level in records for level in place_levels(place)):
            return PlaceState.NOT_JUDGED
        return PlaceState.NO_REPORTS

    def own(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        """The record kept at ``place`` itself."""
        return records.get(place)

    def own_factor(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> float | None:
        record = records.get(place)
        return record.factor if record is not None else None

    def shown(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        """The record of the level the factor came from; without one, the finest level of ``place`` with a record."""
        levels = (self.place,) if self.place is not None else place_levels(place)
        return next((records[level] for level in levels if level in records), None)


@dataclass(frozen=True, slots=True)
class UsableIpsReputation(ConnectorReputation):
    """A connector with IPs': the share of its rows eligible for the request whose exit IP is not avoided for the site."""

    def state(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> PlaceState:
        """Own once the site has a report through it or one of its eligible rows is avoided there."""
        if "" in records or self.usable_ips != self.eligible_ips:
            return PlaceState.OWN
        return PlaceState.NO_REPORTS

    def own(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        """The record for no place, which holds every report through it whatever place the request named."""
        return records.get("")

    def shown(self, place: str, records: Mapping[str, ConnectorSiteReputation]) -> ConnectorSiteReputation | None:
        return records.get("")


# A dynamic connector no level of the requested place has a factor for.
_NO_FACTOR = BlockRateReputation(kind=ReputationKind.BLOCK_RATE)
