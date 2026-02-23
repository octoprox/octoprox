# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for Oxylabs proxy provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.models.connector import Connector
from api.models.credential import Credential, CredentialType, OxylabsProxyType
from api.providers.oxylabs import (
    OXYLABS_ENDPOINTS,
    PORT_BASED_TYPES,
    SESSION_BASED_TYPES,
    OxylabsProvider,
    _generate_session_id,
)


@pytest.fixture
def residential_credential() -> Credential:
    """Create a sample Oxylabs residential credential."""
    return Credential(
        id="test-oxylabs-cred",
        name="Test Oxylabs Credential",
        type=CredentialType.OXYLABS,
        project_id="test-project",
        config={
            "proxy_type": OxylabsProxyType.RESIDENTIAL.value,
            "username": "testuser",
            "password": "testpass123",
        },
    )


@pytest.fixture
def datacenter_credential() -> Credential:
    """Create a sample Oxylabs datacenter credential."""
    return Credential(
        id="test-oxylabs-dc-cred",
        name="Test Oxylabs DC Credential",
        type=CredentialType.OXYLABS,
        project_id="test-project",
        config={
            "proxy_type": OxylabsProxyType.DATACENTER.value,
            "username": "dcuser",
            "password": "dcpass123",
        },
    )


@pytest.fixture
def residential_connector() -> Connector:
    """Create a sample Oxylabs residential connector."""
    return Connector(
        id="test-oxylabs-connector",
        name="Test Oxylabs Connector",
        credential_id="test-oxylabs-cred",
        credential_type=CredentialType.OXYLABS,
        project_id="test-project",
        config={
            "num_proxies": 3,
            "country_code": "US",
            "session_duration_minutes": 10,
        },
        enabled=True,
    )


