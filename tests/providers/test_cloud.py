# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the cloud providers: credentials, and what each cloud is asked to create, find, list and terminate.

AWS runs on botocore's stubbed client, Azure on its SDK over a transport that answers locally, and GCP on a stand-in
for its instances client that hands back the SDK's own instance messages.
"""

import base64
import functools
import inspect
import io
import json
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import boto3
import pytest
import requests
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import RequestsTransport
from botocore.credentials import Credentials, DeferredRefreshableCredentials
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber
from google.api_core.exceptions import BadRequest, Forbidden, NotFound
from google.cloud import compute_v1

from api.models.connector import (
    AWSConnectorConfig,
    AzureConnectorConfig,
    Connector,
    GCPConnectorConfig,
)
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyStatus
from api.providers.cloud import (
    CONNECTOR_TAG,
    AWSProvider,
    AzureProvider,
    CloudInstance,
    CloudProvider,
    GCPProvider,
    ProxyCredentials,
    build_squid_setup_script,
)

# The credentials the setup script tests bake in.
TEST_CREDENTIALS = ProxyCredentials("testuser", "testpass123")


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
            "instance_name": "test-proxy",
            "region": "us-east-1",
            "instance_type": "t3.micro",
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
            "instance_name": "test-proxy",
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
            "instance_name": "test-proxy",
            "location": "eastus",
            "vm_size": "Standard_B2ls_v2",
            "ssh_public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... test@example.com",
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
        assert provider._config.region == "us-east-1"
        assert provider._config.instance_type == "t3.micro"
        assert provider._config.security_group == "sg-12345678"
        assert provider._config.key_pair_name == "my-key-pair"
        assert provider._config.instance_name == "test-proxy"

    def test_init_with_required_fields_only(self, aws_credential: Credential) -> None:
        """Test AWSProvider initializes with only required fields."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.AWS,
            project_id="proj",
            config={
                "instance_name": "minimal-proxy",
                "region": "us-west-2",
                "instance_type": "t3.small",
                "key_pair_name": "minimal-key",
                "security_group": "sg-minimal",
            },
        )
        provider = AWSProvider(connector, aws_credential)

        assert provider._config.instance_name == "minimal-proxy"
        assert provider._config.region == "us-west-2"
        assert provider._config.instance_type == "t3.small"


