# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The effective-weight rule: what exit reputation multiplies a connector's routing weight by for one request.

Gateways route by it and the api shows it, through this one class, so the two never disagree; see
docs/routing-strategies.md (Exit reputation).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from api.reputation.record import place_levels


class ReputationKind(StrEnum):
    """What measures a connector's reputation for a site."""

    # A dynamic connector: one less its reported blocks per request there.
    BLOCK_RATE = "block_rate"
    # A connector with IPs: the share of its eligible exit IPs not avoided there.
    USABLE_IPS = "usable_ips"


@dataclass(frozen=True, slots=True)
class ConnectorReputation:
    """One connector's reputation for a request's site: the value its configured weight is multiplied by (1 when
    nothing says otherwise), what measures it, the level of the requested place a dynamic connector's factor came
    from, and a connector with IPs' usable and eligible rows."""

    value: float = 1.0
    kind: ReputationKind | None = None
    place: str | None = None
    usable_ips: int | None = None
    eligible_ips: int | None = None

    @classmethod
    def by_block_rate(cls, factors: Mapping[str, float], place: str) -> ConnectorReputation:
        """A dynamic connector's, from its factors for the site by level of the requested place: the finest level of
        ``place`` that has one (city, then state, then country; "" for a request naming none, its own and only level),
        else 1. There is no figure for all countries to fall back on."""
        for level in place_levels(place):
            factor = factors.get(level)
            if factor is not None:
                return cls(factor, ReputationKind.BLOCK_RATE, level)
        return _NO_FACTOR

    @classmethod
    def by_usable_ips(cls, usable: int, eligible: int) -> ConnectorReputation:
        """A connector with IPs': of its rows eligible for the request, the share whose exit IP is not avoided for the
        site; 1 with none eligible."""
        return cls(usable / eligible if eligible else 1.0, ReputationKind.USABLE_IPS, None, usable, eligible)

    def weight(self, configured: float) -> float:
        """The effective weight of a connector configured at ``configured``."""
        return configured * self.value

    @property
    def lowers(self) -> bool:
        """Whether it takes the weight under the configured one."""
        return self.value < 1.0


# A dynamic connector no level of the requested place has a factor for.
_NO_FACTOR = ConnectorReputation(kind=ReputationKind.BLOCK_RATE)
