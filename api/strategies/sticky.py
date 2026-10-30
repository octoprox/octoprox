# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sticky session routing: a session keeps one upstream proxy, bound in memory here and in Redis for the other gateways.

A binding lives ``STICKY_BINDING_TTL_SECONDS`` past the session's last request, and Redis failing never fails a request.
"""

import hashlib
import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.core.logging import error_text
from api.core.tasks import HeldTasks
from api.models.proxy import Proxy
from api.strategies.base import ProxyGroup, RoutingStrategy, weighted_random_group
from api.strategies.hash_ring import HashRing, ring_position

if TYPE_CHECKING:
    from api.db.redis import RedisClient

logger = structlog.get_logger()

# How long a session keeps its proxy after its last request, local and in Redis.
STICKY_BINDING_TTL_SECONDS = 300
# Expired local entries are swept every this many new local entries, whether
# bound here or warmed from Redis, so abandoned sessions cannot grow the map
# without bound between their own lookups.
_SWEEP_EVERY = 1024


@dataclass(slots=True)
class _Binding:
    proxy_id: str
    # Local expiry on the strategy's clock, pushed out by every request.
    expires_at: float
    # When the Redis copy is next rewritten; zero forces a write.
    redis_due_at: float = 0.0


class StickySessionStrategy(RoutingStrategy):
    """Maintains the same proxy for a given session; a request without one is routed like random.

    Across connectors a new session is placed by weighted rendezvous
    hashing: every connector scores the session id, scaled by its weight,
    and the best score wins. The same session id always lands on the same
    connector, and changing one weight moves only the proportional slice
    of sessions instead of reshuffling them all. Inside the connector the
    proxy is placed on the connector's :class:`HashRing`, kept here until
    its rows change, so a proxy leaving or joining the eligible set moves
    only the sessions it held or wins. A session already bound to a proxy
    keeps it while that proxy stays eligible, whatever the weights say.
    """

    def __init__(
        self,
        ttl_seconds: float = STICKY_BINDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_map: dict[str, _Binding] = {}
        self._ttl = ttl_seconds
        self._clock = clock
        self._bindings_since_sweep = 0
        # Binding writes on their way to Redis, held so none is collected before it runs.
        self._writes = HeldTasks()
        # Connector id -> the ring over its rows, rebuilt when they change.
        self._rings: dict[str, HashRing] = {}

    @property
    def name(self) -> str:
        return "sticky"

    async def settle(self) -> None:
        """Wait for the binding writes under way; the gateway calls it once its listeners are closed."""
        await self._writes.settle()

    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        # One connector whose rows are these proxies.
        return await self.select_weighted(
            [ProxyGroup(key="", weight=1, proxies=proxies)], session_id, redis_client=redis_client, project_id=project_id
        )

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
        if session_id is None:
            return random.choice(weighted_random_group(groups).proxies)

        eligible = {proxy.id: proxy for group in groups for proxy in group.proxies}
        cached = await self._lookup_binding(redis_client, project_id, session_id, eligible)
        if cached is not None:
            return cached

        group = groups[0] if len(groups) == 1 else self.pick_group(groups, session_id)
        members = group.members or group.proxies
        ring = self._rings.get(group.key)
        if ring is None or not ring.matches(members):
            ring = self._rings[group.key] = HashRing(members)
        # The group's eligible proxies are among its members, so the ring finds one.
        selected = ring.place(ring_position(session_id), eligible)
        if selected is not None:
            self._remember_binding(redis_client, project_id, session_id, selected)
        return selected

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        """Weighted rendezvous hashing of the session id over the connectors."""
        if session_id is None:
            return weighted_random_group(groups)
        return max(groups, key=lambda g: _rendezvous_score(g, session_id))

    def bound_proxy_id(self, session_id: str) -> str | None:
        """The proxy this instance currently has ``session_id`` bound to, if the binding is live."""
        binding = self._live_binding(session_id)
        return binding.proxy_id if binding is not None else None

    async def held_proxy(
        self,
        session_id: str,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> str | None:
        """The proxy ``session_id`` is bound to: this instance's binding, else the one Redis holds, remembered here."""
        bound = self.bound_proxy_id(session_id)
        if bound is None and redis_client is not None and project_id is not None:
            bound = await self.stored_binding(redis_client, project_id, session_id)
            if bound is not None:
                self.remember(session_id, bound)
        return bound

    def remember(self, session_id: str, proxy_id: str) -> _Binding:
        """Bind ``session_id`` to ``proxy_id`` on this instance, or push out the expiry of that binding.

        Local only: the picks and the bindings read from Redis are remembered, so later local checks do not go back there.
        """
        now = self._clock()
        binding = self._session_map.get(session_id)
        if binding is not None and binding.proxy_id == proxy_id:
            binding.expires_at = now + self._ttl
            return binding
        binding = _Binding(proxy_id, now + self._ttl)
        if session_id not in self._session_map:
            self._bindings_since_sweep += 1
            if self._bindings_since_sweep >= _SWEEP_EVERY:
                self._bindings_since_sweep = 0
                self._sweep_expired()
        self._session_map[session_id] = binding
        return binding

    def _live_binding(self, session_id: str) -> _Binding | None:
        binding = self._session_map.get(session_id)
        if binding is None:
            return None
        if binding.expires_at <= self._clock():
            del self._session_map[session_id]
            return None
        return binding

    def _sweep_expired(self) -> None:
        now = self._clock()
        for session_id, binding in list(self._session_map.items()):
            if binding.expires_at <= now:
                del self._session_map[session_id]

    def _refresh_redis(
        self,
        redis_client: "RedisClient | None",
        project_id: str | None,
        session_id: str,
        binding: _Binding,
    ) -> None:
        """Rewrite the Redis copy in the background when it is new or half its TTL has passed since the last write."""
        if redis_client is None or project_id is None:
            return
        now = self._clock()
        if binding.redis_due_at > now:
            return
        binding.redis_due_at = now + self._ttl / 2
        self._writes.spawn(
            self._store(redis_client, project_id, session_id, binding.proxy_id),
            name="sticky_binding_write",
            project_id=project_id,
        )

    async def _store(self, redis_client: "RedisClient", project_id: str, session_id: str, proxy_id: str) -> None:
        """Write one binding to Redis; one Redis does not take stays local until its next rewrite."""
        try:
            await redis_client.set_sticky_binding(project_id, session_id, proxy_id, ttl_seconds=max(1, int(self._ttl)))
        except Exception as exc:
            logger.debug("Sticky binding could not be stored", project_id=project_id, error=error_text(exc))

    async def stored_binding(self, redis_client: "RedisClient", project_id: str, session_id: str) -> str | None:
        """The proxy Redis has ``session_id`` bound to, by any gateway; None when unbound or unreadable."""
        try:
            return await redis_client.get_sticky_binding(project_id, session_id)
        except Exception as exc:
            logger.debug("Sticky binding could not be read", project_id=project_id, error=error_text(exc))
            return None

    async def _lookup_binding(
        self,
        redis_client: "RedisClient | None",
        project_id: str | None,
        session_id: str,
        eligible: Mapping[str, Proxy],
    ) -> Proxy | None:
        """Local cache first, then Redis (if available)."""
        cached_proxy_id = await self.held_proxy(session_id, redis_client=redis_client, project_id=project_id)
        if not cached_proxy_id:
            return None
        proxy = eligible.get(cached_proxy_id)
        if proxy is not None:
            binding = self.remember(session_id, cached_proxy_id)
            self._refresh_redis(redis_client, project_id, session_id, binding)
            return proxy
        # Bound proxy is no longer in the eligible set - drop the stale cache
        # entry so the next call places the session on the ring again.
        self._session_map.pop(session_id, None)
        return None

    def _remember_binding(
        self,
        redis_client: "RedisClient | None",
        project_id: str | None,
        session_id: str,
        proxy: Proxy,
    ) -> None:
        binding = self.remember(session_id, proxy.id)
        self._refresh_redis(redis_client, project_id, session_id, binding)

    def allows_exit_reselection(self, session_id: str | None) -> bool:
        """A session under sticky routing was promised one exit; a misplaced exit fails it rather than moving it."""
        return session_id is None

    def forget_group(self, key: str) -> None:
        self._rings.pop(key, None)

    def reset(self) -> None:
        self._session_map.clear()
        self._bindings_since_sweep = 0
        self._rings.clear()


def _session_hash(key: str, session_id: str) -> int:
    """A 64-bit hash of a session id paired with a connector's or a proxy's id."""
    digest = hashlib.blake2s(f"{key}|{session_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _rendezvous_score(group: ProxyGroup, session_id: str) -> float:
    """Score of ``group`` for a session under weighted rendezvous hashing.

    ``-weight / ln(u)`` with ``u`` a uniform draw seeded by the group key
    and the session id. The connector with the highest score wins with
    probability proportional to its weight.
    """
    u = (_session_hash(group.key, session_id) + 1) / (2**64 + 1)
    return -max(group.weight, 1) / math.log(u)
