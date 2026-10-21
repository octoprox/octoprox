# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for reading demand: the rates over the per-minute hashes, the levels and their hysteresis.

The hashes are written straight into the real Redis test container, as the
gateways' metering leaves them (its own tests cover the writing), and the
tracker reads them at a fixed moment half a minute into a minute, so the
oldest minute of a window counts for exactly half.
"""

import pytest

from api.core.demand_tracker import (
    DEMAND_REJECTIONS_FIELD,
    DEMAND_REQUESTS_FIELD,
    DEMAND_TTL_SECONDS,
    DEMAND_WINDOW_SECONDS,
    RECENT_ACTIVITY_MIN_RPM,
    RECENT_ACTIVITY_WINDOW_SECONDS,
    DemandLevel,
    DemandTracker,
    demand_minute,
)
from api.db.redis import DEMAND_KEY, RedisClient

PROJECT = "project-1"
# Half a minute into an epoch minute.
NOW = 1_800_000_030.0
MINUTE = int(NOW // 60)


async def seed(
    redis_client: RedisClient, *, requests: int = 0, rejections: int = 0, minutes_ago: int = 0, project_id: str = PROJECT
) -> None:
    """Counts in one of the project's minute hashes, as gateways add them."""
    key = DEMAND_KEY.format(project_id=project_id, minute=MINUTE - minutes_ago)
    if requests:
        await redis_client.client.hincrby(key, DEMAND_REQUESTS_FIELD, requests)  # type: ignore[misc]
    if rejections:
        await redis_client.client.hincrby(key, DEMAND_REJECTIONS_FIELD, rejections)  # type: ignore[misc]


@pytest.fixture
def tracker(redis_client: RedisClient) -> DemandTracker:
    return DemandTracker(redis_client, clock=lambda: NOW)


class TestBuckets:
    def test_a_moment_falls_in_its_epoch_minute(self) -> None:
        assert demand_minute(NOW) == MINUTE
        assert demand_minute(MINUTE * 60) == MINUTE
        assert demand_minute(MINUTE * 60 - 0.001) == MINUTE - 1

    def test_the_windows_and_the_ttl(self) -> None:
        assert DEMAND_WINDOW_SECONDS == 300
        assert RECENT_ACTIVITY_WINDOW_SECONDS == 60
        assert RECENT_ACTIVITY_MIN_RPM == 1.0
        # A minute's hash outlives every read that includes part of it.
        assert DEMAND_TTL_SECONDS == DEMAND_WINDOW_SECONDS + 60


