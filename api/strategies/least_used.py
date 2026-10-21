# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Least-used routing strategy: the proxy with the fewest transfers in flight on this gateway."""

from __future__ import annotations

import random
from fractions import Fraction
from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.routing.inflight import InFlight
from api.strategies.base import ProxyGroup, RoutingStrategy, weighted_random_group

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class LeastUsedStrategy(RoutingStrategy):
    """Picks the proxy with the fewest transfers open on this gateway.

    Across connectors it picks the one with the fewest per unit of weight; ties are broken at random, by weight.

    Args:
        inflight: this gateway's open transfers; None counts none, for a strategy no gateway serves through.
    """

    def __init__(self, inflight: InFlight | None = None) -> None:
        self._inflight = inflight if inflight is not None else InFlight()

    @property
    def name(self) -> str:
        return "least_used"

    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: RedisClient | None = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        if not proxies:
            return None
        counts = [self._inflight.of_proxy(proxy.id) for proxy in proxies]
        fewest = min(counts)
        return random.choice([proxy for proxy, count in zip(proxies, counts, strict=True) if count == fewest])

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        loads = [Fraction(self._inflight.of_connector(group.key), max(group.weight, 1)) for group in groups]
        lightest = min(loads)
        tied = [group for group, load in zip(groups, loads, strict=True) if load == lightest]
        return tied[0] if len(tied) == 1 else weighted_random_group(tied)