class TestGCPProviderInit:
    """Tests for GCPProvider initialization."""

    def test_init_with_connector_and_credential(
        self, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        """Test GCPProvider initializes correctly with connector and credential."""
        provider = GCPProvider(gcp_connector, gcp_credential)

        assert provider.connector == gcp_connector
        assert provider.credential == gcp_credential
        assert provider._config.project_id == "my-gcp-project"
        assert provider._config.instance_name == "test-proxy"
        assert provider._config.zone == "us-central1-a"
        assert provider._config.machine_type == "e2-micro"
        assert provider._config.network == "default"

    def test_init_with_defaults(self, gcp_credential: Credential) -> None:
        """Test GCPProvider uses defaults for missing config values."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.GCP,
            project_id="proj",
            config={
                "project_id": "minimal-project",
                "instance_name": "minimal-proxy",
            },
        )
        provider = GCPProvider(connector, gcp_credential)

        assert provider._config.zone == "us-central1-a"  # default
        assert provider._config.machine_type == "e2-micro"  # default
        assert provider._config.network == "default"  # default


class TestAzureProviderInit:
    """Tests for AzureProvider initialization."""

    def test_init_with_connector_and_credential(
        self, azure_connector: Connector, azure_credential: Credential
    ) -> None:
        """Test AzureProvider initializes correctly with connector and credential."""
        provider = AzureProvider(azure_connector, azure_credential)

        assert provider.connector == azure_connector
        assert provider.credential == azure_credential
        assert provider._config.subscription_id == "sub-12345"
        assert provider._config.resource_group == "my-resource-group"
        assert provider._config.instance_name == "test-proxy"
        assert provider._config.location == "eastus"
        assert provider._config.vm_size == "Standard_B2ls_v2"

    def test_init_with_defaults(self, azure_credential: Credential) -> None:
        """Test AzureProvider uses defaults for missing config values."""
        connector = Connector(
            id="minimal-connector",
            name="Minimal",
            credential_id="cred",
            credential_type=CredentialType.AZURE,
            project_id="proj",
            config={
                "subscription_id": "minimal-sub",
                "resource_group": "minimal-rg",
                "instance_name": "minimal-proxy",
                "ssh_public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ... test@example.com",
            },
        )
        provider = AzureProvider(connector, azure_credential)

        assert provider._config.location == "eastus"  # default
        assert provider._config.vm_size == "Standard_B2ls_v2"  # default


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


class TestAWSConnectorConfigImageResolution:
    """Tests for AWSConnectorConfig.get_ami() method."""

    def test_get_ami_returns_amd64_ami_for_t3_instance(self) -> None:
        """Test get_ami returns correct AMI for t3 (x86_64) instance type."""
        config = AWSConnectorConfig(
            instance_name="test",
            region="us-east-1",
            instance_type="t3.micro",
            key_pair_name="key",
            security_group="sg-123",
        )
        ami = config.get_ami()
        assert ami is not None
        assert ami.startswith("ami-")

    def test_get_ami_returns_arm64_ami_for_t4g_instance(self) -> None:
        """Test get_ami returns correct AMI for t4g (arm64) instance type."""
        config = AWSConnectorConfig(
            instance_name="test",
            region="us-east-1",
            instance_type="t4g.micro",
            key_pair_name="key",
            security_group="sg-123",
        )
        ami = config.get_ami()
        assert ami is not None
        assert ami.startswith("ami-")

    def test_get_ami_returns_different_amis_for_different_architectures(self) -> None:
        """Test that t3 and t4g instances get different AMIs (different architectures)."""
        config_t3 = AWSConnectorConfig(
            instance_name="test",
            region="us-east-1",
            instance_type="t3.micro",
            key_pair_name="key",
            security_group="sg-123",
        )
        config_t4g = AWSConnectorConfig(
            instance_name="test",
            region="us-east-1",
            instance_type="t4g.micro",
            key_pair_name="key",
            security_group="sg-123",
        )
        ami_t3 = config_t3.get_ami()
        ami_t4g = config_t4g.get_ami()
        assert ami_t3 != ami_t4g

    def test_get_ami_returns_none_for_unsupported_region(self) -> None:
        """Test get_ami returns None for unsupported region."""
        config = AWSConnectorConfig(
            instance_name="test",
            region="invalid-region-1",
            instance_type="t3.micro",
            key_pair_name="key",
            security_group="sg-123",
        )
        ami = config.get_ami()
        assert ami is None


class TestGCPConnectorConfigImageResolution:
    """Tests for GCPConnectorConfig.get_source_image() method."""

    def test_get_source_image_returns_x86_image_for_e2_instance(self) -> None:
        """Test get_source_image returns x86_64 image for e2 machine type."""
        config = GCPConnectorConfig(
            project_id="my-project",
            instance_name="test",
            zone="us-central1-a",
            machine_type="e2-micro",
        )
        image = config.get_source_image()
        assert "ubuntu-2404-lts-amd64" in image

    def test_get_source_image_returns_arm64_image_for_t2a_instance(self) -> None:
        """Test get_source_image returns arm64 image for t2a machine type."""
        config = GCPConnectorConfig(
            project_id="my-project",
            instance_name="test",
            zone="us-central1-a",
            machine_type="t2a-standard-1",
        )
        image = config.get_source_image()
        assert "ubuntu-2404-lts-arm64" in image

    def test_get_source_image_returns_different_images_for_different_architectures(self) -> None:
        """Test that e2 and t2a instances get different images."""
        config_e2 = GCPConnectorConfig(
            project_id="my-project",
            instance_name="test",
            zone="us-central1-a",
            machine_type="e2-micro",
        )
        config_t2a = GCPConnectorConfig(
            project_id="my-project",
            instance_name="test",
            zone="us-central1-a",
            machine_type="t2a-standard-1",
        )
        image_e2 = config_e2.get_source_image()
        image_t2a = config_t2a.get_source_image()
        assert image_e2 != image_t2a


class TestAzureConnectorConfigImageResolution:
    """Tests for AzureConnectorConfig.get_image_reference() method."""

    def test_get_image_reference_returns_x86_image_for_bsv2_series(self) -> None:
        """Test get_image_reference returns x86_64 image for Bsv2 VM."""
        config = AzureConnectorConfig(
            subscription_id="sub-123",
            resource_group="rg",
            instance_name="test",
            location="eastus",
            vm_size="Standard_B2ls_v2",
            ssh_public_key="ssh-rsa AAAA...",
        )
        image_ref = config.get_image_reference()
        assert image_ref["publisher"] == "Canonical"
        assert image_ref["offer"] == "ubuntu-24_04-lts"
        assert image_ref["sku"] == "server"
        assert "arm64" not in image_ref["sku"]

    def test_get_image_reference_returns_arm64_image_for_bpsv2_series(self) -> None:
        """Test get_image_reference returns arm64 image for Bpsv2 VM."""
        config = AzureConnectorConfig(
            subscription_id="sub-123",
            resource_group="rg",
            instance_name="test",
            location="eastus",
            vm_size="Standard_B2ps_v2",
            ssh_public_key="ssh-rsa AAAA...",
        )
        image_ref = config.get_image_reference()
        assert image_ref["publisher"] == "Canonical"
        assert "arm64" in image_ref["sku"]

    def test_get_image_reference_returns_different_images_for_different_architectures(self) -> None:
        """Test that Bsv2 and Bpsv2 VMs get different images."""
        config_x86 = AzureConnectorConfig(
            subscription_id="sub-123",
            resource_group="rg",
            instance_name="test",
            location="eastus",
            vm_size="Standard_B2ls_v2",
            ssh_public_key="ssh-rsa AAAA...",
        )
        config_arm = AzureConnectorConfig(
            subscription_id="sub-123",
            resource_group="rg",
            instance_name="test",
            location="eastus",
            vm_size="Standard_B2ps_v2",
            ssh_public_key="ssh-rsa AAAA...",
        )
        image_x86 = config_x86.get_image_reference()
        image_arm = config_arm.get_image_reference()
        assert image_x86["sku"] != image_arm["sku"]

    def test_get_image_reference_returns_copy(self) -> None:
        """Test that get_image_reference returns a copy, not the original dict."""
        config = AzureConnectorConfig(
            subscription_id="sub-123",
            resource_group="rg",
            instance_name="test",
            location="eastus",
            vm_size="Standard_B2ls_v2",
            ssh_public_key="ssh-rsa AAAA...",
        )
        image_ref1 = config.get_image_reference()
        image_ref2 = config.get_image_reference()
        assert image_ref1 is not image_ref2
        image_ref1["publisher"] = "Modified"
        assert image_ref2["publisher"] == "Canonical"


class TestGenerateProxyCredentials:
    """Tests for ProxyCredentials.generate."""

    def test_username_starts_with_u_and_is_eight_characters(self) -> None:
        credentials = ProxyCredentials.generate()
        assert credentials.username.startswith("u")
        assert len(credentials.username) == 8

    def test_password_length(self) -> None:
        """Test that password is 24 characters."""
        assert len(ProxyCredentials.generate().password) == 24

    def test_credentials_are_alphanumeric(self) -> None:
        """Test that credentials contain only alphanumeric characters."""
        credentials = ProxyCredentials.generate()
        assert credentials.username.isalnum()
        assert credentials.password.isalnum()

    def test_credentials_are_unique(self) -> None:
        """Test that successive calls produce different credentials."""
        assert ProxyCredentials.generate() != ProxyCredentials.generate()


class TestBuildSquidSetupScript:
    """Tests for build_squid_setup_script."""

    def test_no_auth_produces_allow_all(self) -> None:
        """Test that no credentials produces an open proxy config."""
        script = build_squid_setup_script()
        assert "http_access allow all" in script
        assert "req_header" not in script
        assert "##OCTOPROX_AUTH_CONFIG##" not in script

    def test_with_auth_uses_req_header_acl(self) -> None:
        """Test that credentials produce req_header ACL directives."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert "req_header Proxy-Authorization" in script
        assert "has_valid_auth" in script
        assert "http_access allow has_valid_auth" in script
        assert "http_access deny all" in script
        assert "deny_info TCP_RESET all" in script

    def test_with_auth_does_not_allow_all(self) -> None:
        """Test that auth mode does not contain 'allow all'."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert "http_access allow all" not in script

    def test_with_auth_contains_correct_base64(self) -> None:
        """Test that auth mode contains the correct base64-encoded credentials."""
        import base64

        script = build_squid_setup_script(TEST_CREDENTIALS)
        expected_b64 = base64.b64encode(b"testuser:testpass123").decode()
        assert expected_b64 in script

    def test_with_auth_does_not_use_auth_param(self) -> None:
        """Test that the new approach does not use auth_param (no 407 challenges)."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert "auth_param" not in script
        assert "ncsa_auth" not in script
        assert "htpasswd" not in script

    def test_with_auth_no_preamble_needed(self) -> None:
        """Test that auth mode doesn't inject bash preamble (no htpasswd setup)."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert "openssl passwd" not in script
        assert "/etc/squid/passwd" not in script

    def test_base64_plus_sign_is_escaped_for_regex(self) -> None:
        """A + in the base64 Squid matches is escaped, since the ACL is a regex."""
        script = build_squid_setup_script(ProxyCredentials("u1234567", "p>p>p>"))
        assert "dTEyMzQ1Njc6cD5wPnA\\+" in script
        assert "dTEyMzQ1Njc6cD5wPnA+" not in script

    def test_placeholders_are_fully_replaced(self) -> None:
        """Test that no placeholders remain in the output."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert "##OCTOPROX_AUTH_CONFIG##" not in script

    def test_script_is_valid_bash(self) -> None:
        """Test that the script starts with a shebang and has key structure."""
        script = build_squid_setup_script(TEST_CREDENTIALS)
        assert script.startswith("#!/bin/bash")
        assert "squid.conf" in script
        assert "systemctl restart squid" in script

    def test_no_credentials_produce_the_same_as_no_args(self) -> None:
        """Test that None credentials produce the same result as no args."""
        assert build_squid_setup_script(None) == build_squid_setup_script()



class _FakeSession:
    """Stands in for boto3.Session: records its kwargs and hands back tagged clients."""

    def __init__(self, sts: MagicMock, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._sts = sts

    def client(self, name: str, **kwargs: Any) -> Any:
        if name == "sts":
            self._sts.created_with = kwargs
            return self._sts
        return ("client", name, self.kwargs)

    def resource(self, name: str) -> Any:
        return ("resource", name, self.kwargs)

    def get_credentials(self) -> Credentials | None:
        if "aws_access_key_id" in self.kwargs:
            return Credentials(self.kwargs["aws_access_key_id"], self.kwargs["aws_secret_access_key"])
        return self.kwargs.get("host_credentials")


class _FakeBoto3:
    """Minimal boto3 module: Session() plus one shared STS mock."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.sts = MagicMock()
        self.sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIATEMPKEY",
                "SecretAccessKey": "tempsecret",
                "SessionToken": "temptoken",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }
        # What the default chain finds on the host; None means nothing.
        self.host_credentials: Credentials | None = Credentials("AKIAHOST", "hostsecret")

    def Session(self, **kwargs: Any) -> _FakeSession:  # noqa: N802 - mirrors boto3's name
        if "aws_access_key_id" not in kwargs and "botocore_session" not in kwargs:
            kwargs = {**kwargs, "host_credentials": self.host_credentials}
        session = _FakeSession(self.sts, **kwargs)
        self.sessions.append(session)
        return session


def _credential(config: dict[str, Any], credential_id: str = "test-aws-credential") -> Credential:
    return Credential(
        id=credential_id,
        name="AWS",
        type=CredentialType.AWS,
        project_id="test-project",
        config=config,
    )


def _assumed_credentials(fake_boto3: _FakeBoto3) -> Any:
    """The refreshable credentials carried by the session the EC2 clients were made from."""
    assumed = fake_boto3.sessions[-1]
    return assumed.kwargs["botocore_session"].get_credentials()


class TestAWSProviderSessions:
    """How the AWS provider turns a credential's auth mode into boto3 clients."""

    @pytest.fixture
    def fake_boto3(self, monkeypatch: pytest.MonkeyPatch) -> _FakeBoto3:
        fake = _FakeBoto3()
        monkeypatch.setitem(sys.modules, "boto3", fake)
        return fake

    def test_access_keys_mode_uses_stored_keys(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        credential = _credential(
            {"auth_mode": "access_keys", "access_key": "AKIA", "secret_key": "secret"}
        )
        provider = AWSProvider(aws_connector, credential)

        client, resource = provider._get_boto3_clients()

        assert len(fake_boto3.sessions) == 1
        assert fake_boto3.sessions[0].kwargs == {
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "secret",
            "region_name": "us-east-1",
        }
        assert client == ("client", "ec2", fake_boto3.sessions[0].kwargs)
        assert resource == ("resource", "ec2", fake_boto3.sessions[0].kwargs)
        fake_boto3.sts.assume_role.assert_not_called()

    def test_legacy_config_without_auth_mode_uses_stored_keys(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        """Credentials saved before auth_mode existed keep working unchanged."""
        provider = AWSProvider(aws_connector, _credential({"access_key": "AKIA", "secret_key": "s"}))

        provider._get_boto3_clients()

        assert fake_boto3.sessions[0].kwargs["aws_access_key_id"] == "AKIA"

    def test_access_keys_mode_without_keys_never_uses_host_identity(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        """An explicit access_keys credential with no keys fails instead of provisioning as the host."""
        provider = AWSProvider(aws_connector, _credential({"auth_mode": "access_keys"}))

        with pytest.raises(ValueError, match="access_keys mode"):
            provider._get_boto3_clients()
        assert fake_boto3.sessions == []

    def test_host_role_mode_uses_default_credential_chain(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        provider = AWSProvider(aws_connector, _credential({"auth_mode": "host_role"}))

        provider._get_boto3_clients()

        assert "aws_access_key_id" not in fake_boto3.sessions[0].kwargs
        assert fake_boto3.sessions[0].kwargs["region_name"] == "us-east-1"
        fake_boto3.sts.assume_role.assert_not_called()

    def test_host_role_mode_ignores_leftover_keys(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        provider = AWSProvider(
            aws_connector,
            _credential({"auth_mode": "host_role", "access_key": "stale", "secret_key": "stale"}),
        )

        provider._get_boto3_clients()

        assert "aws_access_key_id" not in fake_boto3.sessions[0].kwargs

    def test_role_arn_is_assumed_from_base_identity(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        credential = _credential(
            {
                "auth_mode": "access_keys",
                "access_key": "AKIA",
                "secret_key": "secret",
                "role_arn": "arn:aws:iam::123456789012:role/octoprox",
                "external_id": "corp-42",
            }
        )
        provider = AWSProvider(aws_connector, credential)

        client, _ = provider._get_boto3_clients()

        # Building the session makes no network call; the first use does.
        fake_boto3.sts.assume_role.assert_not_called()
        base, assumed = fake_boto3.sessions
        assert base.kwargs["aws_access_key_id"] == "AKIA"
        assert assumed.kwargs["region_name"] == "us-east-1"
        assert client == ("client", "ec2", assumed.kwargs)

        credentials = _assumed_credentials(fake_boto3)
        assert isinstance(credentials, DeferredRefreshableCredentials)
        frozen = credentials.get_frozen_credentials()

        fake_boto3.sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/octoprox",
            RoleSessionName="octoprox-test-aws-credential",
            ExternalId="corp-42",
        )
        # The STS call itself was signed with the base identity.
        assert fake_boto3.sts.created_with["aws_access_key_id"] == "AKIA"
        assert (frozen.access_key, frozen.secret_key, frozen.token) == ("ASIATEMPKEY", "tempsecret", "temptoken")

    def test_host_role_assuming_a_role_uses_host_identity_for_sts(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        credential = _credential(
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:role/octoprox"}
        )
        provider = AWSProvider(aws_connector, credential)

        provider._get_boto3_clients()
        _assumed_credentials(fake_boto3).get_frozen_credentials()

        kwargs = fake_boto3.sts.assume_role.call_args.kwargs
        assert "ExternalId" not in kwargs
        assert fake_boto3.sts.created_with["aws_access_key_id"] == "AKIAHOST"

    def test_assumed_credentials_are_cached_until_refresh_is_due(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        credential = _credential(
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:role/octoprox"}
        )
        provider = AWSProvider(aws_connector, credential)
        provider._get_boto3_clients()
        credentials = _assumed_credentials(fake_boto3)

        credentials.get_frozen_credentials()
        credentials.get_frozen_credentials()

        assert fake_boto3.sts.assume_role.call_count == 1

    def test_assumed_credentials_refresh_when_inside_the_mandatory_window(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        fake_boto3.sts.assume_role.return_value["Credentials"]["Expiration"] = datetime.now(UTC) + timedelta(minutes=5)
        credential = _credential(
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:role/octoprox"}
        )
        provider = AWSProvider(aws_connector, credential)
        provider._get_boto3_clients()
        credentials = _assumed_credentials(fake_boto3)

        credentials.get_frozen_credentials()
        credentials.get_frozen_credentials()

        assert fake_boto3.sts.assume_role.call_count == 2

    def test_role_without_host_identity_fails_clearly(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        fake_boto3.host_credentials = None
        credential = _credential(
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:role/octoprox"}
        )
        provider = AWSProvider(aws_connector, credential)

        with pytest.raises(NoCredentialsError):
            provider._get_boto3_clients()

    def test_clients_are_cached(self, aws_connector: Connector, fake_boto3: _FakeBoto3) -> None:
        provider = AWSProvider(aws_connector, _credential({"auth_mode": "host_role"}))

        first = provider._get_boto3_clients()
        second = provider._get_boto3_clients()

        assert first == second
        assert len(fake_boto3.sessions) == 1

    def test_role_session_name_is_sanitised_and_bounded(self, aws_connector: Connector) -> None:
        provider = AWSProvider(aws_connector, _credential({}, credential_id="has space/and:colon" + "x" * 80))

        name = provider._role_session_name()

        assert name.startswith("octoprox-has-space-and-colon")
        assert len(name) == 64

    def test_assume_role_failure_propagates(
        self, aws_connector: Connector, fake_boto3: _FakeBoto3
    ) -> None:
        """A rejected AssumeRole surfaces to the auto scaler instead of being swallowed."""
        fake_boto3.sts.assume_role.side_effect = RuntimeError("AccessDenied: not authorized to assume")
        credential = _credential(
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:role/octoprox"}
        )
        provider = AWSProvider(aws_connector, credential)
        provider._get_boto3_clients()

        with pytest.raises(RuntimeError, match="AccessDenied"):
            _assumed_credentials(fake_boto3).get_frozen_credentials()

    def test_missing_boto3_is_an_error(self, aws_connector: Connector, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scale job records it on the connector, rather than reading a missing SDK as nothing to do."""
        monkeypatch.setitem(sys.modules, "boto3", None)
        provider = AWSProvider(aws_connector, _credential({"auth_mode": "host_role"}))

        with pytest.raises(RuntimeError, match="boto3 is not installed"):
            provider._get_boto3_clients()


class TestGCPProviderCredentials:
    """How the GCP provider turns a credential's auth mode into google-auth credentials."""

    @pytest.fixture
    def gcp_auth(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        mocks = SimpleNamespace(
            default=MagicMock(return_value=("host-creds", "host-project")),
            from_info=MagicMock(return_value="key-creds"),
            impersonated=MagicMock(return_value="impersonated-creds"),
            instances_client=MagicMock(side_effect=lambda credentials: ("instances", credentials)),
        )
        monkeypatch.setattr("google.auth.default", mocks.default)
        monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info", mocks.from_info)
        monkeypatch.setattr("google.auth.impersonated_credentials.Credentials", mocks.impersonated)
        monkeypatch.setattr("google.cloud.compute_v1.InstancesClient", mocks.instances_client)
        return mocks

    @staticmethod
    def _credential(config: dict[str, Any]) -> Credential:
        return Credential(id="gcp-cred", name="GCP", type=CredentialType.GCP, project_id="p", config=config)

    def test_key_mode_uses_stored_key(self, gcp_connector: Connector, gcp_auth: SimpleNamespace) -> None:
        key = '{"type": "service_account", "project_id": "test"}'
        provider = GCPProvider(gcp_connector, self._credential({"auth_mode": "service_account_key", "service_account_json": key, "project_id": "test"}))

        client = provider._get_compute_client()

        gcp_auth.from_info.assert_called_once_with(
            {"type": "service_account", "project_id": "test"},
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        gcp_auth.default.assert_not_called()
        assert client == ("instances", "key-creds")

    def test_legacy_config_without_auth_mode_uses_stored_key(
        self, gcp_connector: Connector, gcp_credential: Credential, gcp_auth: SimpleNamespace
    ) -> None:
        GCPProvider(gcp_connector, gcp_credential)._get_compute_client()

        gcp_auth.from_info.assert_called_once()
        gcp_auth.default.assert_not_called()

    def test_host_identity_uses_application_default_credentials(
        self, gcp_connector: Connector, gcp_auth: SimpleNamespace
    ) -> None:
        provider = GCPProvider(gcp_connector, self._credential({"auth_mode": "host_identity"}))

        client = provider._get_compute_client()

        gcp_auth.default.assert_called_once_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        gcp_auth.from_info.assert_not_called()
        assert client == ("instances", "host-creds")

    def test_host_identity_ignores_leftover_key(self, gcp_connector: Connector, gcp_auth: SimpleNamespace) -> None:
        provider = GCPProvider(gcp_connector, self._credential({"auth_mode": "host_identity", "service_account_json": "{}"}))

        provider._get_compute_client()

        gcp_auth.from_info.assert_not_called()
        gcp_auth.default.assert_called_once()

    def test_key_mode_without_key_never_uses_host_identity(
        self, gcp_connector: Connector, gcp_auth: SimpleNamespace
    ) -> None:
        provider = GCPProvider(gcp_connector, self._credential({"auth_mode": "service_account_key"}))

        with pytest.raises(ValueError, match="service_account_key mode"):
            provider._get_compute_client()
        gcp_auth.default.assert_not_called()

    def test_impersonation_wraps_the_base_identity(self, gcp_connector: Connector, gcp_auth: SimpleNamespace) -> None:
        provider = GCPProvider(
            gcp_connector,
            self._credential({"auth_mode": "host_identity", "impersonate_service_account": "octoprox@target.iam.gserviceaccount.com"}),
        )

        client = provider._get_compute_client()

        gcp_auth.impersonated.assert_called_once_with(
            source_credentials="host-creds",
            target_principal="octoprox@target.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        assert client == ("instances", "impersonated-creds")

    def test_client_is_cached(self, gcp_connector: Connector, gcp_auth: SimpleNamespace) -> None:
        provider = GCPProvider(gcp_connector, self._credential({"auth_mode": "host_identity"}))

        first = provider._get_compute_client()
        second = provider._get_compute_client()

        assert first == second
        assert gcp_auth.default.call_count == 1


class TestAzureProviderCredentials:
    """How the Azure provider turns a credential's auth mode into an azure-identity credential."""

    @pytest.fixture
    def azure_sdk(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        host = MagicMock(name="host-credential")
        host.get_token.return_value = SimpleNamespace(token="host-jwt")
        mocks = SimpleNamespace(
            host=host,
            default=MagicMock(return_value=host),
            secret=MagicMock(return_value="secret-credential"),
            assertion=MagicMock(return_value="federated-credential"),
            compute=MagicMock(side_effect=lambda credential, subscription: ("compute", credential, subscription)),
            network=MagicMock(side_effect=lambda credential, subscription: ("network", credential, subscription)),
        )
        monkeypatch.setattr("azure.identity.DefaultAzureCredential", mocks.default)
        monkeypatch.setattr("azure.identity.ClientSecretCredential", mocks.secret)
        monkeypatch.setattr("azure.identity.ClientAssertionCredential", mocks.assertion)
        monkeypatch.setattr("azure.mgmt.compute.ComputeManagementClient", mocks.compute)
        monkeypatch.setattr("azure.mgmt.network.NetworkManagementClient", mocks.network)
        return mocks

    @staticmethod
    def _credential(config: dict[str, Any]) -> Credential:
        return Credential(id="azure-cred", name="Azure", type=CredentialType.AZURE, project_id="p", config=config)

    def test_secret_mode_uses_stored_secret(self, azure_connector: Connector, azure_sdk: SimpleNamespace) -> None:
        provider = AzureProvider(
            azure_connector,
            self._credential({"auth_mode": "client_secret", "tenant_id": "t", "client_id": "c", "client_secret": "s"}),
        )

        compute, network = provider._get_azure_clients()

        azure_sdk.secret.assert_called_once_with(tenant_id="t", client_id="c", client_secret="s")
        azure_sdk.default.assert_not_called()
        # The subscription comes from the connector, as before.
        assert compute == ("compute", "secret-credential", "sub-12345")
        assert network == ("network", "secret-credential", "sub-12345")

    def test_legacy_config_without_auth_mode_uses_stored_secret(
        self, azure_connector: Connector, azure_credential: Credential, azure_sdk: SimpleNamespace
    ) -> None:
        AzureProvider(azure_connector, azure_credential)._get_azure_clients()

        azure_sdk.secret.assert_called_once()
        azure_sdk.default.assert_not_called()

    def test_host_identity_uses_default_chain(self, azure_connector: Connector, azure_sdk: SimpleNamespace) -> None:
        provider = AzureProvider(azure_connector, self._credential({"auth_mode": "host_identity"}))

        compute, _ = provider._get_azure_clients()

        azure_sdk.default.assert_called_once_with()
        azure_sdk.secret.assert_not_called()
        assert compute == ("compute", azure_sdk.host, "sub-12345")

    def test_host_identity_can_pick_a_user_assigned_identity(
        self, azure_connector: Connector, azure_sdk: SimpleNamespace
    ) -> None:
        identity = "11111111-2222-3333-4444-555555555555"
        provider = AzureProvider(
            azure_connector, self._credential({"auth_mode": "host_identity", "managed_identity_client_id": identity})
        )

        provider._get_azure_clients()

        azure_sdk.default.assert_called_once_with(
            managed_identity_client_id=identity, workload_identity_client_id=identity
        )

    def test_host_identity_ignores_leftover_secret(self, azure_connector: Connector, azure_sdk: SimpleNamespace) -> None:
        provider = AzureProvider(
            azure_connector,
            self._credential({"auth_mode": "host_identity", "tenant_id": "t", "client_id": "c", "client_secret": "stale"}),
        )

        provider._get_azure_clients()

        azure_sdk.secret.assert_not_called()

    def test_secret_mode_without_secret_never_uses_host_identity(
        self, azure_connector: Connector, azure_sdk: SimpleNamespace
    ) -> None:
        provider = AzureProvider(azure_connector, self._credential({"auth_mode": "client_secret", "tenant_id": "t"}))

        with pytest.raises(ValueError, match="client_secret mode"):
            provider._get_azure_clients()
        azure_sdk.default.assert_not_called()

    def test_federation_exchanges_the_host_token_for_the_target_app(
        self, azure_connector: Connector, azure_sdk: SimpleNamespace
    ) -> None:
        provider = AzureProvider(
            azure_connector,
            self._credential(
                {
                    "auth_mode": "host_identity",
                    "target_tenant_id": "customer-tenant",
                    "target_client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                }
            ),
        )

        compute, _ = provider._get_azure_clients()

        azure_sdk.assertion.assert_called_once()
        kwargs = azure_sdk.assertion.call_args.kwargs
        assert kwargs["tenant_id"] == "customer-tenant"
        assert kwargs["client_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        # The assertion callback hands azure-identity a fresh host token each time.
        assert kwargs["func"]() == "host-jwt"
        azure_sdk.host.get_token.assert_called_with("api://AzureADTokenExchange/.default")
        assert compute == ("compute", "federated-credential", "sub-12345")

    def test_clients_are_cached(self, azure_connector: Connector, azure_sdk: SimpleNamespace) -> None:
        provider = AzureProvider(azure_connector, self._credential({"auth_mode": "host_identity"}))

        first = provider._get_azure_clients()
        second = provider._get_azure_clients()

        assert first == second
        assert azure_sdk.default.call_count == 1




def _named(connector: Connector, instance_name: str, **config: Any) -> Connector:
    """The connector with its instances named from ``instance_name``, and ``config`` merged into its config."""
    return connector.model_copy(update={"config": {**connector.config, "instance_name": instance_name, **config}})


class TestCloudProviderOf:
    def test_each_cloud_credential_type_gets_its_provider_and_any_other_none(
        self,
        aws_connector: Connector,
        aws_credential: Credential,
        gcp_connector: Connector,
        gcp_credential: Credential,
        azure_connector: Connector,
        azure_credential: Credential,
    ) -> None:
        assert isinstance(CloudProvider.of(aws_connector, aws_credential), AWSProvider)
        assert isinstance(CloudProvider.of(gcp_connector, gcp_credential), GCPProvider)
        assert isinstance(CloudProvider.of(azure_connector, azure_credential), AzureProvider)
        static = Credential(name="s", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p", config={})
        assert CloudProvider.of(aws_connector, static) is None


class TestCreationName:
    def test_a_long_prefix_is_cut_so_the_name_fits_each_clouds_limit(
        self,
        aws_connector: Connector,
        aws_credential: Credential,
        gcp_connector: Connector,
        gcp_credential: Credential,
        azure_connector: Connector,
        azure_credential: Credential,
    ) -> None:
        long = "p" * 80
        providers: list[tuple[CloudProvider, int]] = [
            (AWSProvider(_named(aws_connector, long), aws_credential), 64),
            (GCPProvider(_named(gcp_connector, long), gcp_credential), 63),
            (AzureProvider(_named(azure_connector, long), azure_credential), 64),
        ]
        for provider, limit in providers:
            name = provider.creation_name(1_700_000_000.9)
            assert len(name) == limit, type(provider).__name__
            assert re.fullmatch(r"p+-1700000000-[0-9a-f]{6}", name)

    def test_a_short_prefix_is_kept_whole_and_every_name_is_new(
        self, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        provider = AWSProvider(aws_connector, aws_credential)
        names = {provider.creation_name(1_700_000_000.0) for _ in range(5)}
        assert len(names) == 5
        assert all(re.fullmatch(r"test-proxy-1700000000-[0-9a-f]{6}", name) for name in names)


# --- AWS ----------------------------------------------------------------------------------


def _token(proxy: Proxy) -> str:
    """The base64 of the proxy's ``username:password``, which its Squid matches the Proxy-Authorization header against."""
    return base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()


def _in_start_up_script(proxy: Proxy, script: str) -> bool:
    """Whether a start-up script holds the proxy's credentials, as the regex its Squid matches (+ escaped)."""
    return _token(proxy).replace("+", "\\+") in script


def _carries_credentials(proxy: Proxy, request: Any) -> bool:
    """Whether ``request`` holds the proxy's password, or the base64 of its credentials, anywhere in it."""
    text = json.dumps(request, default=str)
    return bool(proxy.password and proxy.password in text) or _token(proxy) in text


def _aws_instance(connector: Connector, name: str | None, instance_id: str, *, state: str = "running") -> dict[str, Any]:
    """An instance as DescribeInstances describes it, launched under the client token ``name`` (None: launched without)."""
    instance: dict[str, Any] = {
        "InstanceId": instance_id,
        "State": {"Name": state, "Code": 16 if state == "running" else 0},
        "PrivateIpAddress": "10.0.0.5",
        "PublicIpAddress": "203.0.113.5",
        "Tags": [{"Key": CONNECTOR_TAG, "Value": connector.id}],
    }
    if name is not None:
        instance["ClientToken"] = name
    return instance


_LIVE_STATES = {"Name": "instance-state-name", "Values": ["pending", "running"]}


class TestAWSInstances:
    """What the AWS provider asks EC2, on a real client whose answers are stubbed."""

    @pytest.fixture
    def ec2(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
        resource = boto3.resource("ec2", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s")
        stubber = Stubber(resource.meta.client)
        monkeypatch.setattr(AWSProvider, "_get_boto3_clients", lambda self: (resource.meta.client, resource))
        with stubber:
            yield stubber
            stubber.assert_no_pending_responses()

    async def test_create_launches_under_the_name_as_client_token_tagged_with_the_connector_alone(
        self, ec2: Stubber, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        launched: dict[str, Any] = {}
        ec2.client.meta.events.register(
            "before-parameter-build.ec2.RunInstances", lambda params, **kwargs: launched.update(params)
        )
        name = "test-proxy-1700000000-abcdef"
        running = {"Reservations": [{"Instances": [_aws_instance(aws_connector, name, "i-0abc")]}]}
        ec2.add_response("run_instances", {"Instances": [{"InstanceId": "i-0abc"}]})
        ec2.add_response("describe_instances", running, {"InstanceIds": ["i-0abc"]})
        ec2.add_response("describe_instances", running, {"InstanceIds": ["i-0abc"]})
        connector = _named(aws_connector, "test-proxy", tags={"team": "scraping", "Name": "mine", CONNECTOR_TAG: "x"})

        proxy = await AWSProvider(connector, aws_credential).create_instance(name)

        assert launched["ClientToken"] == name
        (spec,) = launched["TagSpecifications"]
        tags = {tag["Key"]: tag["Value"] for tag in spec["Tags"]}
        assert tags == {"ManagedBy": "octoprox", "team": "scraping", "Name": name, CONNECTOR_TAG: connector.id}
        # The credentials are in the user data, the instance's start-up script, and nowhere else.
        assert _in_start_up_script(proxy, base64.b64decode(launched["UserData"]).decode())
        assert not _carries_credentials(proxy, {key: value for key, value in launched.items() if key != "UserData"})
        assert (proxy.id, proxy.host, proxy.port, proxy.status) == ("aws-i-0abc", "203.0.113.5", 3128, ProxyStatus.INITIALIZING)
        assert (proxy.connector_id, proxy.metadata) == (connector.id, {"instance_id": "i-0abc"})

    async def test_create_of_an_instance_running_without_an_address_is_an_error(
        self, ec2: Stubber, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        bare = _aws_instance(aws_connector, "x-1-abcdef", "i-0abc")
        del bare["PublicIpAddress"], bare["PrivateIpAddress"]
        ec2.add_response("run_instances", {"Instances": [{"InstanceId": "i-0abc"}]})
        ec2.add_response("describe_instances", {"Reservations": [{"Instances": [bare]}]})
        ec2.add_response("describe_instances", {"Reservations": [{"Instances": [bare]}]})
        with pytest.raises(RuntimeError, match="i-0abc runs without an address"):
            await AWSProvider(aws_connector, aws_credential).create_instance("x-1-abcdef")

    async def test_create_without_an_image_for_the_region_is_an_error(
        self, ec2: Stubber, aws_credential: Credential, aws_connector: Connector
    ) -> None:
        with pytest.raises(ValueError, match="No Ubuntu image"):
            await AWSProvider(_named(aws_connector, "x", region="invalid-region-1"), aws_credential).create_instance("x-1-abcdef")

    async def test_tagged_instances_reads_every_page_of_the_connectors_live_instances(
        self, ec2: Stubber, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        tagged = {"Name": f"tag:{CONNECTOR_TAG}", "Values": [aws_connector.id]}
        first = {
            "Reservations": [{"Instances": [_aws_instance(aws_connector, "a-1-aaaaaa", "i-a")]}],
            "NextToken": "page-2",
        }
        second = {
            "Reservations": [
                {"Instances": [_aws_instance(aws_connector, "b-1-bbbbbb", "i-b", state="pending")]},
                # Launched by hand with the tag: no client token.
                {"Instances": [_aws_instance(aws_connector, None, "i-c")]},
            ]
        }
        ec2.add_response("describe_instances", first, {"Filters": [tagged, _LIVE_STATES]})
        ec2.add_response("describe_instances", second, {"Filters": [tagged, _LIVE_STATES], "NextToken": "page-2"})

        listed = await AWSProvider(aws_connector, aws_credential).tagged_instances()

        # Stopping and terminated instances are filtered out by EC2; a pending one is listed with the running ones.
        assert listed == [
            CloudInstance("a-1-aaaaaa", "aws-i-a"),
            CloudInstance("b-1-bbbbbb", "aws-i-b"),
            CloudInstance("", "aws-i-c"),
        ]

    async def test_terminate_succeeds_for_an_instance_aws_no_longer_knows_and_raises_other_errors(
        self, ec2: Stubber, aws_connector: Connector, aws_credential: Credential
    ) -> None:
        provider = AWSProvider(aws_connector, aws_credential)
        stopping = {"TerminatingInstances": [{"InstanceId": "i-0abc", "CurrentState": {"Name": "shutting-down", "Code": 32}}]}
        ec2.add_response("terminate_instances", stopping, {"InstanceIds": ["i-0abc"]})
        ec2.add_client_error("terminate_instances", "InvalidInstanceID.NotFound", expected_params={"InstanceIds": ["i-0abc"]})
        ec2.add_client_error("terminate_instances", "UnauthorizedOperation", expected_params={"InstanceIds": ["i-0abc"]})

        await provider.terminate_instance("aws-i-0abc")
        await provider.terminate_instance("aws-i-0abc")
        with pytest.raises(ClientError, match="UnauthorizedOperation"):
            await provider.terminate_instance("aws-i-0abc")


# --- GCP ----------------------------------------------------------------------------------


class _Operation:
    """An extended operation of Compute Engine, done at once or failed with ``error``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.waited = False

    def result(self, timeout: float | None = None) -> None:
        self.waited = True
        if self.error is not None:
            raise self.error


class _Instances:
    """Compute Engine's instances client for one zone: its instances by name, and every operation it handed out."""

    def __init__(self) -> None:
        self.instances: dict[str, compute_v1.Instance] = {}
        self.operations: list[_Operation] = []
        # Each listing's filter, and the project and zone it was asked in.
        self.filters: list[str] = []
        self.listed_in: list[tuple[str, str]] = []
        self.insert_error: Exception | None = None
        self.delete_error: Exception | None = None
        # The external IP an inserted instance gets.
        self.address = "34.1.2.3"

    def put(self, name: str, status: str, *, nat_ip: str = "", labels: dict[str, str] | None = None) -> None:
        """Hold an instance as the zone reports it."""
        self.instances[name] = compute_v1.Instance(
            name=name,
            status=status,
            labels=labels or {},
            network_interfaces=[
                compute_v1.NetworkInterface(
                    network_i_p="10.128.0.2" if status == "RUNNING" else "",
                    access_configs=[compute_v1.AccessConfig(nat_i_p=nat_ip)],
                )
            ],
        )

    def _operation(self, error: Exception | None) -> _Operation:
        self.operations.append(_Operation(error))
        return self.operations[-1]

    def insert(self, *, project: str, zone: str, instance_resource: compute_v1.Instance) -> _Operation:
        created = compute_v1.Instance(instance_resource)
        created.status = "RUNNING"
        created.network_interfaces[0].access_configs[0].nat_i_p = self.address
        self.instances[created.name] = created
        return self._operation(self.insert_error)

    def get(self, *, project: str, zone: str, instance: str) -> compute_v1.Instance:
        if instance not in self.instances:
            raise NotFound(f"instance {instance}")
        return self.instances[instance]

    def list(self, request: compute_v1.ListInstancesRequest) -> list[compute_v1.Instance]:
        self.listed_in.append((request.project, request.zone))
        self.filters.append(request.filter)
        return list(self.instances.values())

    def delete(self, *, project: str, zone: str, instance: str) -> _Operation:
        if instance not in self.instances:
            raise NotFound(f"instance {instance}")
        if self.delete_error is None:
            self.instances.pop(instance)
        return self._operation(self.delete_error)


class TestGCPInstances:
    """What the GCP provider asks Compute Engine; the client answers with the SDK's own instance messages."""

    @pytest.fixture
    def zone(self, monkeypatch: pytest.MonkeyPatch) -> _Instances:
        instances = _Instances()
        monkeypatch.setattr(GCPProvider, "_get_compute_client", lambda self: instances)
        return instances

    async def test_create_names_and_labels_the_instance_with_its_credentials_in_the_start_up_script_alone(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        connector = _named(gcp_connector, "test-proxy", tags={"Team_Name": "Scraping", CONNECTOR_TAG: "x"})
        name = "test-proxy-1700000000-abcdef"

        proxy = await GCPProvider(connector, gcp_credential).create_instance(name)

        created = compute_v1.Instance.to_dict(zone.instances[name])
        assert created["labels"] == {"managed-by": "octoprox", "team-name": "scraping", CONNECTOR_TAG: connector.id}
        [item] = created["metadata"]["items"]
        assert item["key"] == "startup-script" and _in_start_up_script(proxy, item["value"])
        item["value"] = ""
        assert not _carries_credentials(proxy, created)
        assert [operation.waited for operation in zone.operations] == [True]
        assert (proxy.id, proxy.host, proxy.metadata) == (f"gcp-{name}", "34.1.2.3", {"instance_name": name})

    async def test_create_raises_the_error_its_insert_ended_with(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        zone.insert_error = Forbidden("Quota 'CPUS' exceeded")
        with pytest.raises(Forbidden, match="Quota"):
            await GCPProvider(gcp_connector, gcp_credential).create_instance("test-proxy-1-abcdef")

    async def test_create_of_an_instance_running_without_an_address_is_an_error(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        zone.address = ""
        with pytest.raises(RuntimeError, match="test-proxy-1-abcdef runs without an address"):
            await GCPProvider(gcp_connector, gcp_credential).create_instance("test-proxy-1-abcdef")

    async def test_a_missing_sdk_is_an_error(
        self, gcp_connector: Connector, gcp_credential: Credential, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr("google.cloud.compute_v1")
        monkeypatch.setitem(sys.modules, "google.cloud.compute_v1", None)
        with pytest.raises(RuntimeError, match="google-cloud-compute is not installed"):
            await GCPProvider(gcp_connector, gcp_credential).tagged_instances()

    async def test_tagged_instances_filters_on_the_connector_label_and_skips_stopped_ones(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        zone.put("a", "RUNNING", nat_ip="34.1.2.3")
        zone.put("b", "STAGING")
        zone.put("c", "STOPPING", nat_ip="34.1.2.4")
        zone.put("d", "TERMINATED")
        zone.put("e", "PROVISIONING")

        listed = await GCPProvider(gcp_connector, gcp_credential).tagged_instances()

        assert zone.filters == [f'labels.{CONNECTOR_TAG} = "{gcp_connector.id}"']
        assert zone.listed_in == [("my-gcp-project", gcp_connector.config["zone"])]
        assert listed == [CloudInstance("a", "gcp-a"), CloudInstance("b", "gcp-b"), CloudInstance("e", "gcp-e")]

    @pytest.mark.parametrize("method", ["insert", "get", "list", "delete"])
    def test_the_stand_in_takes_only_what_the_sdk_client_takes(self, method: str) -> None:
        """A call the stand-in answers must be one the SDK's client accepts too: a listing once passed a filter the SDK refuses, and the stand-in took it."""
        sdk = inspect.signature(getattr(compute_v1.InstancesClient, method)).parameters
        stand_in = inspect.signature(getattr(_Instances, method)).parameters
        assert set(stand_in) - {"self"} <= set(sdk)

    async def test_terminate_waits_for_the_delete_and_counts_a_missing_or_stopping_instance_as_terminated(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        provider = GCPProvider(gcp_connector, gcp_credential)
        zone.put("a", "RUNNING", nat_ip="34.1.2.3")
        await provider.terminate_instance("gcp-a")
        assert "a" not in zone.instances and [operation.waited for operation in zone.operations] == [True]

        await provider.terminate_instance("gcp-a")

        # Another run's delete holds it: the instance is stopping.
        zone.put("b", "STOPPING")
        zone.delete_error = BadRequest("The resource is not ready")
        await provider.terminate_instance("gcp-b")

    async def test_terminate_raises_when_a_delete_fails_on_an_instance_still_running(
        self, zone: _Instances, gcp_connector: Connector, gcp_credential: Credential
    ) -> None:
        zone.put("a", "RUNNING", nat_ip="34.1.2.3")
        zone.delete_error = Forbidden("compute.instances.delete denied")
        with pytest.raises(Forbidden):
            await GCPProvider(gcp_connector, gcp_credential).terminate_instance("gcp-a")


# --- Azure --------------------------------------------------------------------------------

# The resource kinds the provider lists in its resource group.
_ARM_LISTED = frozenset({"virtualMachines", "publicIPAddresses", "networkInterfaces"})


class _Arm(requests.adapters.BaseAdapter):
    """Azure Resource Manager on a ``requests`` adapter: it holds resources by kind and name, and keeps each request.

    A PUT stores its body as provisioned, a GET reads one resource or lists a kind, and a DELETE removes one, or answers
    with the status ``delete_status`` names for it, or raises the error ``delete_errors`` names for it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str, Any]] = []
        self.resources: dict[tuple[str, str], dict[str, Any]] = {}
        self.delete_status: dict[str, int] = {}
        self.delete_errors: dict[str, Exception] = {}
        # Statuses a PUT of a kind answers with instead, and the IP a public IP gets once created.
        self.put_status: dict[str, int] = {}
        self.pip_address: str | None = "203.0.113.9"

    def put(self, kind: str, name: str, *, tags: dict[str, str] | None = None, **properties: Any) -> None:
        """Hold a provisioned resource, as another create left it."""
        self.resources[(kind, name)] = {
            "id": f"/rg/{kind}/{name}",
            "name": name,
            "location": "eastus",
            "tags": tags or {},
            "properties": {"provisioningState": "Succeeded", **properties},
        }

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        path = urlsplit(request.url).path
        body = json.loads(request.body) if request.body else None
        self.sent.append((str(request.method), path, body))
        segments = path.rstrip("/").split("/")
        if segments[-1] in _ARM_LISTED:
            listed = [resource for (kind, _name), resource in self.resources.items() if kind == segments[-1]]
            return self._answer(request, 200, {"value": listed})
        kind, name = segments[-2], segments[-1]
        if request.method == "PUT":
            if (status := self.put_status.get(kind)) is not None:
                return self._answer(request, status, {"error": {"code": str(status), "message": name}})
            properties = {**(body or {}).get("properties", {}), "provisioningState": "Succeeded"}
            if kind == "publicIPAddresses" and self.pip_address is not None:
                properties["ipAddress"] = self.pip_address
            elif kind == "virtualMachines":
                properties["vmId"] = "vm-guid"
            self.resources[(kind, name)] = {**(body or {}), "id": path, "name": name, "properties": properties}
            return self._answer(request, 200, self.resources[(kind, name)])
        if request.method == "DELETE":
            if (error := self.delete_errors.get(name)) is not None:
                raise error
            if (status := self.delete_status.get(name)) is not None:
                return self._answer(request, status, {"error": {"code": str(status), "message": name}})
            self.resources.pop((kind, name), None)
            return self._answer(request, 204, None)
        found = self.resources.get((kind, name))
        if found is None and kind in _ARM_LISTED:
            return self._answer(request, 404, {"error": {"code": "ResourceNotFound", "message": name}})
        return self._answer(request, 200, found or {"id": path, "name": name, "properties": {}})

    @staticmethod
    def _answer(request: requests.PreparedRequest, status: int, body: Any) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        if body is not None:
            response.headers["Content-Type"] = "application/json"
        response.raw = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
        response.request = request
        response.url = request.url
        return response

    def close(self) -> None:
        pass


def _keys(value: Any) -> list[str]:
    """Every key of every mapping in ``value``, however deep."""
    if isinstance(value, dict):
        return [key for item_key, item in value.items() for key in (item_key, *_keys(item))]
    if isinstance(value, list):
        return [key for item in value for key in _keys(item)]
    return []


class TestAzureInstances:
    """What the Azure provider sends, through the real SDK on a transport that answers locally.

    The SDK sends a dict body as it is, without mapping attribute names, so each body must already be the REST API's
    JSON: camelCase keys, nested under ``properties``.
    """

    @pytest.fixture
    def arm(self, monkeypatch: pytest.MonkeyPatch) -> _Arm:
        from azure.mgmt.compute import ComputeManagementClient
        from azure.mgmt.network import NetworkManagementClient

        adapter = _Arm()
        session = requests.Session()
        session.mount("https://", adapter)
        transport = RequestsTransport(session=session, session_owner=False)
        monkeypatch.setattr(
            "azure.mgmt.compute.ComputeManagementClient", functools.partial(ComputeManagementClient, transport=transport)
        )
        monkeypatch.setattr(
            "azure.mgmt.network.NetworkManagementClient", functools.partial(NetworkManagementClient, transport=transport)
        )
        token = AccessToken("token", int(time.time()) + 3600)
        monkeypatch.setattr(
            AzureProvider, "_build_credential", lambda self: SimpleNamespace(get_token=lambda *scopes, **kwargs: token)
        )
        return adapter

    @pytest.fixture
    def connector(self, azure_connector: Connector) -> Connector:
        return _named(azure_connector, "test-proxy", vnet_name="vnet", subnet_name="subnet")

    def _vm(self, arm: _Arm, connector: Connector, name: str, *, state: str = "Succeeded", pip: str | None = "203.0.113.10") -> None:
        """A VM of the connector as its create leaves it: tagged, with its public IP."""
        arm.put("virtualMachines", name, tags={CONNECTOR_TAG: connector.id}, vmId=f"guid-{name}")
        arm.resources[("virtualMachines", name)]["properties"]["provisioningState"] = state
        if pip is not None:
            arm.put("publicIPAddresses", f"{name}-pip", ipAddress=pip)

    async def test_create_sends_rest_shaped_bodies_named_and_tagged_with_the_credentials_in_the_custom_data_alone(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        connector = _named(
            connector, "test-proxy", vnet_name="vnet", subnet_name="subnet", tags={"team": "scraping", CONNECTOR_TAG: "x"}
        )
        name = "test-proxy-1700000000-abcdef"

        proxy = await AzureProvider(connector, azure_credential).create_instance(name)

        assert (proxy.id, proxy.host) == (f"azure-{name}", "203.0.113.9")
        assert proxy.metadata == {"vm_name": name, "vm_id": "vm-guid"}
        bodies = {path.split("/")[-2]: (path, body) for method, path, body in arm.sent if method == "PUT"}
        assert set(bodies) == {"publicIPAddresses", "networkInterfaces", "virtualMachines"}
        for _path, body in bodies.values():
            assert not [key for key in _keys(body) if "_" in key], body
        assert bodies["publicIPAddresses"] == (
            bodies["publicIPAddresses"][0],
            {"location": "eastus", "sku": {"name": "Standard"}, "properties": {"publicIPAllocationMethod": "Static"}},
        )
        assert bodies["publicIPAddresses"][0].endswith(f"/publicIPAddresses/{name}-pip")
        nic_path, nic = bodies["networkInterfaces"]
        assert nic_path.endswith(f"/networkInterfaces/{name}-nic")
        (ip_configuration,) = nic["properties"]["ipConfigurations"]
        assert ip_configuration["properties"]["subnet"]["id"].endswith("/virtualNetworks/vnet/subnets/subnet")
        assert ip_configuration["properties"]["publicIPAddress"]["id"].endswith(f"{name}-pip")
        vm_path, vm_body = bodies["virtualMachines"]
        assert vm_path.endswith(f"/virtualMachines/{name}")
        assert vm_body["tags"] == {"managed-by": "octoprox", "team": "scraping", CONNECTOR_TAG: connector.id}
        vm = vm_body["properties"]
        assert vm["hardwareProfile"] == {"vmSize": "Standard_B2ls_v2"}
        assert vm["storageProfile"]["osDisk"]["name"] == f"{name}-osdisk"
        assert vm["storageProfile"]["osDisk"]["managedDisk"] == {"storageAccountType": "Standard_LRS"}
        assert vm["osProfile"]["computerName"] == name
        assert vm["osProfile"]["linuxConfiguration"]["ssh"]["publicKeys"][0]["keyData"] == connector.config["ssh_public_key"]
        (interface,) = vm["networkProfile"]["networkInterfaces"]
        assert interface["id"].endswith(f"{name}-nic") and interface["properties"] == {"primary": True}
        # The credentials are in the custom data, the VM's start-up script, and nowhere else.
        assert _in_start_up_script(proxy, base64.b64decode(vm["osProfile"].pop("customData")).decode())
        assert not _carries_credentials(proxy, [body for _path, body in bodies.values()])

    async def test_a_create_that_fails_deletes_the_interface_and_public_ip_it_made(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        arm.put_status["virtualMachines"] = 400
        with pytest.raises(HttpResponseError):
            await AzureProvider(connector, azure_credential).create_instance("vm-a")
        deleted = [path.rsplit("/", 1)[-1] for method, path, _body in arm.sent if method == "DELETE"]
        assert deleted == ["vm-a-nic", "vm-a-pip"] and arm.resources == {}

    async def test_create_of_a_vm_without_an_address_or_a_network_is_an_error(
        self, arm: _Arm, connector: Connector, azure_connector: Connector, azure_credential: Credential
    ) -> None:
        arm.pip_address = None
        with pytest.raises(RuntimeError, match="vm-a runs without an address"):
            await AzureProvider(connector, azure_credential).create_instance("vm-a")
        with pytest.raises(ValueError, match="vnet_name and subnet_name"):
            await AzureProvider(azure_connector, azure_credential).create_instance("vm-b")

    async def test_a_missing_sdk_is_an_error(
        self, connector: Connector, azure_credential: Credential, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "azure.mgmt.compute", None)
        with pytest.raises(RuntimeError, match="the Azure SDK is not installed"):
            await AzureProvider(connector, azure_credential).tagged_instances()

    async def test_tagged_instances_lists_the_connectors_live_vms_from_the_vm_list_alone(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        self._vm(arm, connector, "vm-a")
        arm.put("networkInterfaces", "vm-a-nic", ipConfigurations=[{"name": "ip", "properties": {"privateIPAddress": "10.0.0.3"}}])
        self._vm(arm, connector, "vm-b", state="Deleting")
        self._vm(arm, connector.model_copy(update={"id": "another"}), "vm-c")
        arm.put("virtualMachines", "vm-d")
        self._vm(arm, connector, "vm-e", pip=None)
        self._vm(arm, connector, "vm-g", state="Creating", pip=None)
        self._vm(arm, connector, "vm-h", state="Failed")

        listed = await AzureProvider(connector, azure_credential).tagged_instances()

        assert listed == [
            CloudInstance("vm-a", "azure-vm-a"),
            CloudInstance("vm-e", "azure-vm-e"),
            CloudInstance("vm-g", "azure-vm-g"),
        ]
        assert [path.rsplit("/", 1)[-1] for _method, path, _body in arm.sent] == ["virtualMachines"]

    async def test_terminate_deletes_the_vm_then_its_interface_and_public_ip(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        self._vm(arm, connector, "vm-a")
        arm.put("networkInterfaces", "vm-a-nic")

        await AzureProvider(connector, azure_credential).terminate_instance("azure-vm-a")

        deleted = [path.rsplit("/", 1)[-1] for method, path, _body in arm.sent if method == "DELETE"]
        assert deleted == ["vm-a", "vm-a-nic", "vm-a-pip"]
        assert arm.resources == {}

    async def test_a_network_resource_left_after_a_vm_is_terminated_is_logged_not_raised(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        self._vm(arm, connector, "vm-a")
        arm.put("networkInterfaces", "vm-a-nic")
        arm.delete_status["vm-a-nic"] = 400

        await AzureProvider(connector, azure_credential).terminate_instance("azure-vm-a")

        assert list(arm.resources) == [("networkInterfaces", "vm-a-nic")]

    async def test_a_network_resource_cleanup_cut_by_a_deadline_is_logged_by_its_type(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type, and the termination stands."""
        self._vm(arm, connector, "vm-a")
        arm.put("networkInterfaces", "vm-a-nic")
        arm.delete_errors["vm-a-nic"] = TimeoutError()

        with patch("api.providers.cloud.logger") as log:
            await AzureProvider(connector, azure_credential).terminate_instance("azure-vm-a")

        event = "Failed to clean up a network resource of a terminated VM"
        failed = [call.kwargs for call in log.warning.call_args_list if call.args == (event,)]
        assert failed == [{"resource": "vm-a-nic", "error": "TimeoutError"}]

    async def test_terminate_counts_a_missing_vm_or_one_under_another_delete_as_terminated(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        provider = AzureProvider(connector, azure_credential)
        arm.delete_status["vm-gone"] = 404
        await provider.terminate_instance("azure-vm-gone")

        self._vm(arm, connector, "vm-b", state="Deleting")
        arm.delete_status["vm-b"] = 409
        await provider.terminate_instance("azure-vm-b")
        # The other delete removes the network resources once the VM is gone.
        assert ("publicIPAddresses", "vm-b-pip") in arm.resources

    async def test_terminate_raises_when_a_vm_that_is_not_being_deleted_refuses(
        self, arm: _Arm, connector: Connector, azure_credential: Credential
    ) -> None:
        self._vm(arm, connector, "vm-a")
        arm.delete_status["vm-a"] = 409
        with pytest.raises(HttpResponseError):
            await AzureProvider(connector, azure_credential).terminate_instance("azure-vm-a")
