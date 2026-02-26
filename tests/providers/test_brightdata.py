# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for BrightData proxy provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models.connector import BrightDataProxyType, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import ProxyStatus
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
    """Tests for BrightDataProvider._build_username method.

    Note: Usernames now contain {customer_id} placeholder that gets resolved
    by the proxy manager at request time.
    """

    def test_residential_with_country_and_session(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test username format for residential with country and session."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        username = provider._build_username(session_id="glob_abc123xyz456")

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "brd-customer-{customer_id}-zone-test_zone_res-session-glob_abc123xyz456-country-us"

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

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "brd-customer-{customer_id}-zone-res_zone-session-glob_xyz789"

    def test_datacenter_with_hashed_ip(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test username format for datacenter with hashed IP."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        username = provider._build_username(hashed_ip="hashed_ip_123")

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "brd-customer-{customer_id}-zone-test_zone_dc-ip-hashed_ip_123"

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

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "brd-customer-{customer_id}-zone-dc_zone-ip-hashed_ip_456-country-gb"

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

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "brd-customer-{customer_id}-zone-isp_zone-ip-hashed_ip_789"



class TestBrightDataProviderDiscoverIp:
    """Tests for BrightDataProvider.discover_ip method."""

    @pytest.mark.asyncio
    async def test_session_based_returns_none(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test discover_ip returns (None, None) for session-based proxies."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        proxy = provider._create_session_proxy(0)

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
        proxy = provider._create_port_proxy(0)

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
        proxy = provider._create_port_proxy(0)

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
        existing_proxies = [provider._create_session_proxy(0)]

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
        existing_proxies = [provider._create_session_proxy(i) for i in range(5)]

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
        existing_proxies = [provider._create_session_proxy(i) for i in range(3)]

        # Sync should do nothing
        proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

        assert len(proxies_to_add) == 0
        assert len(proxy_ids_to_remove) == 0

    @pytest.mark.asyncio
    async def test_sync_port_based_discovers_ips(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test sync_proxies discovers unique IPs for new port-based proxies."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        # Start with 0 proxies
        existing_proxies: list = []

        # Mock discover_ip to return unique IPs
        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            mock_discover.side_effect = [("1.2.3.4", "1.2.3.4"), ("5.6.7.8", "5.6.7.8")]

            proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

            assert len(proxies_to_add) == 2
            assert len(proxy_ids_to_remove) == 0
            assert proxies_to_add[0].display_host == "1.2.3.4"
            assert proxies_to_add[0].metadata["hashed_ip"] == "1.2.3.4"
            assert proxies_to_add[1].display_host == "5.6.7.8"
            assert proxies_to_add[1].metadata["hashed_ip"] == "5.6.7.8"


class TestBrightDataProviderRefreshIps:
    """Tests for BrightDataProvider.refresh_ips method."""

    @pytest.mark.asyncio
    async def test_refresh_session_based_does_nothing(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test refresh_ips does nothing for session-based proxies."""
        provider = BrightDataProvider(residential_connector, residential_credential)
        proxies = [provider._create_session_proxy(i) for i in range(3)]

        # Should return empty lists (no updates, no removals)
        updated, to_remove = await provider.refresh_ips(proxies)
        assert len(updated) == 0
        assert len(to_remove) == 0

    @pytest.mark.asyncio
    async def test_refresh_port_based_updates_ips(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test refresh_ips updates IPs for port-based proxies."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)
        proxies = [provider._create_port_proxy(i) for i in range(2)]

        # Set initial IPs
        for proxy in proxies:
            proxy.display_host = "old.ip.address"
            proxy.metadata["hashed_ip"] = "old.ip.address"

        # Mock discover_ip to return unique IPs
        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            mock_discover.side_effect = [("5.6.7.8", "5.6.7.8"), ("9.10.11.12", "9.10.11.12")]

            updated, to_remove = await provider.refresh_ips(proxies)

            assert len(updated) == 2
            assert len(to_remove) == 0
            assert updated[0].display_host == "5.6.7.8"
            assert updated[0].metadata["hashed_ip"] == "5.6.7.8"
            assert "5.6.7.8" in updated[0].username
            assert updated[1].display_host == "9.10.11.12"
            assert updated[1].metadata["hashed_ip"] == "9.10.11.12"
            assert "9.10.11.12" in updated[1].username


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


class TestBrightDataSyncDeduplicate:
    """Tests for duplicate IP detection in sync_proxies."""

    @pytest.mark.asyncio
    async def test_sync_port_based_retries_on_duplicate_ip(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test that sync retries when a duplicate IP is discovered."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            # Slot 0: unique IP, Slot 1: dupe then unique on retry
            mock_discover.side_effect = [
                ("1.2.3.4", "1.2.3.4"),  # slot 0 - unique
                ("1.2.3.4", "1.2.3.4"),  # slot 1 attempt 1 - dupe
                ("5.6.7.8", "5.6.7.8"),  # slot 1 attempt 2 - unique
            ]

            proxies_to_add, _ = await provider.sync_proxies([])

            assert len(proxies_to_add) == 2
            assert proxies_to_add[0].metadata["discovered_ip"] == "1.2.3.4"
            assert proxies_to_add[1].metadata["discovered_ip"] == "5.6.7.8"
            assert mock_discover.call_count == 3

    @pytest.mark.asyncio
    async def test_sync_port_based_gives_up_after_max_retries(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test that sync gives up on a slot after max retries."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            # Slot 0: unique, Slot 1: 3 dupes (all retries exhausted)
            mock_discover.side_effect = [
                ("1.2.3.4", "1.2.3.4"),  # slot 0 - unique
                ("1.2.3.4", "1.2.3.4"),  # slot 1 attempt 1 - dupe
                ("1.2.3.4", "1.2.3.4"),  # slot 1 attempt 2 - dupe
                ("1.2.3.4", "1.2.3.4"),  # slot 1 attempt 3 - dupe
            ]

            proxies_to_add, _ = await provider.sync_proxies([])

            # Only slot 0 added, slot 1 failed all retries
            assert len(proxies_to_add) == 1
            assert mock_discover.call_count == 4

    @pytest.mark.asyncio
    async def test_sync_port_based_stops_after_consecutive_failed_slots(
        self, datacenter_credential: Credential
    ) -> None:
        """Test that sync stops after 3 consecutive failed slots."""
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
                "num_proxies": 5,
            },
            enabled=True,
        )
        provider = BrightDataProvider(connector, datacenter_credential)

        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            # Slot 0: unique, Slots 1-3: all return dupes (3 retries each)
            mock_discover.side_effect = [
                ("1.2.3.4", "1.2.3.4"),  # slot 0 - unique
                *[("1.2.3.4", "1.2.3.4")] * 3,  # slot 1 - 3 dupe retries
                *[("1.2.3.4", "1.2.3.4")] * 3,  # slot 2 - 3 dupe retries
                *[("1.2.3.4", "1.2.3.4")] * 3,  # slot 3 - 3 dupe retries (triggers stop)
            ]

            proxies_to_add, _ = await provider.sync_proxies([])

            assert len(proxies_to_add) == 1
            # slot 0 (1 call) + slots 1-3 (3 retries each = 9 calls) = 10
            assert mock_discover.call_count == 10

    @pytest.mark.asyncio
    async def test_sync_port_based_skips_ip_already_in_existing(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test that IPs already in existing proxies trigger retries."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        # Create an existing proxy with a known IP
        existing = provider._create_port_proxy(0)
        existing.metadata["discovered_ip"] = "1.2.3.4"

        # Configure for 2 proxies, 1 existing
        datacenter_connector.config["num_proxies"] = 2

        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            # Slot 1: first attempt returns existing IP, second returns unique
            mock_discover.side_effect = [
                ("1.2.3.4", "1.2.3.4"),  # dupe of existing
                ("5.6.7.8", "5.6.7.8"),  # unique
            ]

            proxies_to_add, _ = await provider.sync_proxies([existing])

            assert len(proxies_to_add) == 1
            assert proxies_to_add[0].metadata["discovered_ip"] == "5.6.7.8"


class TestBrightDataRefreshDeduplicate:
    """Tests for duplicate IP detection in refresh_ips."""

    @pytest.mark.asyncio
    async def test_refresh_removes_duplicate_ip_proxy(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test that proxies with duplicate IPs on refresh are returned for removal."""
        provider = BrightDataProvider(datacenter_connector, datacenter_credential)

        proxies = [provider._create_port_proxy(i) for i in range(2)]
        for p in proxies:
            p.metadata["discovered_ip"] = "old.ip"
            p.metadata["hashed_ip"] = "old.ip"
            p.status = ProxyStatus.HEALTHY

        with patch.object(provider, "discover_ip", new_callable=AsyncMock) as mock_discover:
            # Both proxies refresh to the same IP
            mock_discover.return_value = ("1.2.3.4", "1.2.3.4")

            updated, to_remove = await provider.refresh_ips(proxies)

            # First proxy gets updated normally
            assert len(updated) == 1
            assert updated[0].display_host == "1.2.3.4"
            # Second proxy is returned for removal
            assert len(to_remove) == 1
            assert to_remove[0] == proxies[1].id

