# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Demand: how many requests each project asked for lately, for scaling and the api's demand view.

Only the gateways' request metering writes it, to a Redis hash per project and
minute, and :class:`DemandTracker` reads it; losing Redis empties the window,
so callers treat a project with no demand at all as unknown rather than idle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from api.db.redis import DEMAND_KEY

if TYPE_CHECKING:
    from api.db.redis import RedisClient

# The span demand is averaged over, in seconds: long enough that one burst
# does not scale a pool on its own, short enough that a pool follows its
# traffic within minutes. A whole number of minutes, the buckets' size.
DEMAND_WINDOW_SECONDS = 300
# The span that tells traffic still arriving from a burst that is only left
# in the longer window (it gates scaling up).
RECENT_ACTIVITY_WINDOW_SECONDS = 60
# Requests per minute in the recent span that count as activity.
RECENT_ACTIVITY_MIN_RPM = 1.0
# How long a demand hash lives after its last write, in seconds: the window
# plus the minute the hash spans, so a minute's hash outlives every read that
# still includes part of it, and goes a minute after it has left the window.
DEMAND_TTL_SECONDS = DEMAND_WINDOW_SECONDS + 60
# The fields of a demand hash: requests that completed through a proxy, and
# requests rejected before any proxy served them (no proxy, every proxy
# quarantined, a traffic limit). Both are demand the pool is asked to meet.
DEMAND_REQUESTS_FIELD = "r"
DEMAND_REJECTIONS_FIELD = "x"