@pytest.fixture
def datacenter_connector() -> Connector:
    """Create a sample Oxylabs datacenter connector."""
    return Connector(
        id="test-oxylabs-dc-connector",
        name="Test Oxylabs DC Connector",
        credential_id="test-oxylabs-dc-cred",
        credential_type=CredentialType.OXYLABS,
        project_id="test-project",
        config={
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

    def test_length_is_12(self) -> None:
        """Test that session ID is 12 characters."""
        session_id = _generate_session_id()
        assert len(session_id) == 12

    def test_is_alphanumeric_lowercase(self) -> None:
        """Test that session ID contains only lowercase alphanumeric chars."""
        session_id = _generate_session_id()
        assert session_id.isalnum()
        assert session_id.islower()

    def test_unique_ids(self) -> None:
        """Test that successive calls produce different IDs."""
        ids = [_generate_session_id() for _ in range(10)]
        assert len(set(ids)) == 10


class TestOxylabsProviderInit:
    """Tests for OxylabsProvider initialization."""

    def test_init_residential(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test OxylabsProvider initializes correctly for residential type."""
        provider = OxylabsProvider(residential_connector, residential_credential)

        assert provider.connector == residential_connector
        assert provider.credential == residential_credential
        assert provider._proxy_type == OxylabsProxyType.RESIDENTIAL
        assert provider._username == "testuser"
        assert provider._password == "testpass123"
        assert provider._endpoint == "pr.oxylabs.io"
        assert provider._base_port == 7777

    def test_init_datacenter(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test OxylabsProvider initializes correctly for datacenter type."""
        provider = OxylabsProvider(datacenter_connector, datacenter_credential)

        assert provider._proxy_type == OxylabsProxyType.DATACENTER
        assert provider._endpoint == "dc.oxylabs.io"
        assert provider._base_port == 8001

    def test_init_without_credential_raises(
        self, residential_connector: Connector
    ) -> None:
        """Test that OxylabsProvider raises without credentials."""
        with pytest.raises(ValueError, match="requires credentials"):
            OxylabsProvider(residential_connector, None)  # type: ignore

    def test_is_session_based_residential(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test is_session_based returns True for residential."""
        provider = OxylabsProvider(residential_connector, residential_credential)
        assert provider.is_session_based() is True

    def test_is_session_based_datacenter(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test is_session_based returns False for datacenter."""
        provider = OxylabsProvider(datacenter_connector, datacenter_credential)
        assert provider.is_session_based() is False


class TestOxylabsProviderBuildUsername:
    """Tests for OxylabsProvider._build_username method.

    Note: Usernames now contain {username} placeholder that gets resolved
    by the proxy manager at request time.
    """

    def test_residential_with_country_and_session(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test username format for residential with country and session."""
        provider = OxylabsProvider(residential_connector, residential_credential)
        username = provider._build_username("abc123")

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "customer-{username}-cc-US-sessid-abc123"

    def test_residential_without_country(
        self, residential_credential: Credential
    ) -> None:
        """Test username format for residential without country code."""
        connector = Connector(
            id="test-connector",
            name="Test",
            credential_id="cred",
            credential_type=CredentialType.OXYLABS,
            project_id="proj",
            config={"num_proxies": 1, "country_code": "", "session_duration_minutes": 10},
            enabled=True,
        )
        provider = OxylabsProvider(connector, residential_credential)
        username = provider._build_username("xyz789")

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "customer-{username}-sessid-xyz789"

    def test_datacenter_username(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test username format for datacenter (port-based)."""
        provider = OxylabsProvider(datacenter_connector, datacenter_credential)
        username = provider._build_username()

        # Username contains placeholder that will be resolved by proxy manager
        assert username == "user-{username}"


class TestOxylabsProviderDiscoverIp:
    """Tests for OxylabsProvider.discover_ip method."""

    @pytest.mark.asyncio
    async def test_session_based_returns_none(
        self, residential_connector: Connector, residential_credential: Credential
    ) -> None:
        """Test discover_ip returns None for session-based proxies."""
        provider = OxylabsProvider(residential_connector, residential_credential)
        proxy = provider._create_session_proxy("test_session_123", 0)

        result = await provider.discover_ip(proxy)
        assert result is None

    @pytest.mark.asyncio
    async def test_port_based_discovers_ip(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test discover_ip returns IP for port-based proxies."""
        provider = OxylabsProvider(datacenter_connector, datacenter_credential)
        proxy = provider._create_port_proxy(8001, 0)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "1.2.3.4"}

        # Create a mock client that works as an async context manager
        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.oxylabs.httpx.AsyncClient", return_value=mock_client_instance):
            result = await provider.discover_ip(proxy)
            assert result == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_port_based_uses_host_for_routing(
        self, datacenter_connector: Connector, datacenter_credential: Credential
    ) -> None:
        """Test discover_ip uses proxy.host for routing (not display_host)."""
        provider = OxylabsProvider(datacenter_connector, datacenter_credential)
        proxy = provider._create_port_proxy(8001, 0)
        # Simulate that display_host has been set to discovered IP
        # but host should still be the Oxylabs endpoint
        proxy.display_host = "1.2.3.4"
        assert proxy.host == "dc.oxylabs.io"  # host should be the endpoint

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ip": "5.6.7.8"}

        # Create a mock client that works as an async context manager
        mock_client_instance = MagicMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("api.providers.oxylabs.httpx.AsyncClient", return_value=mock_client_instance) as mock_client:
            result = await provider.discover_ip(proxy)
            assert result == "5.6.7.8"

            # Verify the proxy URL used host (the endpoint), not display_host
            call_kwargs = mock_client.call_args.kwargs
            assert "dc.oxylabs.io:8001" in call_kwargs["proxy"]
            assert "1.2.3.4" not in call_kwargs["proxy"]


class TestOxylabsEndpoints:
    """Tests for Oxylabs endpoint configuration."""

    def test_all_session_types_have_endpoints(self) -> None:
        """Test all session-based types have endpoint configuration."""
        for proxy_type in SESSION_BASED_TYPES:
            assert proxy_type in OXYLABS_ENDPOINTS

    def test_all_port_types_have_endpoints(self) -> None:
        """Test all port-based types have endpoint configuration."""
        for proxy_type in PORT_BASED_TYPES:
            assert proxy_type in OXYLABS_ENDPOINTS

    def test_session_types_use_port_7777(self) -> None:
        """Test session-based types use port 7777."""
        for proxy_type in SESSION_BASED_TYPES:
            _, port = OXYLABS_ENDPOINTS[proxy_type]
            assert port == 7777

    def test_port_types_use_port_8001(self) -> None:
        """Test port-based types use base port 8001."""
        for proxy_type in PORT_BASED_TYPES:
            _, port = OXYLABS_ENDPOINTS[proxy_type]
            assert port == 8001
