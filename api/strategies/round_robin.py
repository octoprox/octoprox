# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Round-robin routing strategy."""

from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.strategies.base import ProxyGroup, RoutingStrategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient

_FLAT = ""


class RoundRobinStrategy(RoutingStrategy):
    """Cycles through proxies sequentially.

    Across connectors it runs smooth weighted round robin (the nginx
    algorithm): weights 3 and 1 produce the fixed order A A B A, three A
    for every B with the B spread out rather than clumped. Each connector
    then keeps its own position, so its proxies are cycled in order
    regardless of how often the other connectors are picked in between.
    """

    def __init__(self) -> None:
        self._indices: dict[str, int] = {}
        self._current: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "round_robin"

    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        return self._next_in(_FLAT, proxies)

    async def select_weighted(
        self,
        groups: list[ProxyGroup],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        groups = [g for g in groups if g.proxies]
        if not groups:
            return None
        group = groups[0] if len(groups) == 1 else self.pick_group(groups, session_id)
        return self._next_in(group.key, group.proxies)

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        """Smooth weighted round robin over the connectors present in this call.

        A connector absent from a call (quarantined, filtered out) keeps its
        accumulated credit and resumes its cadence when it returns; state
        for a removed connector goes through ``forget_group``.
        """
        total = 0
        best: ProxyGroup | None = None
        for g in groups:
            weight = max(g.weight, 1)
            total += weight
            self._current[g.key] = self._current.get(g.key, 0) + weight
            if best is None or self._current[g.key] > self._current[best.key]:
                best = g
        assert best is not None
        self._current[best.key] -= total
        return best

    def _next_in(self, key: str, proxies: list[Proxy]) -> Proxy | None:
        if not proxies:
            return None
        index = self._indices.get(key, 0) % len(proxies)
        self._indices[key] = (index + 1) % len(proxies)
        return proxies[index]

    def forget_group(self, key: str) -> None:
        self._indices.pop(key, None)
        self._current.pop(key, None)

    def reset(self) -> None:
        self._indices.clear()
        self._current.clear()
