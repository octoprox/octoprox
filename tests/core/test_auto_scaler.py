"""Tests for AutoScaler class."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.auto_scaler import AutoScaler, CHECK_INTERVAL_SECONDS, MAX_ERROR_BACKOFF_MINUTES, SCALING_COOLDOWN_SECONDS
from api.core.demand_tracker import DemandLevel
from api.core import signals, utc_now
from api.models.connector import CloudConnectorConfig, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


@pytest.fixture
def mock_data_provider() -> MagicMock:
    """Create a mock data provider for testing."""
    provider = MagicMock()
    provider.connectors = []
    provider.get_credential = MagicMock(return_value=None)
    provider.get_active_proxies_for_connector = MagicMock(return_value=[])
    provider.get_proxies_for_connector = MagicMock(return_value=[])
    provider.demand_tracker = MagicMock()
    provider.demand_tracker.get_demand_level = AsyncMock(return_value=DemandLevel.LOW)
    provider.demand_tracker.has_recent_activity = AsyncMock(return_value=True)
    return provider


@pytest.fixture
def auto_scaler(mock_data_provider: MagicMock) -> AutoScaler:
    """Create an AutoScaler instance for testing."""
    return AutoScaler(mock_data_provider)


@pytest.fixture
def sample_connector() -> Connector:
    """Create a sample connector for testing."""
    return Connector(
        id="test-connector-1",
        name="Test AWS Connector",
        credential_id="test-credential-1",
        credential_type=CredentialType.AWS,
        project_id="test-project-1",
        config={
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "instance_name": "test-proxy",
            "key_pair_name": "test-key",
            "security_group": "sg-12345",
            "ami_id": "ami-12345",
            "min_proxies": 1,
            "max_proxies": 5,
            "min_rotation_period_minutes": 60,
            "max_rotation_period_minutes": 120,
        },
        enabled=True,
    )


@pytest.fixture
def sample_credential() -> Credential:
    """Create a sample AWS credential for testing."""
    return Credential(
        id="test-credential-1",
        name="Test AWS Credential",
        type=CredentialType.AWS,
        project_id="test-project-1",
        config={"access_key": "test", "secret_key": "test"},
    )


@pytest.fixture
def sample_proxy() -> Proxy:
    """Create a sample proxy for testing."""
    return Proxy(
        id="test-proxy-1",
        host="1.2.3.4",
        port=8080,
        protocol=ProxyProtocol.HTTP,
        connector_id="test-connector-1",
        status=ProxyStatus.HEALTHY,
        metadata={"instance_id": "i-12345"},
    )


class TestAutoScalerInit:
    """Tests for AutoScaler initialization."""

    def test_init(self, mock_data_provider: MagicMock) -> None:
        """Test AutoScaler initialization."""
        scaler = AutoScaler(mock_data_provider)
        assert scaler._data_provider == mock_data_provider
        assert scaler._running is False
        assert scaler._rotation_schedule == {}
        assert scaler._last_scale_action_at == {}
        assert scaler._last_demand_level == {}


class TestCalculateTargetCount:
    """Tests for _calculate_target_count method."""

    def test_low_demand_returns_min(self, auto_scaler: AutoScaler) -> None:
        """Test that LOW demand returns minimum proxy count."""
        result = auto_scaler._calculate_target_count(
            DemandLevel.LOW, min_proxies=2, max_proxies=10, current_count=5
        )
        assert result == 2

    def test_high_demand_returns_max(self, auto_scaler: AutoScaler) -> None:
        """Test that HIGH demand returns maximum proxy count."""
        result = auto_scaler._calculate_target_count(
            DemandLevel.HIGH, min_proxies=2, max_proxies=10, current_count=5
        )
        assert result == 10

    def test_medium_demand_returns_midpoint(self, auto_scaler: AutoScaler) -> None:
        """Test that MEDIUM demand returns midpoint."""
        # When current is below midpoint, return midpoint
        result = auto_scaler._calculate_target_count(
            DemandLevel.MEDIUM, min_proxies=2, max_proxies=10, current_count=3
        )
        assert result == 6  # midpoint of 2 and 10

    def test_medium_demand_returns_midpoint_even_if_above(
        self, auto_scaler: AutoScaler
    ) -> None:
        """Test that MEDIUM demand always targets midpoint (allows gradual step-down)."""
        result = auto_scaler._calculate_target_count(
            DemandLevel.MEDIUM, min_proxies=2, max_proxies=10, current_count=8
        )
        assert result == 6  # midpoint, not current count


class TestGetMaxStepSizes:
    """Tests for _get_max_step_sizes static method."""

    def test_small_pool(self) -> None:
        """Test step sizes for a small pool (range=4)."""
        up, down = AutoScaler._get_max_step_sizes(1, 5)
        assert up == 2   # ceil(4/3)
        assert down == 1  # ceil(4/6)

    def test_medium_pool(self) -> None:
        """Test step sizes for a medium pool (range=9)."""
        up, down = AutoScaler._get_max_step_sizes(1, 10)
        assert up == 3   # ceil(9/3)
        assert down == 2  # ceil(9/6)

    def test_large_pool(self) -> None:
        """Test step sizes for a large pool (range=18)."""
        up, down = AutoScaler._get_max_step_sizes(2, 20)
        assert up == 6   # ceil(18/3)
        assert down == 3  # ceil(18/6)

    def test_minimum_step_size(self) -> None:
        """Test that step sizes are at least 1."""
        up, down = AutoScaler._get_max_step_sizes(1, 2)
        assert up >= 1
        assert down >= 1

    def test_equal_min_max(self) -> None:
        """Test step sizes when min == max (range=0)."""
        up, down = AutoScaler._get_max_step_sizes(5, 5)
        assert up == 1  # max(1, ceil(0/3))
        assert down == 1  # max(1, ceil(0/6))


class TestScheduleRotation:
    """Tests for _schedule_rotation method."""

    def test_schedule_rotation_sets_time(
        self, auto_scaler: AutoScaler, sample_proxy: Proxy, sample_connector: Connector
    ) -> None:
        """Test that schedule_rotation sets a rotation time."""
        cloud_config = sample_connector.cloud_config
        assert cloud_config is not None
        auto_scaler._schedule_rotation(sample_proxy, cloud_config)

        assert sample_proxy.id in auto_scaler._rotation_schedule
        rotation_time = auto_scaler._rotation_schedule[sample_proxy.id]
        assert isinstance(rotation_time, datetime)
        assert "scheduled_rotation_at" in sample_proxy.metadata

    def test_schedule_rotation_within_bounds(
        self, auto_scaler: AutoScaler, sample_proxy: Proxy
    ) -> None:
        """Test that rotation time is within min/max bounds."""
        cloud_config = CloudConnectorConfig(
            min_rotation_period_minutes=60,
            max_rotation_period_minutes=120,
        )
        # Use naive datetime to match utc_now() which returns naive datetime
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        auto_scaler._schedule_rotation(sample_proxy, cloud_config)

        rotation_time = auto_scaler._rotation_schedule[sample_proxy.id]
        min_time = now + timedelta(minutes=60)
        max_time = now + timedelta(minutes=120)

        # Allow some tolerance for test execution time
        assert rotation_time >= min_time - timedelta(seconds=5)
        assert rotation_time <= max_time + timedelta(seconds=5)


class TestCheckConnectorScaling:
    """Tests for _check_connector_scaling method."""

    @pytest.mark.asyncio
    async def test_scale_up_when_below_min(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test scaling up when current count is below minimum."""
        mock_data_provider.get_active_proxies_for_connector.return_value = []
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once_with(sample_connector, sample_credential, 1)

    @pytest.mark.asyncio
    async def test_scale_down_when_above_target(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
        sample_proxy: Proxy,
    ) -> None:
        """Test scaling down when current count is above target."""
        # 3 proxies with LOW demand should scale down to min (1)
        # max_down = ceil(4/6) = 1, so only 1 proxy drained per cycle
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            # Delta is 3-1=2, but capped by max_down=1
            mock_scale_down.assert_called_once_with(sample_connector, 1)

    @pytest.mark.asyncio
    async def test_no_scaling_when_at_target(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test no scaling when current count equals target."""
        # 1 proxy with LOW demand should stay at 1 (min)
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
                await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
                mock_scale_up.assert_not_called()
                mock_scale_down.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_previous_demand_level(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that previous demand level is passed to demand tracker."""
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.MEDIUM
        )

        # First call: no previous level
        await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
        call_args = mock_data_provider.demand_tracker.get_demand_level.call_args
        assert call_args[0][2] is None  # previous_level

        # Second call: should pass MEDIUM as previous level
        await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
        call_args = mock_data_provider.demand_tracker.get_demand_level.call_args
        assert call_args[0][2] == DemandLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_incremental_scale_up_capped(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale-up delta is capped by max step size."""
        # min=1, max=5 → max_up = ceil(4/3) = 2
        # 1 proxy with HIGH demand → target=5, delta=4, capped to 2
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once_with(sample_connector, sample_credential, 2)


class TestScalingCooldown:
    """Tests for scaling cooldown behavior."""

    @pytest.mark.asyncio
    async def test_scaling_skipped_during_cooldown(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scaling is skipped when in cooldown period."""
        # Set a recent scale action
        auto_scaler._last_scale_action_at[sample_connector.id] = utc_now() - timedelta(seconds=60)

        mock_data_provider.get_active_proxies_for_connector.return_value = []
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_scaling_proceeds_after_cooldown_expires(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scaling proceeds after cooldown period expires."""
        # Set a scale action that happened more than SCALING_COOLDOWN_SECONDS ago
        auto_scaler._last_scale_action_at[sample_connector.id] = (
            utc_now() - timedelta(seconds=SCALING_COOLDOWN_SECONDS + 10)
        )

        mock_data_provider.get_active_proxies_for_connector.return_value = []
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            # Should scale up to min (1) since current is 0
            mock_scale_up.assert_called_once()

    def test_is_in_scaling_cooldown_no_history(
        self, auto_scaler: AutoScaler
    ) -> None:
        """Test cooldown returns False when no previous action."""
        assert auto_scaler._is_in_scaling_cooldown("unknown-connector") is False

    def test_is_in_scaling_cooldown_recent_action(
        self, auto_scaler: AutoScaler
    ) -> None:
        """Test cooldown returns True for recent action."""
        auto_scaler._last_scale_action_at["conn-1"] = utc_now() - timedelta(seconds=30)
        assert auto_scaler._is_in_scaling_cooldown("conn-1") is True

    def test_is_in_scaling_cooldown_expired(
        self, auto_scaler: AutoScaler
    ) -> None:
        """Test cooldown returns False when expired."""
        auto_scaler._last_scale_action_at["conn-1"] = (
            utc_now() - timedelta(seconds=SCALING_COOLDOWN_SECONDS + 1)
        )
        assert auto_scaler._is_in_scaling_cooldown("conn-1") is False


class TestRotationAwareness:
    """Tests for rotation-awareness: skip scaling when proxy is initializing."""

    @pytest.mark.asyncio
    async def test_scaling_skipped_when_initializing_proxy_exists(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scaling is skipped when an INITIALIZING proxy exists."""
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
            Proxy(id="proxy-2", host="1.2.3.5", port=8080, status=ProxyStatus.INITIALIZING, connector_id="test-connector-1"),
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_scaling_proceeds_when_no_initializing_proxy(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scaling proceeds when all proxies are past initialization."""
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once()


class TestRecentActivityGating:
    """Tests for scale-up gated on recent activity."""

    @pytest.mark.asyncio
    async def test_scale_up_blocked_without_recent_activity(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale-up is blocked when there is no recent activity."""
        # 1 proxy, HIGH demand → target=5 → wants to scale up
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )
        # No recent activity (stale burst)
        mock_data_provider.demand_tracker.has_recent_activity = AsyncMock(
            return_value=False
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_scale_up_proceeds_with_recent_activity(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale-up proceeds when there is recent activity."""
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )
        mock_data_provider.demand_tracker.has_recent_activity = AsyncMock(
            return_value=True
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once()

    @pytest.mark.asyncio
    async def test_scale_down_not_gated_by_recent_activity(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale-down is NOT gated by recent activity."""
        # 3 proxies with LOW demand → target=1 → scale down
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )
        # Even with no recent activity, scale-down should proceed
        mock_data_provider.demand_tracker.has_recent_activity = AsyncMock(
            return_value=False
        )

        with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_down.assert_called_once()


class TestScaleDown:
    """Tests for _scale_down method."""

    @pytest.mark.asyncio
    async def test_scale_down_starts_draining(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
    ) -> None:
        """Test that scale_down emits draining signals for proxies."""
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies

        with patch.object(signals.proxy_draining_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._scale_down(sample_connector, 2)
            assert mock_signal.call_count == 2

    @pytest.mark.asyncio
    async def test_scale_down_prefers_unhealthy(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
    ) -> None:
        """Test that scale_down prefers unhealthy proxies."""
        proxies = [
            Proxy(id="healthy-1", host="1.2.3.1", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
            Proxy(id="unhealthy-1", host="1.2.3.2", port=8080, status=ProxyStatus.UNHEALTHY, connector_id="test-connector-1"),
            Proxy(id="healthy-2", host="1.2.3.3", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies

        with patch.object(signals.proxy_draining_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._scale_down(sample_connector, 1)
            # Should drain the unhealthy proxy first
            mock_signal.assert_called_once()
            call_kwargs = mock_signal.call_args[1]
            assert call_kwargs["proxy_id"] == "unhealthy-1"

    @pytest.mark.asyncio
    async def test_scale_down_records_cooldown(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
    ) -> None:
        """Test that scale_down records cooldown timestamp."""
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.1", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies

        with patch.object(signals.proxy_draining_requested, "send_async", new_callable=AsyncMock):
            await auto_scaler._scale_down(sample_connector, 1)

        assert sample_connector.id in auto_scaler._last_scale_action_at


class TestStopMethod:
    """Tests for stop method."""

    def test_stop_sets_running_false(self, auto_scaler: AutoScaler) -> None:
        """Test that stop sets _running to False."""
        auto_scaler._running = True
        auto_scaler.stop()
        assert auto_scaler._running is False


class TestHandleDrainingProxy:
    """Tests for _handle_draining_proxy method."""

    @pytest.mark.asyncio
    async def test_first_check_records_initial_stats(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test that first check records initial traffic stats in metadata."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=1000,
            bytes_received=5000,
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._handle_draining_proxy(proxy)

            # Should record initial stats, not mark as terminating
            assert proxy.metadata["draining_prev_bytes_sent"] == 1000
            assert proxy.metadata["draining_prev_bytes_received"] == 5000
            mock_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_traffic_change_marks_terminating(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test that no traffic change between checks marks proxy as terminating."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=1000,
            bytes_received=5000,
            metadata={
                "draining_prev_bytes_sent": 1000,
                "draining_prev_bytes_received": 5000,
            },
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._handle_draining_proxy(proxy)

            # Should mark as terminating since no traffic change
            mock_signal.assert_called_once()
            call_kwargs = mock_signal.call_args[1]
            assert call_kwargs["proxy_id"] == "draining-proxy"

    @pytest.mark.asyncio
    async def test_traffic_still_flowing_updates_stats(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test that ongoing traffic updates previous stats for next check."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=2000,  # Increased from 1000
            bytes_received=10000,  # Increased from 5000
            metadata={
                "draining_prev_bytes_sent": 1000,
                "draining_prev_bytes_received": 5000,
            },
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._handle_draining_proxy(proxy)

            # Should update stats, not mark as terminating
            assert proxy.metadata["draining_prev_bytes_sent"] == 2000
            assert proxy.metadata["draining_prev_bytes_received"] == 10000
            mock_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_bytes_sent_changed(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test that change in only bytes_sent keeps proxy draining."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=2000,  # Changed
            bytes_received=5000,  # Same
            metadata={
                "draining_prev_bytes_sent": 1000,
                "draining_prev_bytes_received": 5000,
            },
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._handle_draining_proxy(proxy)

            # Should not mark as terminating
            mock_signal.assert_not_called()
            assert proxy.metadata["draining_prev_bytes_sent"] == 2000

    @pytest.mark.asyncio
    async def test_only_bytes_received_changed(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test that change in only bytes_received keeps proxy draining."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=1000,  # Same
            bytes_received=10000,  # Changed
            metadata={
                "draining_prev_bytes_sent": 1000,
                "draining_prev_bytes_received": 5000,
            },
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._handle_draining_proxy(proxy)

            # Should not mark as terminating
            mock_signal.assert_not_called()
            assert proxy.metadata["draining_prev_bytes_received"] == 10000

    @pytest.mark.asyncio
    async def test_zero_traffic_from_start(
        self,
        auto_scaler: AutoScaler,
    ) -> None:
        """Test proxy with zero traffic from start terminates after two checks."""
        proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.4",
            port=8080,
            connector_id="test-connector-1",
            status=ProxyStatus.DRAINING,
            bytes_sent=0,
            bytes_received=0,
        )

        with patch.object(signals.proxy_terminating_requested, "send_async", new_callable=AsyncMock) as mock_signal:
            # First check - records initial stats
            await auto_scaler._handle_draining_proxy(proxy)
            assert proxy.metadata["draining_prev_bytes_sent"] == 0
            assert proxy.metadata["draining_prev_bytes_received"] == 0
            mock_signal.assert_not_called()

            # Second check - no change, should terminate
            await auto_scaler._handle_draining_proxy(proxy)
            mock_signal.assert_called_once()
            call_kwargs = mock_signal.call_args[1]
            assert call_kwargs["proxy_id"] == "draining-proxy"


class TestShouldSkipScaling:
    """Tests for _should_skip_scaling method (error backoff logic)."""

    def test_returns_false_when_no_error(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that scaling is not skipped when there's no error."""
        sample_connector.last_error = None
        sample_connector.consecutive_errors = 0
        assert auto_scaler._should_skip_scaling(sample_connector) is False

    def test_returns_false_when_consecutive_errors_is_zero(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that scaling is not skipped when consecutive_errors is 0."""
        sample_connector.last_error = "Some old error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)
        sample_connector.consecutive_errors = 0
        assert auto_scaler._should_skip_scaling(sample_connector) is False

    def test_returns_true_when_within_backoff_period(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that scaling is skipped when within backoff period."""
        # 1 consecutive error = 2^1 = 2 minutes backoff
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)  # 1 min ago
        sample_connector.consecutive_errors = 1
        assert auto_scaler._should_skip_scaling(sample_connector) is True

    def test_returns_false_when_backoff_period_passed(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that scaling is not skipped when backoff period has passed."""
        # 1 consecutive error = 2^1 = 2 minutes backoff
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=3)  # 3 min ago
        sample_connector.consecutive_errors = 1
        assert auto_scaler._should_skip_scaling(sample_connector) is False

    def test_exponential_backoff_calculation(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that backoff is calculated exponentially (2^n minutes)."""
        sample_connector.last_error = "Cloud provider error"

        # 2 consecutive errors = 2^2 = 4 minutes backoff
        sample_connector.last_error_at = utc_now() - timedelta(minutes=3)
        sample_connector.consecutive_errors = 2
        assert auto_scaler._should_skip_scaling(sample_connector) is True  # 3 < 4

        sample_connector.last_error_at = utc_now() - timedelta(minutes=5)
        assert auto_scaler._should_skip_scaling(sample_connector) is False  # 5 > 4

        # 3 consecutive errors = 2^3 = 8 minutes backoff
        sample_connector.last_error_at = utc_now() - timedelta(minutes=7)
        sample_connector.consecutive_errors = 3
        assert auto_scaler._should_skip_scaling(sample_connector) is True  # 7 < 8

        sample_connector.last_error_at = utc_now() - timedelta(minutes=9)
        assert auto_scaler._should_skip_scaling(sample_connector) is False  # 9 > 8

    def test_backoff_caps_at_max(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that backoff is capped at MAX_ERROR_BACKOFF_MINUTES."""
        sample_connector.last_error = "Cloud provider error"
        # 10 consecutive errors would be 2^10 = 1024 minutes, but should cap at 30
        sample_connector.consecutive_errors = 10

        # Should still be in backoff at 29 minutes
        sample_connector.last_error_at = utc_now() - timedelta(minutes=29)
        assert auto_scaler._should_skip_scaling(sample_connector) is True

        # Should not be in backoff after 31 minutes
        sample_connector.last_error_at = utc_now() - timedelta(minutes=31)
        assert auto_scaler._should_skip_scaling(sample_connector) is False

    def test_returns_false_when_last_error_at_is_none(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that scaling is not skipped when last_error_at is None."""
        sample_connector.last_error = "Some error"
        sample_connector.last_error_at = None
        sample_connector.consecutive_errors = 5
        assert auto_scaler._should_skip_scaling(sample_connector) is False


class TestRecordConnectorError:
    """Tests for _record_connector_error method."""

    @pytest.mark.asyncio
    async def test_emits_signal_with_correct_args(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that _record_connector_error emits connector_error_updated signal."""
        sample_connector.consecutive_errors = 0
        error_msg = "Failed to create instance"

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._record_connector_error(sample_connector, error_msg)

            mock_signal.assert_called_once()
            call_kwargs = mock_signal.call_args[1]
            assert call_kwargs["connector_id"] == sample_connector.id
            assert call_kwargs["error"] == error_msg
            assert call_kwargs["consecutive_errors"] == 1

    @pytest.mark.asyncio
    async def test_increments_consecutive_errors(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that consecutive_errors is incremented."""
        sample_connector.consecutive_errors = 3

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock):
            await auto_scaler._record_connector_error(sample_connector, "error")

        assert sample_connector.consecutive_errors == 4

    @pytest.mark.asyncio
    async def test_updates_local_connector_state(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that local connector state is updated."""
        sample_connector.last_error = None
        sample_connector.last_error_at = None
        sample_connector.consecutive_errors = 0
        error_msg = "Test error"

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock):
            await auto_scaler._record_connector_error(sample_connector, error_msg)

        assert sample_connector.last_error == error_msg
        assert sample_connector.last_error_at is not None
        assert sample_connector.consecutive_errors == 1


class TestClearConnectorError:
    """Tests for _clear_connector_error method."""

    @pytest.mark.asyncio
    async def test_does_nothing_when_no_error(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that _clear_connector_error does nothing when no error exists."""
        sample_connector.consecutive_errors = 0

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._clear_connector_error(sample_connector)
            mock_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_signal_when_error_exists(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that signal is emitted with error=None and consecutive_errors=0."""
        sample_connector.last_error = "Previous error"
        sample_connector.consecutive_errors = 5

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock) as mock_signal:
            await auto_scaler._clear_connector_error(sample_connector)

            mock_signal.assert_called_once()
            call_kwargs = mock_signal.call_args[1]
            assert call_kwargs["connector_id"] == sample_connector.id
            assert call_kwargs["error"] is None
            assert call_kwargs["consecutive_errors"] == 0

    @pytest.mark.asyncio
    async def test_clears_local_connector_state(
        self, auto_scaler: AutoScaler, sample_connector: Connector
    ) -> None:
        """Test that local connector state is cleared."""
        sample_connector.last_error = "Some error"
        sample_connector.last_error_at = utc_now()
        sample_connector.consecutive_errors = 3

        with patch.object(signals.connector_error_updated, "send_async", new_callable=AsyncMock):
            await auto_scaler._clear_connector_error(sample_connector)

        assert sample_connector.last_error is None
        assert sample_connector.last_error_at is None
        assert sample_connector.consecutive_errors == 0


class TestScalingWithBackoff:
    """Tests for scaling operations respecting error backoff."""

    @pytest.mark.asyncio
    async def test_scale_up_skipped_when_in_backoff(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale up is skipped when connector is in error backoff."""
        # Set up connector with recent error
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)
        sample_connector.consecutive_errors = 2  # 4 minute backoff

        mock_data_provider.get_active_proxies_for_connector.return_value = []
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.HIGH
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_scale_down_skipped_when_in_backoff(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scale down is skipped when connector is in error backoff."""
        # Set up connector with recent error
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)
        sample_connector.consecutive_errors = 2  # 4 minute backoff

        # 3 proxies with LOW demand should scale down, but backoff prevents it
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_data_provider.get_active_proxies_for_connector.return_value = proxies
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_down.assert_not_called()

    @pytest.mark.asyncio
    async def test_scaling_proceeds_after_backoff_expires(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test that scaling proceeds after backoff period expires."""
        # Set up connector with old error (backoff expired)
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=10)
        sample_connector.consecutive_errors = 2  # 4 minute backoff, but 10 min passed

        mock_data_provider.get_active_proxies_for_connector.return_value = []
        mock_data_provider.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once()


class TestRotationWithBackoff:
    """Tests for rotation operations respecting error backoff."""

    @pytest.mark.asyncio
    async def test_rotation_skipped_when_in_backoff(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
        sample_proxy: Proxy,
    ) -> None:
        """Test that rotation is skipped when connector is in error backoff."""
        # Set up connector with recent error
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)
        sample_connector.consecutive_errors = 2  # 4 minute backoff

        # Set up proxy with rotation due
        sample_proxy.status = ProxyStatus.HEALTHY
        auto_scaler._rotation_schedule[sample_proxy.id] = utc_now() - timedelta(minutes=5)

        mock_data_provider.get_proxies_for_connector.return_value = [sample_proxy]
        mock_data_provider.get_credential.return_value = sample_credential
        mock_data_provider.connectors = [sample_connector]

        with patch.object(auto_scaler, "_start_rotation", new_callable=AsyncMock) as mock_rotation:
            await auto_scaler._check_connector_rotation(sample_connector, sample_credential)
            mock_rotation.assert_not_called()

    @pytest.mark.asyncio
    async def test_termination_skipped_when_in_backoff(
        self,
        auto_scaler: AutoScaler,
        mock_data_provider: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
        sample_proxy: Proxy,
    ) -> None:
        """Test that termination is skipped when connector is in error backoff."""
        # Set up connector with recent error
        sample_connector.last_error = "Cloud provider error"
        sample_connector.last_error_at = utc_now() - timedelta(minutes=1)
        sample_connector.consecutive_errors = 2  # 4 minute backoff

        # Set up proxy in terminating state
        sample_proxy.status = ProxyStatus.TERMINATING

        mock_data_provider.get_proxies_for_connector.return_value = [sample_proxy]
        mock_data_provider.get_credential.return_value = sample_credential
        mock_data_provider.connectors = [sample_connector]

        with patch.object(auto_scaler, "_handle_terminating_proxy", new_callable=AsyncMock) as mock_terminate:
            await auto_scaler._check_connector_rotation(sample_connector, sample_credential)
            mock_terminate.assert_not_called()
