# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Random routing strategy."""

import random
from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.strategies.base import RoutingStrategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class RandomStrategy(RoutingStrategy):
    """Selects a random proxy from the pool."""

    @property
    def name(self) -> str:
        return "random"

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

        return random.choice(proxies)

