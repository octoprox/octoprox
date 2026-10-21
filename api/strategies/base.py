# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class for routing strategies."""

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from api.models.proxy import Proxy

if TYPE_CHECKING:
    from api.db.redis import RedisClient


@dataclass(frozen=True)
class ProxyGroup:
    """The eligible proxies of one connector, with the connector's routing weight.

    ``key`` identifies the group (the connector id) so stateful strategies
    can keep per-group state across calls. ``weight`` is the connector's
    relative share; only its ratio to the other groups' weights matters.
    """

    key: str
    weight: int
    proxies: list[Proxy] = field(default_factory=list)


class RoutingStrategy(ABC):
    """Spreads a project's requests: the connector by weight (:meth:`pick_group`), then the proxy in it (:meth:`select`).

    Only sticky does I/O, its session bindings in Redis; the others route on memory alone.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the strategy name."""
        ...

    @abstractmethod
    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        """Select a proxy from the available pool.

        Args:
            proxies: Healthy proxies eligible for selection.
            session_id: Optional client session identifier.
            redis_client: Optional Redis client; used by stateful
                strategies for cross-instance lookup/persistence. May be
                None for tests or strategies that don't need it.
            project_id: Project the selection is scoped to; pairs with
                ``redis_client`` for namespacing persisted state.

        Returns:
            Selected proxy or None if no proxies available.
        """
        ...

    async def select_weighted(
        self,
        groups: list[ProxyGroup],
        session_id: str | None = None,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        """Select a proxy across connectors, honouring their routing weights.

        Groups without proxies are ignored: a connector with nothing
        eligible for this request takes no share, and the others split the
        traffic in proportion to their own weights. Strategies override
        ``pick_group`` to say how the connector is chosen; the proxy inside
        it is then chosen by ``select`` as usual.
        """
        groups = [g for g in groups if g.proxies]
        if not groups:
            return None
        group = groups[0] if len(groups) == 1 else self.pick_group(groups, session_id)
        return await self.select(
            group.proxies, session_id, redis_client=redis_client, project_id=project_id
        )

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        """Choose the connector for a request; ``groups`` all have proxies and there are at least two.

        The default is a weighted random draw, which is what every strategy
        without an opinion about connectors uses. Weights are relative:
        weights 1 and 3 give the second connector three quarters of the
        requests.
        """
        return weighted_random_group(groups)

    def forget_group(self, key: str) -> None:  # noqa: B027
        """Drop any per-connector state for ``key``; called when the connector is removed."""

    async def settle(self) -> None:  # noqa: B027
        """Wait for the writes this strategy has under way, none by default; called as the gateway stops."""

    async def held_proxy(
        self,
        session_id: str,
        *,
        redis_client: "RedisClient | None" = None,
        project_id: str | None = None,
    ) -> str | None:
        """The proxy this strategy holds ``session_id`` to, which the router keeps it on while quarantined; none by default."""
        return None

    def allows_exit_reselection(self, session_id: str | None) -> bool:
        """Whether a request may move to another proxy after its selection was found misplaced.

        Preflight asks this before retrying with a different upstream. The
        default says yes: stateless strategies promise nothing about which
        proxy a request gets. A strategy that binds sessions overrides it.
        """
        return True

    def reset(self) -> None:  # noqa: B027
        """Reset any internal state. Override if needed."""


def weighted_random_group(groups: list[ProxyGroup]) -> ProxyGroup:
    """One group drawn at random with probability proportional to its weight."""
    return random.choices(groups, weights=[max(g.weight, 1) for g in groups])[0]
