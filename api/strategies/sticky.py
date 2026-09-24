# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sticky session routing strategy.

A given ``session_id`` always resolves to the same upstream proxy.
The binding lives in two places:

* ``_session_map`` is the in-process cache (hot path, no Redis I/O).
* ``sticky:<project_id>:<session_id>`` in Redis (cross-instance source
  of truth, written by ``select`` after assigning a new proxy).

When a request lands on a peer instance for the first time, the peer's
``select`` reads the Redis binding and warms its own local cache - so
session affinity survives across instances.
"""

import hashlib
import math
import random
from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.strategies.base import ProxyGroup, RoutingStrategy, weighted_random_group

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class StickySessionStrategy(RoutingStrategy):
    """Maintains the same proxy for a given session/client.

    Across connectors a new session is placed by weighted rendezvous
    hashing: every connector scores the session id, scaled by its weight,
    and the best score wins. The same session id always lands on the same
    connector, and changing one weight moves only the proportional slice
    of sessions instead of reshuffling them all. A session already bound
    to a proxy keeps it while that proxy stays eligible, whatever the
    weights say.
    """

    def __init__(self) -> None:
        self._session_map: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "sticky"

    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        if not proxies:
            return None

        if session_id is None:
            return random.choice(proxies)

        cached = await self._lookup_binding(
            redis_client, project_id, session_id, proxies
        )
        if cached is not None:
            return cached

        selected = self._consistent_hash_pick(proxies, session_id)
        await self._remember_binding(
            redis_client, project_id, session_id, selected
        )
        return selected

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
        if len(groups) == 1:
            return await self.select(
                groups[0].proxies, session_id, redis_client=redis_client, project_id=project_id
            )
        if session_id is None:
            return random.choice(weighted_random_group(groups).proxies)

        everything = [p for g in groups for p in g.proxies]
        cached = await self._lookup_binding(redis_client, project_id, session_id, everything)
        if cached is not None:
            return cached

        group = self.pick_group(groups, session_id)
        selected = self._consistent_hash_pick(group.proxies, session_id)
        await self._remember_binding(redis_client, project_id, session_id, selected)
        return selected

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        """Weighted rendezvous hashing of the session id over the connectors."""
        if session_id is None:
            return weighted_random_group(groups)
        return max(groups, key=lambda g: _rendezvous_score(g, session_id))

    async def _lookup_binding(
        self,
        redis_client: "RedisClient | None",
        project_id: str | None,
        session_id: str,
        proxies: list[Proxy],
    ) -> Proxy | None:
        """Local cache first, then Redis (if available)."""
        cached_proxy_id = self._session_map.get(session_id)
        if cached_proxy_id is None and redis_client is not None and project_id is not None:
            cached_proxy_id = await redis_client.get_sticky_binding(
                project_id, session_id
            )
        if not cached_proxy_id:
            return None
        for proxy in proxies:
            if proxy.id == cached_proxy_id:
                self._session_map[session_id] = cached_proxy_id
                return proxy
        # Bound proxy is no longer in the eligible set - drop the stale cache
        # entry so the next call re-binds via consistent hashing.
        self._session_map.pop(session_id, None)
        return None

    @staticmethod
    def _consistent_hash_pick(proxies: list[Proxy], session_id: str) -> Proxy:
        hash_value = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
        return proxies[hash_value % len(proxies)]

    async def _remember_binding(
        self,
        redis_client: "RedisClient | None",
        project_id: str | None,
        session_id: str,
        proxy: Proxy,
    ) -> None:
        self._session_map[session_id] = proxy.id
        if redis_client is not None and project_id is not None:
            await redis_client.set_sticky_binding(project_id, session_id, proxy.id)

    def allows_exit_reselection(self, session_id: str | None) -> bool:
        """A session under sticky routing was promised one exit; a misplaced exit fails it rather than moving it."""
        return session_id is None

    def reset(self) -> None:
        self._session_map.clear()


def _rendezvous_score(group: ProxyGroup, session_id: str) -> float:
    """Score of ``group`` for a session under weighted rendezvous hashing.

    ``-weight / ln(u)`` with ``u`` a uniform draw seeded by the group key
    and the session id. The connector with the highest score wins with
    probability proportional to its weight.
    """
    digest = hashlib.blake2s(f"{group.key}|{session_id}".encode(), digest_size=8).digest()
    u = (int.from_bytes(digest, "big") + 1) / (2**64 + 1)
    return -max(group.weight, 1) / math.log(u)
