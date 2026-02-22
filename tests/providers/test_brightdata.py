# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for BrightData proxy provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models.connector import BrightDataProxyType, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import ProxyProtocol, ProxyStatus
from api.providers.brightdata import (
    BRIGHTDATA_HOST,
    BRIGHTDATA_PORT,
    PORT_BASED_TYPES,
    SESSION_BASED_TYPES,
    BrightDataProvider,
    _generate_session_id,
)


@pytest.fixture
def residential_credential() -> Credential:
    """Create a sample BrightData residential credential."""
    return Credential(
        id="test-brightdata-cred",
        name="Test BrightData Credential",
        type=CredentialType.BRIGHTDATA,
        project_id="test-project",
        config={
            "token": "test-token-123",
            "customer_id": "test-customer",
        },
    )


@pytest.fixture
def datacenter_credential() -> Credential:
    """Create a sample BrightData datacenter credential."""
    return Credential(
        id="test-brightdata-dc-cred",
        name="Test BrightData DC Credential",
        type=CredentialType.BRIGHTDATA,
        project_id="test-project",
        config={
            "token": "test-token-456",
            "customer_id": "test-customer-dc",
        },
    )


@pytest.fixture
def residential_connector() -> Connector:
    """Create a sample BrightData residential connector."""
    return Connector(
        id="test-brightdata-connector",
        name="Test BrightData Connector",
        credential_id="test-brightdata-cred",
        credential_type=CredentialType.BRIGHTDATA,
        project_id="test-project",
        config={
            "zone_name": "test_zone_res",
            "zone_password": "zone_pass_123",
            "proxy_type": BrightDataProxyType.RESIDENTIAL.value,
            "num_proxies": 3,
            "country_code": "US",
        },
        enabled=True,
    )


@pytest.fixture
def datacenter_connector() -> Connector:
    """Create a sample BrightData datacenter connector."""
    return Connector(
        id="test-brightdata-dc-connector",
        name="Test BrightData DC Connector",
        credential_id="test-brightdata-dc-cred",
        credential_type=CredentialType.BRIGHTDATA,
        project_id="test-project",
        config={
            "zone_name": "test_zone_dc",
            "zone_password": "zone_pass_456",
            "proxy_type": BrightDataProxyType.DATACENTER.value,
            "num_proxies": 2,
        },
        enabled=True,
    )


