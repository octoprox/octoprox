# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Least-used routing strategy."""

from api.models.proxy import Proxy
from api.strategies.base import RoutingStrategy


class LeastUsedStrategy(RoutingStrategy):
    """Selects the proxy with the minimum request count."""

    @property
    def name(self) -> str:
        return "least_used"

    def select(self, proxies: list[Proxy], session_id: str | None = None) -> Proxy | None:
        if not proxies:
            return None

        return min(proxies, key=lambda p: p.request_count)

