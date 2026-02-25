# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Round-robin routing strategy."""

from api.models.proxy import Proxy
from api.strategies.base import RoutingStrategy


class RoundRobinStrategy(RoutingStrategy):
    """Cycles through proxies sequentially."""

    def __init__(self) -> None:
        self._index = 0

    @property
    def name(self) -> str:
        return "round_robin"

    def select(self, proxies: list[Proxy], session_id: str | None = None) -> Proxy | None:
        if not proxies:
            return None

        # Ensure index is within bounds
        self._index = self._index % len(proxies)
        proxy = proxies[self._index]
        self._index = (self._index + 1) % len(proxies)

        return proxy

    def reset(self) -> None:
        self._index = 0

