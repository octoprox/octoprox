"""Tests for DemandTracker class."""

import pytest

from api.core.demand_tracker import (
    DEMAND_KEY,
    DEMAND_THRESHOLDS,
    DEMAND_WINDOW_SECONDS,
    RECENT_ACTIVITY_MIN_RPM,
    RECENT_ACTIVITY_WINDOW_SECONDS,
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
        # Window is 300s, rate = 10 * (60/300) = 2.0
        assert rate == 2.0

    @pytest.mark.asyncio
    async def test_get_demand_level_low(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is LOW with few requests per proxy."""
        project_id = "test-project-low"

        # Record 25 requests with 1 proxy = 25*0.2 = 5.0 req/min/proxy (LOW, <= 12)
        for _ in range(25):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_get_demand_level_medium(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is MEDIUM with moderate requests per proxy."""
        project_id = "test-project-medium"

        # Record 100 requests with 1 proxy = 100*0.2 = 20.0 req/min/proxy (MEDIUM, > 12 and <= 35)
        for _ in range(100):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_get_demand_level_high(self, demand_tracker: DemandTracker) -> None:
        """Test demand level is HIGH with many requests per proxy."""
        project_id = "test-project-high"

        # Record 200 requests with 1 proxy = 200*0.2 = 40.0 req/min/proxy (HIGH, > 35)
        for _ in range(200):
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

        # Record 200 requests → rate = 40.0 req/min
        for _ in range(200):
            await demand_tracker.record_request(project_id)

        # With 1 proxy: 40.0 req/min/proxy = HIGH (> 35)
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.HIGH

        # With 2 proxies: 20.0 req/min/proxy = MEDIUM (> 12, <= 35)
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=2)
        assert level == DemandLevel.MEDIUM

        # With 10 proxies: 4.0 req/min/proxy = LOW (<= 12)
        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=10)
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_get_demand_info(self, demand_tracker: DemandTracker) -> None:
        """Test get_demand_info returns comprehensive information."""
        project_id = "test-project-info"

        # Record 100 requests → rate = 20.0 req/min
        for _ in range(100):
            await demand_tracker.record_request(project_id)

        info = await demand_tracker.get_demand_info(project_id, current_proxy_count=1)

        assert info["demand_level"] == DemandLevel.MEDIUM
        assert info["requests_per_minute"] == 20.0
        assert info["rate_per_proxy"] == 20.0
        assert info["proxy_count"] == 1

    @pytest.mark.asyncio
    async def test_get_demand_info_with_multiple_proxies(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test get_demand_info calculates rate_per_proxy correctly."""
        project_id = "test-project-info-2"

        # Record 150 requests → rate = 30.0 req/min
        for _ in range(150):
            await demand_tracker.record_request(project_id)

        info = await demand_tracker.get_demand_info(project_id, current_proxy_count=3)

        assert info["requests_per_minute"] == 30.0
        assert info["rate_per_proxy"] == 10.0
        assert info["proxy_count"] == 3
        # 10.0 req/min/proxy is LOW (from None, <= 12)
        assert info["demand_level"] == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_demand_thresholds_boundary_values(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test demand level at exact threshold boundaries (from LOW / first check)."""
        project_id = "test-project-boundary"

        # At exactly 12.0 req/min/proxy (60 requests * 0.2) should be LOW (need > 12)
        for _ in range(60):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(project_id, current_proxy_count=1)
        assert level == DemandLevel.LOW

        # Add one more → 12.2 req/min/proxy → MEDIUM (> 12)
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


class TestDemandHysteresis:
    """Tests for hysteresis behavior in demand level transitions."""

    @pytest.fixture
    def demand_tracker(self, redis_client: RedisClient) -> DemandTracker:
        """Create a DemandTracker instance for testing."""
        return DemandTracker(redis_client)

    @pytest.mark.asyncio
    async def test_hysteresis_low_stays_low_in_dead_zone(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that LOW stays LOW when rate is in dead zone (8-12)."""
        project_id = "test-hysteresis-1"

        # 50 requests * 0.2 = 10.0 req/min/proxy (between 8 and 12)
        for _ in range(50):
            await demand_tracker.record_request(project_id)

        # From LOW: 10.0 <= 12, so stays LOW
        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.LOW
        )
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_hysteresis_medium_stays_medium_in_dead_zone(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that MEDIUM stays MEDIUM when rate is in dead zone (8-12)."""
        project_id = "test-hysteresis-2"

        # 50 requests * 0.2 = 10.0 req/min/proxy (between 8 and 12)
        for _ in range(50):
            await demand_tracker.record_request(project_id)

        # From MEDIUM: 10.0 >= 8, so stays MEDIUM
        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.MEDIUM
        )
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_hysteresis_high_stays_high_in_dead_zone(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that HIGH stays HIGH when rate is in dead zone (25-35)."""
        project_id = "test-hysteresis-3"

        # 150 requests * 0.2 = 30.0 req/min/proxy (between 25 and 35)
        for _ in range(150):
            await demand_tracker.record_request(project_id)

        # From HIGH: 30.0 >= 25, so stays HIGH
        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.HIGH
        )
        assert level == DemandLevel.HIGH

    @pytest.mark.asyncio
    async def test_hysteresis_medium_stays_medium_in_upper_dead_zone(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that MEDIUM stays MEDIUM when rate is in upper dead zone (25-35)."""
        project_id = "test-hysteresis-4"

        # 150 requests * 0.2 = 30.0 req/min/proxy (between 25 and 35)
        for _ in range(150):
            await demand_tracker.record_request(project_id)

        # From MEDIUM: 30.0 <= 35, so stays MEDIUM
        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.MEDIUM
        )
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_hysteresis_medium_drops_to_low(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that MEDIUM drops to LOW when rate clearly drops below medium_to_low."""
        project_id = "test-hysteresis-5"

        # 35 requests * 0.2 = 7.0 req/min/proxy (< 8 = medium_to_low)
        for _ in range(35):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.MEDIUM
        )
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_hysteresis_high_drops_to_medium(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that HIGH drops to MEDIUM when rate drops below high_to_medium."""
        project_id = "test-hysteresis-6"

        # 100 requests * 0.2 = 20.0 req/min/proxy (< 25 = high_to_medium, >= 8)
        for _ in range(100):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.HIGH
        )
        assert level == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_hysteresis_high_drops_to_low(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that HIGH drops to LOW when rate drops below medium_to_low."""
        project_id = "test-hysteresis-7"

        # 35 requests * 0.2 = 7.0 req/min/proxy (< 8 = medium_to_low)
        for _ in range(35):
            await demand_tracker.record_request(project_id)

        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=DemandLevel.HIGH
        )
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_hysteresis_first_check_is_conservative(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test that first check (no previous) uses scale-up thresholds."""
        project_id = "test-hysteresis-8"

        # 50 requests * 0.2 = 10.0 req/min/proxy (between 8 and 12)
        for _ in range(50):
            await demand_tracker.record_request(project_id)

        # No previous level: 10.0 <= 12, so LOW (conservative)
        level = await demand_tracker.get_demand_level(
            project_id, current_proxy_count=1, previous_level=None
        )
        assert level == DemandLevel.LOW

    @pytest.mark.asyncio
    async def test_window_is_5_minutes(self) -> None:
        """Test that the demand window is 5 minutes (300 seconds)."""
        assert DEMAND_WINDOW_SECONDS == 300


class TestRecentActivity:
    """Tests for recent activity detection (dual-window)."""

    @pytest.fixture
    def demand_tracker(self, redis_client: RedisClient) -> DemandTracker:
        """Create a DemandTracker instance for testing."""
        return DemandTracker(redis_client)

    @pytest.mark.asyncio
    async def test_recent_rpm_no_requests(self, demand_tracker: DemandTracker) -> None:
        """Test recent RPM is 0 with no requests."""
        rate = await demand_tracker.get_recent_requests_per_minute("empty-project")
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_recent_rpm_with_requests(self, demand_tracker: DemandTracker) -> None:
        """Test recent RPM counts only requests in the short window."""
        project_id = "test-recent-rpm"

        # Record 10 requests (all within the 60s recent window)
        for _ in range(10):
            await demand_tracker.record_request(project_id)

        rate = await demand_tracker.get_recent_requests_per_minute(project_id)
        # 10 requests in 60s window → 10 * (60/60) = 10.0 req/min
        assert rate == 10.0

    @pytest.mark.asyncio
    async def test_has_recent_activity_true(self, demand_tracker: DemandTracker) -> None:
        """Test has_recent_activity returns True when there are recent requests."""
        project_id = "test-recent-active"

        await demand_tracker.record_request(project_id)

        assert await demand_tracker.has_recent_activity(project_id) is True

    @pytest.mark.asyncio
    async def test_has_recent_activity_false_no_requests(
        self, demand_tracker: DemandTracker
    ) -> None:
        """Test has_recent_activity returns False with no requests."""
        assert await demand_tracker.has_recent_activity("no-requests") is False

    @pytest.mark.asyncio
    async def test_recent_activity_window_constant(self) -> None:
        """Test that the recent activity window is 60 seconds."""
        assert RECENT_ACTIVITY_WINDOW_SECONDS == 60

    @pytest.mark.asyncio
    async def test_recent_activity_min_rpm_constant(self) -> None:
        """Test that the minimum RPM threshold is 1.0."""
        assert RECENT_ACTIVITY_MIN_RPM == 1.0
