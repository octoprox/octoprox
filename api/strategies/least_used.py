# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Least-used routing strategy."""

from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.strategies.base import RoutingStrategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class LeastUsedStrategy(RoutingStrategy):
    """Selects the proxy with the minimum request count."""

    @property
    def name(self) -> str:
        return "least_used"

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

        return min(proxies, key=lambda p: p.request_count)

