"""Tests for AutoScaler class."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.auto_scaler import AutoScaler, CHECK_INTERVAL_SECONDS
from api.core.demand_tracker import DemandLevel
from api.models.connector import CloudConnectorConfig, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


@pytest.fixture
def mock_proxy_manager() -> MagicMock:
    """Create a mock ProxyManager for testing."""
    manager = MagicMock()
    manager.connectors = []
    manager.get_credential = MagicMock(return_value=None)
    manager.get_active_proxies_for_connector = MagicMock(return_value=[])
    manager.get_proxies_for_connector = MagicMock(return_value=[])
    manager.demand_tracker = MagicMock()
    manager.demand_tracker.get_demand_level = AsyncMock(return_value=DemandLevel.LOW)
    manager.add_proxy = AsyncMock()
    manager.remove_proxy = AsyncMock(return_value=True)
    manager.start_proxy_draining = AsyncMock()
    manager.mark_proxy_terminating = AsyncMock()
    return manager


@pytest.fixture
def auto_scaler(mock_proxy_manager: MagicMock) -> AutoScaler:
    """Create an AutoScaler instance for testing."""
    return AutoScaler(mock_proxy_manager)


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

    def test_init(self, mock_proxy_manager: MagicMock) -> None:
        """Test AutoScaler initialization."""
        scaler = AutoScaler(mock_proxy_manager)
        assert scaler._proxy_manager == mock_proxy_manager
        assert scaler._running is False
        assert scaler._rotation_schedule == {}


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
        """Test that MEDIUM demand returns midpoint or current count."""
        # When current is below midpoint, return midpoint
        result = auto_scaler._calculate_target_count(
            DemandLevel.MEDIUM, min_proxies=2, max_proxies=10, current_count=3
        )
        assert result == 6  # midpoint of 2 and 10

    def test_medium_demand_keeps_current_if_above_midpoint(
        self, auto_scaler: AutoScaler
    ) -> None:
        """Test that MEDIUM demand keeps current count if above midpoint."""
        result = auto_scaler._calculate_target_count(
            DemandLevel.MEDIUM, min_proxies=2, max_proxies=10, current_count=8
        )
        assert result == 8  # keeps current since above midpoint


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
        mock_proxy_manager: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test scaling up when current count is below minimum."""
        mock_proxy_manager.get_active_proxies_for_connector.return_value = []
        mock_proxy_manager.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_up.assert_called_once_with(sample_connector, sample_credential, 1)

    @pytest.mark.asyncio
    async def test_scale_down_when_above_target(
        self,
        auto_scaler: AutoScaler,
        mock_proxy_manager: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
        sample_proxy: Proxy,
    ) -> None:
        """Test scaling down when current count is above target."""
        # 3 proxies with LOW demand should scale down to min (1)
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_proxy_manager.get_active_proxies_for_connector.return_value = proxies
        mock_proxy_manager.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
            await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
            mock_scale_down.assert_called_once_with(sample_connector, 2)  # 3 - 1 = 2

    @pytest.mark.asyncio
    async def test_no_scaling_when_at_target(
        self,
        auto_scaler: AutoScaler,
        mock_proxy_manager: MagicMock,
        sample_connector: Connector,
        sample_credential: Credential,
    ) -> None:
        """Test no scaling when current count equals target."""
        # 1 proxy with LOW demand should stay at 1 (min)
        proxies = [
            Proxy(id="proxy-1", host="1.2.3.4", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
        ]
        mock_proxy_manager.get_active_proxies_for_connector.return_value = proxies
        mock_proxy_manager.demand_tracker.get_demand_level = AsyncMock(
            return_value=DemandLevel.LOW
        )

        with patch.object(auto_scaler, "_scale_up", new_callable=AsyncMock) as mock_scale_up:
            with patch.object(auto_scaler, "_scale_down", new_callable=AsyncMock) as mock_scale_down:
                await auto_scaler._check_connector_scaling(sample_connector, sample_credential)
                mock_scale_up.assert_not_called()
                mock_scale_down.assert_not_called()


class TestScaleDown:
    """Tests for _scale_down method."""

    @pytest.mark.asyncio
    async def test_scale_down_starts_draining(
        self,
        auto_scaler: AutoScaler,
        mock_proxy_manager: MagicMock,
        sample_connector: Connector,
    ) -> None:
        """Test that scale_down starts draining proxies."""
        proxies = [
            Proxy(id=f"proxy-{i}", host=f"1.2.3.{i}", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1")
            for i in range(3)
        ]
        mock_proxy_manager.get_active_proxies_for_connector.return_value = proxies

        await auto_scaler._scale_down(sample_connector, 2)

        assert mock_proxy_manager.start_proxy_draining.call_count == 2

    @pytest.mark.asyncio
    async def test_scale_down_prefers_unhealthy(
        self,
        auto_scaler: AutoScaler,
        mock_proxy_manager: MagicMock,
        sample_connector: Connector,
    ) -> None:
        """Test that scale_down prefers unhealthy proxies."""
        proxies = [
            Proxy(id="healthy-1", host="1.2.3.1", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
            Proxy(id="unhealthy-1", host="1.2.3.2", port=8080, status=ProxyStatus.UNHEALTHY, connector_id="test-connector-1"),
            Proxy(id="healthy-2", host="1.2.3.3", port=8080, status=ProxyStatus.HEALTHY, connector_id="test-connector-1"),
        ]
        mock_proxy_manager.get_active_proxies_for_connector.return_value = proxies

        await auto_scaler._scale_down(sample_connector, 1)

        # Should drain the unhealthy proxy first
        mock_proxy_manager.start_proxy_draining.assert_called_once_with("unhealthy-1")


class TestStopMethod:
    """Tests for stop method."""

    def test_stop_sets_running_false(self, auto_scaler: AutoScaler) -> None:
        """Test that stop sets _running to False."""
        auto_scaler._running = True
        auto_scaler.stop()
        assert auto_scaler._running is False

