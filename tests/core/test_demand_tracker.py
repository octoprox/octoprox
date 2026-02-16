"""Tests for DemandTracker class."""

import pytest

from api.core.demand_tracker import (
    DEMAND_KEY,
    DEMAND_THRESHOLDS,
    DEMAND_WINDOW_SECONDS,
    DemandLevel,
    DemandTracker,
)
from api.db.redis import RedisClient


class TestDemandLevel:
    """Tests for DemandLevel enum."""

    def test_demand_level_values(self) -> None:
        """Test that DemandLevel has expected values."""
        assert DemandLevel.LOW.value == "low"
        assert DemandLevel.MEDIUM.value == "medium"
        assert DemandLevel.HIGH.value == "high"


class TestDemandTracker:
    """Tests for DemandTracker class."""

    @pytest.fixture
    def demand_tracker(self, redis_client: RedisClient) -> DemandTracker:
        """Create a DemandTracker instance for testing."""
        return DemandTracker(redis_client)

    @pytest.mark.asyncio
    async def test_record_request_creates_entry(
        self, demand_tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        """Test that record_request creates an entry in Redis."""
        project_id = "test-project-1"
        key = DEMAND_KEY.format(project_id=project_id)

        # Initially no entries
        count = await redis_client.client.zcard(key)
        assert count == 0

        # Record a request
        await demand_tracker.record_request(project_id)

        # Should have one entry
        count = await redis_client.client.zcard(key)
        assert count == 1

    @pytest.mark.asyncio
    async def test_record_multiple_requests(
        self, demand_tracker: DemandTracker, redis_client: RedisClient
    ) -> None:
        """Test recording multiple requests."""
        project_id = "test-project-2"
        key = DEMAND_KEY.format(project_id=project_id)

        # Record multiple requests
        for _ in range(5):
            await demand_tracker.record_request(project_id)

        # Should have 5 entries
        count = await redis_client.client.zcard(key)
        assert count == 5

    @pytest.mark.asyncio
    async def test_get_requests_per_minute_no_requests(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test get_requests_per_minute with no requests."""
        project_id = "test-project-empty"
        rate = await demand_tracker.get_requests_per_minute(project_id)
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_get_requests_per_minute_with_requests(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test get_requests_per_minute with recorded requests."""
        project_id = "test-project-3"

        # Record 10 requests
        for _ in range(10):
            await demand_tracker.record_request(project_id)

        rate = await demand_tracker.get_requests_per_minute(project_id)
        # Since window is 60 seconds and we just recorded, rate should be 10
        assert rate == 10.0

    @pytest.mark.asyncio
    async def test_get_demand_level_low(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is LOW with few requests per proxy."""
        project_id = "test-project-low"

        # Record 5 requests with 1 proxy = 5 req/min/proxy (LOW)
        for _ in range(5):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_get_demand_level_medium(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is MEDIUM with moderate requests per proxy."""
        project_id = "test-project-medium"

        # Record 20 requests with 1 proxy = 20 req/min/proxy (MEDIUM)
        for _ in range(20):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_get_demand_level_high(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is HIGH with many requests per proxy."""
        project_id = "test-project-high"

        # Record 40 requests with 1 proxy = 40 req/min/proxy (HIGH)
        for _ in range(40):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.HIGH

    @pytest.mark.asyncio
    async def test_get_demand_level_no_proxies_with_requests(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test demand level is HIGH when no proxies but requests exist."""
        project_id = "test-project-no-proxies"

        await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=0)
        assert level == DemandLevel.HIGH



    @pytest.mark.asyncio
    async def test_get_demand_level_scales_with_proxy_count(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that demand level scales with proxy count."""
        project_id = "test-project-scaling"

        # Record 40 requests
        for _ in range(40):
            await demand_tracker.record_request(project_id)

        # With 1 proxy: 40 req/min/proxy = HIGH
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.HIGH

        # With 2 proxies: 20 req/min/proxy = MEDIUM
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=2)
        assert level == DemandLevel.MEDIUM

        # With 10 proxies: 4 req/min/proxy = LOW
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=10)
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_get_demand_info(self, demand_tracker: DemandTracker) -> None:
        """Test get_demand_info returns comprehensive information."""
        project_id = "test-project-info"

        # Record 15 requests
        for _ in range(15):
            await demand_tracker.record_request(project_id)

        info = await demand_tracker.get_demand_info(project_id, current_proxy_count=1)

        assert info["demand_level"] == DemandLevel.MEDIUM
        assert info["requests_per_minute"] == 15.0
        assert info["rate_per_proxy"] == 15.0
        assert info["proxy_count"] == 1

    @pytest.mark.asyncio
    async def test_get_demand_info_with_multiple_proxies(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test get_demand_info calculates rate_per_proxy correctly."""
        project_id = "test-project-info-2"

        # Record 30 requests
        for _ in range(30):
            await demand_tracker.record_request(project_id)

        info = await demand_tracker.get_demand_info(project_id, current_proxy_count=3)

        assert info["requests_per_minute"] == 30.0
        assert info["rate_per_proxy"] == 10.0
        assert info["proxy_count"] == 3
        assert info["demand_level"] == DemandLevel.LOW  # 10 req/min/proxy is at threshold

    @pytest.mark.asyncio
    async def test_demand_thresholds_boundary_values(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test demand level at exact threshold boundaries."""
        project_id = "test-project-boundary"

        # At exactly low_max (10) should be LOW
        for _ in range(10):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.LOW

        # Add one more to go to 11 - should be MEDIUM
        await demand_tracker.record_request(project_id)
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_get_demand_level_no_proxies_no_requests(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test demand level is LOW when no proxies and no requests."""
        project_id = "test-project-empty-2"

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=0)
        assert level == DemandLevel.LOW

