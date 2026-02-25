# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for HealthChecker class."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core import utc_now
from api.core.health_checker import (
    INITIALIZATION_GRACE_PERIOD,
    HealthChecker,
)
from api.core.signals import health_check_completed
from api.models.proxy import Proxy, ProxyStatus


@pytest.fixture
def mock_proxy_data_provider() -> MagicMock:
    """Create a mock ProxyDataProvider for testing."""
    provider = MagicMock()
    provider.proxies = []
    provider.is_connector_enabled = MagicMock(return_value=True)
    return provider


@pytest.fixture
def health_checker(mock_proxy_data_provider: MagicMock) -> HealthChecker:
    """Create a HealthChecker instance for testing."""
    return HealthChecker(mock_proxy_data_provider)


@pytest.fixture
def initializing_proxy() -> Proxy:
    """Create a proxy in INITIALIZING status."""
    return Proxy(
        id="init-proxy-1",
        host="1.2.3.4",
        port=3128,
        connector_id="test-connector",
        status=ProxyStatus.INITIALIZING,
        created_at=utc_now(),
        consecutive_failures=0,
    )


@pytest.fixture
def old_initializing_proxy() -> Proxy:
    """Create a proxy in INITIALIZING status that's past the grace period."""
    return Proxy(
        id="old-init-proxy-1",
        host="1.2.3.5",
        port=3128,
        connector_id="test-connector",
        status=ProxyStatus.INITIALIZING,
        created_at=utc_now() - timedelta(minutes=6),  # Past 5-minute grace period
        consecutive_failures=0,
    )


@pytest.fixture
def healthy_proxy() -> Proxy:
    """Create a healthy proxy."""
    return Proxy(
        id="healthy-proxy-1",
        host="1.2.3.6",
        port=3128,
        connector_id="test-connector",
        status=ProxyStatus.HEALTHY,
        created_at=utc_now() - timedelta(hours=1),
        consecutive_failures=0,
    )


class TestInitializationGracePeriod:
    """Tests for initialization grace period behavior."""

    def test_grace_period_is_five_minutes(self) -> None:
        """Test that the grace period constant is 5 minutes."""
        assert timedelta(minutes=5) == INITIALIZATION_GRACE_PERIOD

    def test_is_within_grace_period_new_proxy(
        self, health_checker: HealthChecker, initializing_proxy: Proxy
    ) -> None:
        """Test that a newly created initializing proxy is within grace period."""
        assert health_checker._is_within_initialization_grace_period(initializing_proxy)

    def test_is_within_grace_period_old_proxy(
        self, health_checker: HealthChecker, old_initializing_proxy: Proxy
    ) -> None:
        """Test that an old initializing proxy is not within grace period."""
        assert not health_checker._is_within_initialization_grace_period(old_initializing_proxy)

    def test_is_within_grace_period_healthy_proxy(
        self, health_checker: HealthChecker, healthy_proxy: Proxy
    ) -> None:
        """Test that a healthy proxy is not within grace period (wrong status)."""
        assert not health_checker._is_within_initialization_grace_period(healthy_proxy)

    @pytest.mark.asyncio
    async def test_handle_check_failure_keeps_initializing_within_grace_period(
        self,
        health_checker: HealthChecker,
        initializing_proxy: Proxy,
    ) -> None:
        """Test that failures during grace period keep status as INITIALIZING."""
        received_signals: list[dict] = []

        async def signal_receiver(sender, **kwargs):
            received_signals.append(kwargs)

        health_check_completed.connect(signal_receiver)
        try:
            await health_checker._handle_check_failure(initializing_proxy, "Timeout")

            assert len(received_signals) == 1
            assert received_signals[0]["proxy_id"] == initializing_proxy.id
            assert received_signals[0]["status"] == ProxyStatus.INITIALIZING
            assert received_signals[0]["consecutive_failures"] == 1
        finally:
            health_check_completed.disconnect(signal_receiver)

    @pytest.mark.asyncio
    async def test_handle_check_failure_degrades_after_grace_period(
        self,
        health_checker: HealthChecker,
        old_initializing_proxy: Proxy,
    ) -> None:
        """Test that failures after grace period mark proxy as DEGRADED."""
        received_signals: list[dict] = []

        async def signal_receiver(sender, **kwargs):
            received_signals.append(kwargs)

        health_check_completed.connect(signal_receiver)
        try:
            await health_checker._handle_check_failure(old_initializing_proxy, "Timeout")

            assert len(received_signals) == 1
            assert received_signals[0]["proxy_id"] == old_initializing_proxy.id
            assert received_signals[0]["status"] == ProxyStatus.DEGRADED
            assert received_signals[0]["consecutive_failures"] == 1
        finally:
            health_check_completed.disconnect(signal_receiver)

    @pytest.mark.asyncio
    async def test_handle_check_failure_marks_unhealthy_after_three_failures(
        self,
        health_checker: HealthChecker,
        old_initializing_proxy: Proxy,
    ) -> None:
        """Test that 3+ failures after grace period mark proxy as UNHEALTHY."""
        old_initializing_proxy.consecutive_failures = 2  # Will become 3

        received_signals: list[dict] = []

        async def signal_receiver(sender, **kwargs):
            received_signals.append(kwargs)

        health_check_completed.connect(signal_receiver)
        try:
            await health_checker._handle_check_failure(old_initializing_proxy, "Timeout")

            assert len(received_signals) == 1
            assert received_signals[0]["proxy_id"] == old_initializing_proxy.id
            assert received_signals[0]["status"] == ProxyStatus.UNHEALTHY
            assert received_signals[0]["consecutive_failures"] == 3
        finally:
            health_check_completed.disconnect(signal_receiver)

    @pytest.mark.asyncio
    async def test_failures_tracked_during_grace_period(
        self,
        health_checker: HealthChecker,
        initializing_proxy: Proxy,
    ) -> None:
        """Test that consecutive failures are tracked even during grace period."""
        initializing_proxy.consecutive_failures = 5

        received_signals: list[dict] = []

        async def signal_receiver(sender, **kwargs):
            received_signals.append(kwargs)

        health_check_completed.connect(signal_receiver)
        try:
            await health_checker._handle_check_failure(initializing_proxy, "Connection refused")

            # Should still be INITIALIZING but with incremented failure count
            assert len(received_signals) == 1
            assert received_signals[0]["proxy_id"] == initializing_proxy.id
            assert received_signals[0]["status"] == ProxyStatus.INITIALIZING
            assert received_signals[0]["consecutive_failures"] == 6
        finally:
            health_check_completed.disconnect(signal_receiver)


