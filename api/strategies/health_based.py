# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Health-based routing strategy: the proxies that served this gateway best, with every new row tried first."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from api.models.proxy import Proxy
from api.routing.outcomes import LocalOutcomes
from api.strategies.base import RoutingStrategy

if TYPE_CHECKING:
    from api.db.redis import RedisClient

# Outcomes a row needs on this gateway before its score counts, in requests.
# Below this it is always a candidate. Ten keeps a cold gateway exploring for
# only a few requests per row, and one unlucky request among ten moves the
# success share by ten points, not all the way to zero.
HEALTH_MIN_SAMPLES = 10


class HealthBasedStrategy(RoutingStrategy):
    """Routes to the best-scoring third of the measured proxies and to every proxy not measured yet, one at random.

    Args:
        outcomes: this gateway's outcomes per proxy; None holds none, for a strategy no gateway serves through.
    """

    def __init__(self, outcomes: LocalOutcomes | None = None) -> None:
        self._outcomes = outcomes if outcomes is not None else LocalOutcomes()

    @property
    def name(self) -> str:
        return "health_based"

    @staticmethod
    def score(requests: int, successes: int, latency_sum_ms: float) -> float:
        """A proxy's score from its outcomes, higher is better: 60% success share, 40% connect latency.

        The latency part is 100 points at 0 ms falling to none at 1000 ms; a
        proxy whose latency was never measured gets a neutral 50.
        """
        success_rate = successes / requests * 100 if requests else 0.0
        average_ms = latency_sum_ms / requests if requests else 0.0
        latency_score = max(0.0, 100 - average_ms / 10) if average_ms > 0 else 50.0
        return success_rate * 0.6 + latency_score * 0.4

    async def select(
        self,
        proxies: list[Proxy],
        session_id: str | None = None,
        *,
        redis_client: RedisClient | None = None,
        project_id: str | None = None,
    ) -> Proxy | None:
        if not proxies:
            return None
        candidates: list[Proxy] = []
        scored: list[tuple[float, Proxy]] = []
        for proxy in proxies:
            requests, successes, latency_sum_ms = self._outcomes.of_proxy(proxy.id)
            if requests < HEALTH_MIN_SAMPLES:
                candidates.append(proxy)
            else:
                scored.append((self.score(requests, successes, latency_sum_ms), proxy))
        if scored:
            # Stable: proxies scoring the same keep their feed order.
            scored.sort(key=lambda entry: entry[0], reverse=True)
            candidates.extend(proxy for _, proxy in scored[: max(1, len(scored) // 3)])
        return random.choice(candidates)