class TestGenerateSessionId:
    """Tests for _generate_session_id function."""

    def test_returns_string(self) -> None:
        """Test that session ID is a string."""
        session_id = _generate_session_id()
        assert isinstance(session_id, str)

    def test_starts_with_glob(self) -> None:
        """Test that session ID starts with 'glob_'."""
        session_id = _generate_session_id()
        assert session_id.startswith("glob_")

    def test_length_is_17(self) -> None:
        """Test that session ID is 17 characters (glob_ + 12 chars)."""
        session_id = _generate_session_id()
        assert len(session_id) == 17

    def test_contains_only_lowercase_and_digits(self) -> None:
        """Test that session ID contains only lowercase letters and digits after prefix."""
        session_id = _generate_session_id()
        random_part = session_id[5:]  # Skip 'glob_'
        assert random_part.isalnum()
        assert random_part.islower() or random_part.isdigit()

    def test_generates_unique_ids(self) -> None:
        """Test that multiple calls generate different IDs."""
        ids = [_generate_session_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestBrightDataProviderInit:
    """Tests for BrightDataProvider initialization."""

    def test_init_residential(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test BrightDataProvider initializes correctly for residential type."""
        provider = BrightDataProvider(residential_connector, residential_credential)

        assert provider.connector == residential_connector
        assert provider.credential == residential_credential
        assert provider._token == "test-token-123"
        assert provider._customer_id == "test-customer"
        assert provider._zone_name == "test_zone_res"
        assert provider._zone_password == "zone_pass_123"
        assert provider._proxy_type == BrightDataProxyType.RESIDENTIAL

    def test_init_datacenter(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test BrightDataProvider initializes correctly for datacenter type."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        assert provider._proxy_type == BrightDataProxyType.DATACENTER




    def test_init_without_credential_raises(
        self, residential_connector: Connector
    ) -> None:
        """Test that BrightDataProvider raises without credentials."""
        with pytest.raises(ValueError, match="requires credentials"):
            BrightDataProvider(residential_connector, None)  # type: ignore

    def test_is_session_based_residential(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test is_session_based returns True for residential."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        assert provider.is_session_based() is True

    def test_is_session_based_mobile(
        self, residential_credential: Credential
    ) -> None:
        """Test is_session_based returns True for mobile."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "mobile_zone",
                "zone_password": "pass",
                "proxy_type": BrightDataProxyType.MOBILE.value,
                "num_proxies": 1,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, residential_credential)
        assert provider.is_session_based() is True

    def test_is_session_based_datacenter(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test is_session_based returns False for datacenter."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        assert provider.is_session_based() is False

    def test_is_session_based_isp(
        self, datacenter_credential: Credential
    ) -> None:
        """Test is_session_based returns False for ISP."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "isp_zone",
                "zone_password": "pass",
                "proxy_type": BrightDataProxyType.ISP.value,
                "num_proxies": 1,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, datacenter_credential)
        assert provider.is_session_based() is False


class TestBrightDataProviderBuildUsername:
    """Tests for BrightDataProvider._build_username method."""

    def test_residential_with_country_and_session(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test username format for residential with country and session."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        username = provider._build_username(session_id="glob_abc123xyz456")

        assert username == "brd-customer-test-customer-zone-test_zone_res-session-glob_abc123xyz456-country-us"

    def test_residential_without_country(
        self, residential_credential: Credential
    ) -> None:
        """Test username format for residential without country code."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "res_zone",
                "zone_password": "pass",
                "proxy_type": BrightDataProxyType.RESIDENTIAL.value,
                "num_proxies": 1,
                "country_code": "",
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, residential_credential)
        username = provider._build_username(session_id="glob_xyz789")

        assert username == "brd-customer-test-customer-zone-res_zone-session-glob_xyz789"

    def test_datacenter_with_hashed_ip(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test username format for datacenter with hashed IP."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        username = provider._build_username(hashed_ip="hashed_ip_123")

        assert username == "brd-customer-test-customer-dc-zone-test_zone_dc-ip-hashed_ip_123"

    def test_datacenter_with_country_and_hashed_ip(
        self, datacenter_credential: Credential
    ) -> None:
        """Test username format for datacenter with country and hashed IP."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "dc_zone",
                "zone_password": "pass",
                "proxy_type": BrightDataProxyType.DATACENTER.value,
                "num_proxies": 1,
                "country_code": "GB",
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, datacenter_credential)
        username = provider._build_username(hashed_ip="hashed_ip_456")

        assert username == "brd-customer-test-customer-dc-zone-dc_zone-ip-hashed_ip_456-country-gb"

    def test_isp_without_country(
        self, datacenter_credential: Credential
    ) -> None:
        """Test username format for ISP without country."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "isp_zone",
                "zone_password": "pass",
                "proxy_type": BrightDataProxyType.ISP.value,
                "num_proxies": 1,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, datacenter_credential)
        username = provider._build_username(hashed_ip="hashed_ip_789")

        assert username == "brd-customer-test-customer-dc-zone-isp_zone-ip-hashed_ip_789"



class TestBrightDataProviderGetProxies:
    """Tests for BrightDataProvider.get_proxies method."""

    def test_residential_creates_session_proxies(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test get_proxies creates session-based proxies for residential."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        proxies = provider.get_proxies()

        assert len(proxies) == 3
        for proxy in proxies:
            assert proxy.host == BRIGHTDATA_HOST
            assert proxy.port == BRIGHTDATA_PORT
            assert proxy.protocol == ProxyProtocol.HTTP
            assert proxy.status == ProxyStatus.HEALTHY
            assert "session_id" in proxy.metadata
            assert proxy.metadata["proxy_type"] == "residential"
            assert proxy.username.startswith("brd-customer-test-customer-zone-test_zone_res-session-glob_")
            assert proxy.password == "zone_pass_123"

    def test_mobile_creates_session_proxies(
        self, residential_credential: Credential
    ) -> None:
        """Test get_proxies creates session-based proxies for mobile."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "mobile_zone",
                "zone_password": "mobile_pass",
                "proxy_type": BrightDataProxyType.MOBILE.value,
                "num_proxies": 2,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, residential_credential)
        proxies = provider.get_proxies()

        assert len(proxies) == 2
        for proxy in proxies:
            assert proxy.metadata["proxy_type"] == "mobile"
            assert "session_id" in proxy.metadata

    def test_datacenter_creates_port_proxies(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test get_proxies creates port-based proxies for datacenter."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        proxies = provider.get_proxies()

        assert len(proxies) == 2
        for proxy in proxies:
            assert proxy.host == BRIGHTDATA_HOST
            assert proxy.port == BRIGHTDATA_PORT
            assert proxy.status == ProxyStatus.INITIALIZING
            assert proxy.metadata["proxy_type"] == "datacenter"
            assert "session_id" not in proxy.metadata
            assert proxy.password == "zone_pass_456"

    def test_isp_creates_port_proxies(
        self, datacenter_credential: Credential
    ) -> None:
        """Test get_proxies creates port-based proxies for ISP."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="proj",
            config={
                "zone_name": "isp_zone",
                "zone_password": "isp_pass",
                "proxy_type": BrightDataProxyType.ISP.value,
                "num_proxies": 1,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, datacenter_credential)
        proxies = provider.get_proxies()

        assert len(proxies) == 1
        assert proxies[0].metadata["proxy_type"] == "isp"
        assert proxies[0].status == ProxyStatus.INITIALIZING


class TestBrightDataProviderDiscoverIp:
    """Tests for BrightDataProvider.discover_ip method."""

    @pytest.mark.asyncio
    async def test_session_based_returns_none(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test discover_ip returns (None, None) for session-based proxies."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        proxy = provider.get_proxies()[0]

        result = await provider.discover_ip(proxy)
        assert result == (None, None)

    @pytest.mark.asyncio
    async def test_port_based_discovers_ip(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test discover_ip returns IP from JSON body for both display and hashed_ip.

        The x-brd-ip header is in the proxy CONNECT response which httpx doesn't
        expose, so we use the IP from the JSON body for both values.
        """
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        proxy = provider.get_proxies()[0]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "1.2.3.4"}

        # Create a mock client that works as an async context manager
        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.brightdata.httpx.AsyncClient", return_value=mock_client_instance):
            result = await provider.discover_ip(proxy)
            # Both display_ip and hashed_ip should be the same IP from JSON body
            assert result == ("1.2.3.4", "1.2.3.4")

    @pytest.mark.asyncio
    async def test_port_based_handles_missing_ip_in_json(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test discover_ip handles missing IP in JSON response gracefully."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        proxy = provider.get_proxies()[0]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # No "ip" field

        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.brightdata.httpx.AsyncClient", return_value=mock_client_instance):
            result = await provider.discover_ip(proxy)
            # Should return None for both when IP is missing
            assert result == (None, None)



class TestBrightDataProviderSyncProxies:
    """Tests for BrightDataProvider.sync_proxies method."""

    @pytest.mark.asyncio
    async def test_sync_adds_proxies_when_below_target(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test sync_proxies adds proxies when count is below target."""
        provider = BrightDataProvider(residential_connector, residential_credential)

        # Start with 1 proxy instead of 3
        existing_proxies = provider.get_proxies()[:1]

        # Sync should add 2 more proxies
        proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

        assert len(proxies_to_add) == 2
        assert len(proxy_ids_to_remove) == 0
        for proxy in proxies_to_add:
            assert proxy.status == ProxyStatus.HEALTHY
            assert "session_id" in proxy.metadata

    @pytest.mark.asyncio
    async def test_sync_removes_proxies_when_above_target(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test sync_proxies removes proxies when count is above target."""
        provider = BrightDataProvider(residential_connector, residential_credential)

        # Start with 5 proxies instead of 3
        existing_proxies = provider.get_proxies()
        existing_proxies.extend(provider.get_proxies()[:2])

        # Sync should remove 2 proxies
        proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

        assert len(proxies_to_add) == 0  # No new proxies added
        assert len(proxy_ids_to_remove) == 2  # 2 proxies should be removed

    @pytest.mark.asyncio
    async def test_sync_does_nothing_when_at_target(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test sync_proxies does nothing when count matches target."""
        provider = BrightDataProvider(residential_connector, residential_credential)

        # Start with exactly 3 proxies
        existing_proxies = provider.get_proxies()

        # Sync should do nothing
        proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

        assert len(proxies_to_add) == 0
        assert len(proxy_ids_to_remove) == 0

    @pytest.mark.asyncio
    async def test_sync_port_based_discovers_ips(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test sync_proxies discovers IPs for new port-based proxies."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        # Start with 0 proxies
        existing_proxies = []

        # Mock IP discovery
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "1.2.3.4"}

        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.brightdata.httpx.AsyncClient", return_value=mock_client_instance):
            proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

            assert len(proxies_to_add) == 2
            assert len(proxy_ids_to_remove) == 0
            for proxy in proxies_to_add:
                assert proxy.display_host == "1.2.3.4"
                assert "hashed_ip" in proxy.metadata
                # hashed_ip is the same as display_ip (from JSON body)
                assert proxy.metadata["hashed_ip"] == "1.2.3.4"


class TestBrightDataProviderRefreshIps:
    """Tests for BrightDataProvider.refresh_ips method."""

    @pytest.mark.asyncio
    async def test_refresh_session_based_does_nothing(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test refresh_ips does nothing for session-based proxies."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        proxies = provider.get_proxies()

        # Should return empty list (no updates)
        updated = await provider.refresh_ips(proxies)
        assert len(updated) == 0

    @pytest.mark.asyncio
    async def test_refresh_port_based_updates_ips(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test refresh_ips updates IPs for port-based proxies."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        proxies = provider.get_proxies()

        # Set initial IPs
        for proxy in proxies:
            proxy.display_host = "old.ip.address"
            proxy.metadata["hashed_ip"] = "old.ip.address"

        # Mock IP discovery
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "5.6.7.8"}

        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.brightdata.httpx.AsyncClient", return_value=mock_client_instance):
            updated = await provider.refresh_ips(proxies)

            assert len(updated) == 2
            for proxy in updated:
                assert proxy.display_host == "5.6.7.8"
                # hashed_ip is the same as display_ip (from JSON body)
                assert proxy.metadata["hashed_ip"] == "5.6.7.8"
                # Username should be updated with new IP
                assert "5.6.7.8" in proxy.username


class TestBrightDataConstants:
    """Tests for BrightData constants and configuration."""

    def test_session_based_types(self) -> None:
        """Test SESSION_BASED_TYPES contains correct types."""
        assert BrightDataProxyType.RESIDENTIAL in SESSION_BASED_TYPES
        assert BrightDataProxyType.MOBILE in SESSION_BASED_TYPES
        assert len(SESSION_BASED_TYPES) == 2

    def test_port_based_types(self) -> None:
        """Test PORT_BASED_TYPES contains correct types."""
        assert BrightDataProxyType.ISP in PORT_BASED_TYPES
        assert BrightDataProxyType.DATACENTER in PORT_BASED_TYPES
        assert len(PORT_BASED_TYPES) == 2

    def test_host_and_port_constants(self) -> None:
        """Test BrightData host and port constants."""
        assert BRIGHTDATA_HOST == "brd.superproxy.io"
        assert BRIGHTDATA_PORT == 33335