class TestRates:
    async def test_no_requests_no_rate(self, tracker: DemandTracker) -> None:
        assert await tracker.get_requests_per_minute(PROJECT) == 0.0
        assert await tracker.get_recent_requests_per_minute(PROJECT) == 0.0

    async def test_requests_per_minute_over_the_window(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, requests=10)
        # Ten requests in a five-minute window: two a minute.
        assert await tracker.get_requests_per_minute(PROJECT) == 2.0

    async def test_rejected_requests_are_demand_too(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, requests=4, rejections=6)
        assert await tracker.get_requests_per_minute(PROJECT) == 2.0

    async def test_the_oldest_minute_counts_for_its_share_still_inside(
        self, tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        for minutes_ago in range(6):
            await seed(redis_client, requests=10, minutes_ago=minutes_ago)
        # Five whole minutes plus half of the sixth: 55 requests over five minutes.
        assert await tracker.get_requests_per_minute(PROJECT) == 11.0

    async def test_minutes_outside_the_window_do_not_count(
        self, tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        await seed(redis_client, requests=1000, minutes_ago=6)
        assert await tracker.get_requests_per_minute(PROJECT) == 0.0

    async def test_projects_are_read_apart(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, requests=50, project_id="project-2")
        assert await tracker.get_requests_per_minute(PROJECT) == 0.0
        assert await tracker.get_requests_per_minute("project-2") == 10.0

    async def test_the_recent_rate_reads_the_last_minute(
        self, tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        await seed(redis_client, requests=10)
        await seed(redis_client, requests=20, minutes_ago=1)
        await seed(redis_client, requests=500, minutes_ago=2)
        # This minute so far, plus the half of the last one still inside.
        assert await tracker.get_recent_requests_per_minute(PROJECT) == 20.0


class TestRecentActivity:
    async def test_a_request_in_the_last_minute_is_activity(
        self, tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        await seed(redis_client, requests=1)
        assert await tracker.has_recent_activity(PROJECT) is True

    async def test_a_rejection_is_activity_too(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, rejections=1)
        assert await tracker.has_recent_activity(PROJECT) is True

    async def test_no_requests_is_no_activity(self, tracker: DemandTracker) -> None:
        assert await tracker.has_recent_activity(PROJECT) is False

    async def test_a_burst_left_only_in_the_long_window_is_no_activity(
        self, tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        await seed(redis_client, requests=500, minutes_ago=3)
        assert await tracker.get_requests_per_minute(PROJECT) == 100.0
        assert await tracker.has_recent_activity(PROJECT) is False


class TestDemandLevel:
    def test_values(self) -> None:
        assert (DemandLevel.LOW.value, DemandLevel.MEDIUM.value, DemandLevel.HIGH.value) == ("low", "medium", "high")

    async def test_low(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 25 requests -> 5.0 req/min/proxy (LOW, <= 12)
        await seed(redis_client, requests=25)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.LOW

    async def test_medium(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 100 requests -> 20.0 req/min/proxy (MEDIUM, > 12 and <= 35)
        await seed(redis_client, requests=100)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.MEDIUM

    async def test_high(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 200 requests -> 40.0 req/min/proxy (HIGH, > 35)
        await seed(redis_client, requests=200)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.HIGH

    async def test_requests_and_no_proxies_is_high(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, requests=1)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=0) == DemandLevel.HIGH

    async def test_rejections_and_no_proxies_is_high(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        await seed(redis_client, rejections=1)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=0) == DemandLevel.HIGH

    async def test_nothing_and_no_proxies_is_low(self, tracker: DemandTracker) -> None:
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=0) == DemandLevel.LOW

    async def test_the_level_is_per_proxy(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 200 requests -> 40.0 req/min
        await seed(redis_client, requests=200)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.HIGH
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=2) == DemandLevel.MEDIUM
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=10) == DemandLevel.LOW

    async def test_threshold_boundaries(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # Exactly 12.0 req/min/proxy is still LOW (it must exceed 12).
        await seed(redis_client, requests=60)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.LOW
        # One more -> 12.2 -> MEDIUM.
        await seed(redis_client, requests=1)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1) == DemandLevel.MEDIUM

    async def test_demand_info(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 100 requests -> 20.0 req/min
        await seed(redis_client, requests=100)
        info = await tracker.get_demand_info(PROJECT, current_proxy_count=1)
        assert info == {
            "demand_level": DemandLevel.MEDIUM, "requests_per_minute": 20.0, "rate_per_proxy": 20.0, "proxy_count": 1,
        }

    async def test_demand_info_per_proxy(self, tracker: DemandTracker, redis_client: RedisClient) -> None:
        # 150 requests -> 30.0 req/min over 3 proxies: 10.0 each, LOW on a first check.
        await seed(redis_client, requests=150)
        info = await tracker.get_demand_info(PROJECT, current_proxy_count=3)
        assert (info["requests_per_minute"], info["rate_per_proxy"], info["proxy_count"]) == (30.0, 10.0, 3)
        assert info["demand_level"] == DemandLevel.LOW


class TestHysteresis:
    @pytest.mark.parametrize(
        ("requests", "previous", "expected"),
        [
            # 10.0 req/min/proxy, the lower dead zone (8-12).
            (50, DemandLevel.LOW, DemandLevel.LOW),
            (50, DemandLevel.MEDIUM, DemandLevel.MEDIUM),
            (50, None, DemandLevel.LOW),
            # 30.0, the upper dead zone (25-35).
            (150, DemandLevel.HIGH, DemandLevel.HIGH),
            (150, DemandLevel.MEDIUM, DemandLevel.MEDIUM),
            # 7.0, clearly below medium_to_low (8).
            (35, DemandLevel.MEDIUM, DemandLevel.LOW),
            (35, DemandLevel.HIGH, DemandLevel.LOW),
            # 20.0, below high_to_medium (25) but not below 8.
            (100, DemandLevel.HIGH, DemandLevel.MEDIUM),
        ],
    )
    async def test_levels_move_only_past_their_threshold(
        self,
        tracker: DemandTracker,
        redis_client: RedisClient,
        requests: int,
        previous: DemandLevel | None,
        expected: DemandLevel,
    ) -> None:
        await seed(redis_client, requests=requests)
        assert await tracker.get_demand_level(PROJECT, current_proxy_count=1, previous_level=previous) == expected
