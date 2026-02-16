"""Tests for cloud provider classes."""

import pytest

from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.providers.cloud import AWSProvider, AzureProvider, GCPProvider, PROXY_PORT


@pytest.fixture
def aws_connector() -> Connector:
    """Create a sample AWS connector for testing."""
    return Connector(
        id="test-aws-connector",
        name="Test AWS Connector",
        credential_id="test-credential",
        credential_type=CredentialType.AWS,
        project_id="test-project",
        config={
            "region": "us-east-1",
            "instance_type": "t3.micro",
            "ami_id": "ami-12345678",
            "security_group": "sg-12345678",
            "key_pair_name": "my-key-pair",
            "min_proxies": 1,
            "max_proxies": 5,
        },
        enabled=True,
    )


@pytest.fixture
def aws_credential() -> Credential:
    """Create a sample AWS credential for testing."""
    return Credential(
        id="test-aws-credential",
        name="Test AWS Credential",
        type=CredentialType.AWS,
        project_id="test-project",
        config={
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        },
    )


@pytest.fixture
def gcp_connector() -> Connector:
    """Create a sample GCP connector for testing."""
    return Connector(
        id="test-gcp-connector",
        name="Test GCP Connector",
        credential_id="test-credential",
        credential_type=CredentialType.GCP,
        project_id="test-project",
        config={
            "project_id": "my-gcp-project",
            "zone": "us-central1-a",
            "machine_type": "e2-micro",
            "network": "default",
        },
        enabled=True,
    )


@pytest.fixture
def gcp_credential() -> Credential:
    """Create a sample GCP credential for testing."""
    return Credential(
        id="test-gcp-credential",
        name="Test GCP Credential",
        type=CredentialType.GCP,
        project_id="test-project",
        config={
            "service_account_json": '{"type": "service_account", "project_id": "test"}',
        },
    )


@pytest.fixture
def azure_connector() -> Connector:
    """Create a sample Azure connector for testing."""
    return Connector(
        id="test-azure-connector",
        name="Test Azure Connector",
        credential_id="test-credential",
        credential_type=CredentialType.AZURE,
        project_id="test-project",
        config={
            "subscription_id": "sub-12345",
            "resource_group": "my-resource-group",
            "location": "eastus",
            "vm_size": "Standard_B1s",
        },
        enabled=True,
    )


@pytest.fixture
def azure_credential() -> Credential:
    """Create a sample Azure credential for testing."""
    return Credential(
        id="test-azure-credential",
        name="Test Azure Credential",
        type=CredentialType.AZURE,
        project_id="test-project",
        config={
            "client_id": "client-id-123",
            "client_secret": "client-secret-456",
            "tenant_id": "tenant-id-789",
        },
    )


class TestAWSProviderInit:
    """Tests for AWSProvider initialization."""

    def test_init_with_connector_and_credential(
        self, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        """Test AWSProvider initializes correctly with connector and credential."""
        provider = AWSProvider(aws_connector, aws_credential)

        assert provider.connector == aws_connector
        assert provider.credential == aws_credential
        assert provider._region == "us-east-1"
        assert provider._instance_type == "t3.micro"
        assert provider._ami_id == "ami-12345678"
        assert provider._security_group == "sg-12345678"
        assert provider._key_pair_name == "my-key-pair"

    def test_init_with_defaults(self, aws_credential: Credential) -> None:
        """Test AWSProvider uses defaults for missing config values."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.AWS,
            project_id="proj",
            config={},
        )
        provider = AWSProvider(connector, aws_credential)

        assert provider._region == "us-east-1"  # default
        assert provider._instance_type == "t3.micro"  # default


class TestGCPProviderInit:
    """Tests for GCPProvider initialization."""

    def test_init_with_connector_and_credential(
        self, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        """Test GCPProvider initializes correctly with connector and credential."""
        provider = GCPProvider(gcp_connector, gcp_credential)

        assert provider.connector == gcp_connector
        assert provider.credential == gcp_credential
        assert provider._project_id == "my-gcp-project"
        assert provider._zone == "us-central1-a"
        assert provider._machine_type == "e2-micro"
        assert provider._network == "default"

    def test_init_with_defaults(self, gcp_credential: Credential) -> None:
        """Test GCPProvider uses defaults for missing config values."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.GCP,
            project_id="proj",
            config={},
        )
        provider = GCPProvider(connector, gcp_credential)

        assert provider._zone == "us-central1-a"  # default
        assert provider._machine_type == "e2-micro"  # default
        assert provider._network == "default"  # default


class TestAzureProviderInit:
    """Tests for AzureProvider initialization."""

    def test_init_with_connector_and_credential(
        self, azure_connector: Connector, azure_credential: Credential
    ) -> None:
        """Test AzureProvider initializes correctly with connector and credential."""
        provider = AzureProvider(azure_connector, azure_credential)

        assert provider.connector == azure_connector
        assert provider.credential == azure_credential
        assert provider._subscription_id == "sub-12345"
        assert provider._resource_group == "my-resource-group"
        assert provider._location == "eastus"
        assert provider._vm_size == "Standard_B1s"

    def test_init_with_defaults(self, azure_credential: Credential) -> None:
        """Test AzureProvider uses defaults for missing config values."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.AZURE,
            project_id="proj",
            config={},
        )
        provider = AzureProvider(connector, azure_credential)

        assert provider._location == "eastus"  # default
        assert provider._vm_size == "Standard_B1s"  # default


class TestStaticProvider:
    """Tests for StaticProvider class."""

    def test_init_with_connector(self) -> None:
        """Test StaticProvider initializes correctly with a connector."""
        from api.providers.static import StaticProvider

        connector = Connector(
            id="static-connector",
            name="Static Connector",
            credential_id="cred",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="proj",
            config={
                "proxies": [
                    {"host": "1.2.3.4", "port": 8080},
                    {"host": "5.6.7.8", "port": 3128, "protocol": "socks5"},
                ]
            },
        )
        provider = StaticProvider(connector)

        assert provider.connector == connector

    def test_get_proxies(self) -> None:
        """Test StaticProvider gets proxies from config."""
        from api.providers.static import StaticProvider

        connector = Connector(
            id="static-connector",
            name="Static Connector",
            credential_id="cred",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="proj",
            config={
                "proxies": [
                    {"host": "1.2.3.4", "port": 8080},
                    {"host": "5.6.7.8", "port": 3128, "protocol": "socks5"},
                ]
            },
        )
        provider = StaticProvider(connector)
        proxies = provider.get_proxies()

        assert len(proxies) == 2
        assert proxies[0].host == "1.2.3.4"
        assert proxies[0].port == 8080
        assert proxies[1].host == "5.6.7.8"
        assert proxies[1].port == 3128


class TestAPIProvider:
    """Tests for APIProvider class."""

    def test_init_with_connector(self) -> None:
        """Test APIProvider initializes correctly with a connector."""
        from api.providers.api_provider import APIProvider

        connector = Connector(
            id="api-connector",
            name="API Connector",
            credential_id="cred",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="proj",
            config={
                "url": "https://api.example.com/proxies",
                "api_key": "secret-key",
                "headers": {"X-Custom": "value"},
            },
        )
        provider = APIProvider(connector)

        assert provider.connector == connector
        assert provider._url == "https://api.example.com/proxies"
        assert provider._api_key == "secret-key"
        assert provider._headers == {"X-Custom": "value"}
