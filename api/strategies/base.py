# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class for routing strategies."""

from abc import ABC, abstractmethod

from api.models.proxy import Proxy


class RoutingStrategy(ABC):
    """Abstract base class for proxy routing strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the strategy name."""
        ...

    @abstractmethod
    def select(self, proxies: list[Proxy], session_id: str | None = None) -> Proxy | None:
        """Select a proxy from the available pool.

        Args:
            proxies: List of available proxies to choose from.
            session_id: Optional session identifier for sticky sessions.

        Returns:
            Selected proxy or None if no proxies available.
        """
        ...

    def reset(self) -> None:  # noqa: B027
        """Reset any internal state. Override if needed."""
