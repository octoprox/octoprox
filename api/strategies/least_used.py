# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Least-used routing strategy."""

from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.strategies.base import ProxyGroup, RoutingStrategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class LeastUsedStrategy(RoutingStrategy):
    """Selects the proxy with the minimum request count.

    Across connectors the weight acts as relative capacity: the connector
    with the lowest ``requests / weight`` is picked, so a connector with
    weight 3 is allowed three times the requests of one with weight 1
    before it stops being the least used. The requests counted are the
    connector's (``ProxyGroup.load``), not only those of the rows eligible
    for this request, so a filtered request does not mistake a busy
    connector with a few eligible rows for an idle one.
    """

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

    def pick_group(self, groups: list[ProxyGroup], session_id: str | None) -> ProxyGroup:
        return min(groups, key=lambda g: g.load / max(g.weight, 1))
