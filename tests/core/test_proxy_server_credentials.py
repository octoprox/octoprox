# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProxyManager dynamic credential resolution.

The proxy manager resolves credential placeholders in proxy username/password
when selecting proxies. Providers store credentials with placeholders like
{username}, {password}, {customer_id}, {zone_password} that are resolved
at request time using values from the credential/connector chain.
"""

import pytest

from api.core.proxy_manager import ProxyManager
from api.models.connector import BrightDataProxyType, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyProtocol


class TestProxyManagerCredentialResolution:
    """Tests for dynamic credential resolution in ProxyManager."""

    @pytest.fixture
    def proxy_manager_with_data(self) -> ProxyManager:
        """Create a ProxyManager with in-memory data for testing.

        We directly manipulate the internal caches to avoid needing
        a full database setup.
        """
        from unittest.mock import MagicMock

        # Create a minimal proxy manager with mocked dependencies
        mock_session_factory = MagicMock()
        mock_redis_client = MagicMock()
        mock_settings = MagicMock()
        mock_settings.default_strategy = "round_robin"

        manager = ProxyManager(mock_session_factory, mock_redis_client, mock_settings)
        return manager

    def test_resolve_credentials_no_connector(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test that proxy is returned as-is when connector not found."""
        proxy = Proxy(
            host="proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            username="{username}",
            password="{password}",
            connector_id="missing-connector",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        # No resolution possible, placeholders remain
        assert resolved.username == "{username}"
        assert resolved.password == "{password}"

    def test_resolve_credentials_no_credential(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test that proxy is returned as-is when credential not found."""
        connector = Connector(
            id="connector-1",
            name="Test Connector",
            credential_id="missing-credential",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1",
        )
        proxy_manager_with_data._connectors["connector-1"] = connector

        proxy = Proxy(
            host="proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            username="{username}",
            password="{password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        # No resolution possible, placeholders remain
        assert resolved.username == "{username}"
        assert resolved.password == "{password}"

    def test_resolve_oxylabs_session_based_credentials(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test resolving Oxylabs session-based credentials with placeholders."""
        connector = Connector(
            id="connector-1",
            name="Oxylabs Connector",
            credential_id="credential-1",
            credential_type=CredentialType.OXYLABS,
            project_id="project-1",
            config={"num_proxies": 10, "country_code": "US"},
        )
        credential = Credential(
            id="credential-1",
            name="Oxylabs Credential",
            type=CredentialType.OXYLABS,
            project_id="project-1",
            config={
                "username": "newuser",
                "password": "newpass",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with placeholders (as created by OxylabsProvider)
        proxy = Proxy(
            host="pr.oxylabs.io",
            port=7777,
            protocol=ProxyProtocol.HTTP,
            username="customer-{username}-cc-US-sessid-abc123",
            password="{password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        assert resolved.username == "customer-newuser-cc-US-sessid-abc123"
        assert resolved.password == "newpass"

    def test_resolve_oxylabs_port_based_credentials(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test resolving Oxylabs port-based credentials with placeholders."""
        connector = Connector(
            id="connector-1",
            name="Oxylabs ISP Connector",
            credential_id="credential-1",
            credential_type=CredentialType.OXYLABS,
            project_id="project-1",
            config={"num_proxies": 5},
        )
        credential = Credential(
            id="credential-1",
            name="Oxylabs Credential",
            type=CredentialType.OXYLABS,
            project_id="project-1",
            config={
                "username": "ispuser",
                "password": "isppass",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with placeholders (as created by OxylabsProvider for ISP)
        proxy = Proxy(
            host="isp.oxylabs.io",
            port=8001,
            protocol=ProxyProtocol.HTTP,
            username="user-{username}",
            password="{password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        assert resolved.username == "user-ispuser"
        assert resolved.password == "isppass"

    def test_resolve_brightdata_session_based_credentials(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test resolving BrightData session-based credentials with placeholders."""
        connector = Connector(
            id="connector-1",
            name="BrightData Connector",
            credential_id="credential-1",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "zone_name": "zone1",
                "zone_password": "zonepass123",
                "proxy_type": BrightDataProxyType.RESIDENTIAL.value,
                "num_proxies": 10,
                "country_code": "GB",
            },
        )
        credential = Credential(
            id="credential-1",
            name="BrightData Credential",
            type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "token": "api_token",
                "customer_id": "cust_new123",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with placeholders (as created by BrightDataProvider)
        proxy = Proxy(
            host="brd.superproxy.io",
            port=44445,
            protocol=ProxyProtocol.HTTP,
            username="brd-customer-{customer_id}-zone-zone1-session-glob_xyz789-country-gb",
            password="{zone_password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        assert resolved.username == "brd-customer-cust_new123-zone-zone1-session-glob_xyz789-country-gb"
        assert resolved.password == "zonepass123"

    def test_resolve_brightdata_port_based_credentials(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test resolving BrightData port-based (ISP) credentials with placeholders."""
        connector = Connector(
            id="connector-1",
            name="BrightData ISP Connector",
            credential_id="credential-1",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "zone_name": "isp_zone",
                "zone_password": "ispzonepass",
                "proxy_type": BrightDataProxyType.ISP.value,
                "num_proxies": 5,
            },
        )
        credential = Credential(
            id="credential-1",
            name="BrightData Credential",
            type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "token": "api_token",
                "customer_id": "cust_isp456",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with placeholders (as created by BrightDataProvider for ISP)
        proxy = Proxy(
            host="brd.superproxy.io",
            port=44445,
            protocol=ProxyProtocol.HTTP,
            username="brd-customer-{customer_id}-zone-isp_zone-ip-192.168.1.100",
            password="{zone_password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        assert resolved.username == "brd-customer-cust_isp456-zone-isp_zone-ip-192.168.1.100"
        assert resolved.password == "ispzonepass"

    def test_resolve_static_proxy_with_credential_defaults(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test that static proxies can use credential default username/password."""
        connector = Connector(
            id="connector-1",
            name="Static Connector",
            credential_id="credential-1",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1",
        )
        credential = Credential(
            id="credential-1",
            name="Static Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1",
            config={
                "username": "default_user",
                "password": "default_pass",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with placeholders (for proxies without their own credentials)
        proxy = Proxy(
            host="static.proxy.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            username="{username}",
            password="{password}",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        assert resolved.username == "default_user"
        assert resolved.password == "default_pass"

    def test_proxy_without_placeholders_unchanged(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test that proxies without placeholders are returned unchanged."""
        connector = Connector(
            id="connector-1",
            name="Static Connector",
            credential_id="credential-1",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1",
        )
        credential = Credential(
            id="credential-1",
            name="Static Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1",
            config={
                "username": "default_user",
                "password": "default_pass",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        # Proxy with its own credentials (no placeholders)
        proxy = Proxy(
            host="static.proxy.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            username="own_user",
            password="own_pass",
            connector_id="connector-1",
        )

        resolved = proxy_manager_with_data.resolve_proxy_credentials(proxy)

        # Should be unchanged since no placeholders
        assert resolved.username == "own_user"
        assert resolved.password == "own_pass"

    def test_build_credential_context_oxylabs(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test building credential context for Oxylabs."""
        connector = Connector(
            id="connector-1",
            name="Oxylabs Connector",
            credential_id="credential-1",
            credential_type=CredentialType.OXYLABS,
            project_id="project-1",
        )
        credential = Credential(
            id="credential-1",
            name="Oxylabs Credential",
            type=CredentialType.OXYLABS,
            project_id="project-1",
            config={
                "username": "oxy_user",
                "password": "oxy_pass",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        proxy = Proxy(
            host="pr.oxylabs.io",
            port=7777,
            protocol=ProxyProtocol.HTTP,
            connector_id="connector-1",
        )

        context = proxy_manager_with_data._build_credential_context(proxy)

        assert context["username"] == "oxy_user"
        assert context["password"] == "oxy_pass"

    def test_build_credential_context_brightdata(
        self, proxy_manager_with_data: ProxyManager
    ) -> None:
        """Test building credential context for BrightData."""
        connector = Connector(
            id="connector-1",
            name="BrightData Connector",
            credential_id="credential-1",
            credential_type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "zone_name": "zone1",
                "zone_password": "zone_secret",
                "proxy_type": BrightDataProxyType.RESIDENTIAL.value,
                "num_proxies": 5,
            },
        )
        credential = Credential(
            id="credential-1",
            name="BrightData Credential",
            type=CredentialType.BRIGHTDATA,
            project_id="project-1",
            config={
                "token": "api_token",
                "customer_id": "cust_123",
            },
        )
        proxy_manager_with_data._connectors["connector-1"] = connector
        proxy_manager_with_data._credentials["credential-1"] = credential

        proxy = Proxy(
            host="brd.superproxy.io",
            port=44445,
            protocol=ProxyProtocol.HTTP,
            connector_id="connector-1",
        )

        context = proxy_manager_with_data._build_credential_context(proxy)

        assert context["customer_id"] == "cust_123"
        assert context["zone_password"] == "zone_secret"