class TestCheckAllProxies:
    """Tests for _check_all_proxies filtering behavior."""

    @pytest.mark.asyncio
    async def test_initializing_proxies_are_health_checked(
        self,
        health_checker: HealthChecker,
        mock_proxy_data_provider: MagicMock,
        initializing_proxy: Proxy,
    ) -> None:
        """Test that INITIALIZING proxies are included in health checks."""
        mock_proxy_data_provider.proxies = [initializing_proxy]

        with patch.object(
            health_checker, "_check_proxy", new_callable=AsyncMock
        ) as mock_check:
            await health_checker._check_all_proxies()
            mock_check.assert_called_once_with(initializing_proxy)

    @pytest.mark.asyncio
    async def test_draining_proxies_are_skipped(
        self,
        health_checker: HealthChecker,
        mock_proxy_data_provider: MagicMock,
    ) -> None:
        """Test that DRAINING proxies are not health checked."""
        draining_proxy = Proxy(
            id="draining-proxy",
            host="1.2.3.7",
            port=3128,
            connector_id="test-connector",
            status=ProxyStatus.DRAINING,
        )
        mock_proxy_data_provider.proxies = [draining_proxy]

        with patch.object(
            health_checker, "_check_proxy", new_callable=AsyncMock
        ) as mock_check:
            await health_checker._check_all_proxies()
            mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminating_proxies_are_skipped(
        self,
        health_checker: HealthChecker,
        mock_proxy_data_provider: MagicMock,
    ) -> None:
        """Test that TERMINATING proxies are not health checked."""
        terminating_proxy = Proxy(
            id="terminating-proxy",
            host="1.2.3.8",
            port=3128,
            connector_id="test-connector",
            status=ProxyStatus.TERMINATING,
        )
        mock_proxy_data_provider.proxies = [terminating_proxy]

        with patch.object(
            health_checker, "_check_proxy", new_callable=AsyncMock
        ) as mock_check:
            await health_checker._check_all_proxies()
            mock_check.assert_not_called()

