# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class for routing strategies."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from api.models.proxy import Proxy

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class RoutingStrategy(ABC):
    """Abstract base class for proxy routing strategies.

    ``select`` is async so strategies that need cross-instance state
    (e.g. sticky session bindings in Redis) can do that I/O directly
    without forcing the caller to know about it. Stateless strategies
    (round-robin, random, least-used, health-based) ignore the
    ``redis_client`` / ``project_id`` kwargs and run effectively
    synchronously.
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

    def reset(self) -> None:  # noqa: B027
        """Reset any internal state. Override if needed."""