def demand_minute(epoch_seconds: float) -> int:
    """The bucket a moment falls in: whole minutes since the epoch, the ``{minute}`` of ``DEMAND_KEY``."""
    return int(epoch_seconds // 60)


class DemandLevel(StrEnum):
    """Demand level classification for auto-scaling."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Hysteresis thresholds for demand classification (requests per minute per proxy)
# Directional thresholds prevent oscillation between levels
DEMAND_THRESHOLDS = {
    "low_to_medium": 12,     # Must exceed 12 to leave LOW
    "medium_to_low": 8,      # Must drop below 8 to enter LOW
    "medium_to_high": 35,    # Must exceed 35 to enter HIGH
    "high_to_medium": 25,    # Must drop below 25 to leave HIGH
}


class DemandTracker:
    """Reads each project's demand from the per-minute hashes the gateways write.

    Args:
        redis_client: Holds the demand hashes.
        clock: Epoch seconds now; tests pass their own.
    """

    def __init__(self, redis_client: RedisClient, *, clock: Callable[[], float] = time.time) -> None:
        self._redis_client = redis_client
        self._clock = clock

    async def get_requests_per_minute(self, project_id: str) -> float:
        """The project's requests per minute over the demand window, rejected ones included."""
        return await self._rate(project_id, DEMAND_WINDOW_SECONDS)

    async def get_recent_requests_per_minute(self, project_id: str) -> float:
        """The project's requests per minute over the last minute only.

        Tells whether traffic is arriving now, as opposed to a burst that is
        only left in the longer window.
        """
        return await self._rate(project_id, RECENT_ACTIVITY_WINDOW_SECONDS)

    async def has_recent_activity(self, project_id: str) -> bool:
        """Whether the project's recent rate reaches ``RECENT_ACTIVITY_MIN_RPM``.

        Gates scaling up, so a stale burst in the longer window does not add
        proxies to a project nobody uses any more.
        """
        return await self.get_recent_requests_per_minute(project_id) >= RECENT_ACTIVITY_MIN_RPM

    async def get_demand_level(
        self,
        project_id: str,
        current_proxy_count: int,
        previous_level: DemandLevel | None = None,
    ) -> DemandLevel:
        """Calculate the current demand level for a project.

        Uses hysteresis thresholds to prevent oscillation between levels.
        The thresholds for transitioning up are higher than those for
        transitioning down, creating a dead zone that stabilizes scaling.

        Args:
            project_id: The project ID to calculate demand for.
            current_proxy_count: Number of active proxies for the project.
            previous_level: The previous demand level (for hysteresis).
                If None (first check), uses conservative thresholds.

        Returns:
            DemandLevel enum value.
        """
        requests_per_minute = await self.get_requests_per_minute(project_id)

        # Avoid division by zero
        if current_proxy_count <= 0:
            # No proxies - if there are requests, demand is HIGH
            if requests_per_minute > 0:
                return DemandLevel.HIGH
            return DemandLevel.LOW

        # Calculate per-proxy rate
        rate_per_proxy = requests_per_minute / current_proxy_count

        # Apply hysteresis based on previous level
        if previous_level is None or previous_level == DemandLevel.LOW:
            # From LOW (or first check): use scale-up thresholds
            if rate_per_proxy > DEMAND_THRESHOLDS["medium_to_high"]:
                return DemandLevel.HIGH
            elif rate_per_proxy > DEMAND_THRESHOLDS["low_to_medium"]:
                return DemandLevel.MEDIUM
            else:
                return DemandLevel.LOW
        elif previous_level == DemandLevel.MEDIUM:
            # From MEDIUM: need to clearly break out in either direction
            if rate_per_proxy > DEMAND_THRESHOLDS["medium_to_high"]:
                return DemandLevel.HIGH
            elif rate_per_proxy < DEMAND_THRESHOLDS["medium_to_low"]:
                return DemandLevel.LOW
            else:
                return DemandLevel.MEDIUM
        else:  # HIGH
            # From HIGH: need to clearly drop to leave
            if rate_per_proxy < DEMAND_THRESHOLDS["medium_to_low"]:
                return DemandLevel.LOW
            elif rate_per_proxy < DEMAND_THRESHOLDS["high_to_medium"]:
                return DemandLevel.MEDIUM
            else:
                return DemandLevel.HIGH

    async def get_demand_info(
        self,
        project_id: str,
        current_proxy_count: int,
        previous_level: DemandLevel | None = None,
    ) -> dict[str, Any]:
        """Get comprehensive demand information for a project.

        Args:
            project_id: The project ID.
            current_proxy_count: Number of active proxies.
            previous_level: The previous demand level (for hysteresis).

        Returns:
            Dict with demand_level, requests_per_minute, rate_per_proxy.
        """
        requests_per_minute = await self.get_requests_per_minute(project_id)

        rate_per_proxy = 0.0
        if current_proxy_count > 0:
            rate_per_proxy = requests_per_minute / current_proxy_count

        demand_level = await self.get_demand_level(
            project_id, current_proxy_count, previous_level
        )

        return {
            "demand_level": demand_level,
            "requests_per_minute": requests_per_minute,
            "rate_per_proxy": rate_per_proxy,
            "proxy_count": current_proxy_count,
        }

    async def _rate(self, project_id: str, span_seconds: int) -> float:
        """Requests per minute over the last ``span_seconds``, from the minute hashes the span touches, in one round trip.

        The current minute counts with what it holds so far and the oldest only
        for the share of it still inside the span: the usual sliding-window
        estimate, exact while the traffic is steady.
        """
        now = self._clock()
        minutes = span_seconds // 60
        current = demand_minute(now)
        # The share of the oldest minute that is still inside the span.
        inside = 1.0 - (now - current * 60) / 60
        pipe = self._redis_client.client.pipeline(transaction=False)
        for minute in range(current - minutes, current + 1):
            key = DEMAND_KEY.format(project_id=project_id, minute=minute)
            pipe.hmget(key, [DEMAND_REQUESTS_FIELD, DEMAND_REJECTIONS_FIELD])
        rows: list[list[str | None]] = await pipe.execute()
        total = 0.0
        for offset, values in enumerate(rows):
            weight = inside if offset == 0 else 1.0
            total += weight * sum(int(value) for value in values if value is not None)
        return total / minutes
