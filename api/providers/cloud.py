# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Cloud providers: the AWS, GCP and Azure instances of a cloud connector, each a Squid proxy with credentials of its own.

Every instance carries its connector's ID as a tag, so a scale run can list them and terminate one whose row was never
saved; its credentials are in its start-up script alone.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import string
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import structlog

from api.core.logging import error_text
from api.models.connector import (
    AWSConnectorConfig,
    AzureConnectorConfig,
    CloudConnectorConfig,
    Connector,
    GCPConnectorConfig,
)
from api.models.credential import (
    AWSAuthMode,
    AzureAuthMode,
    Credential,
    CredentialType,
    GCPAuthMode,
)
from api.models.proxy import Proxy, ProxyStatus
from api.providers.base import ProxyProvider

logger = structlog.get_logger()

# The port Squid listens on, on every cloud.
PROXY_PORT = 3128
# The tag every instance carries (on GCP a label), its connector's ID, which a
# scale run lists the connector's instances by.
CONNECTOR_TAG = "octoprox-connector"

# OAuth scope the Compute Engine clients request on google-auth credentials.
_GCP_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# GCP instance statuses of an instance that is starting or running.
_GCP_LIVE = frozenset({"PROVISIONING", "STAGING", "RUNNING"})
# Scope of the host identity token exchanged for a federated app's token.
_AZURE_TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
# The provisioning state of an Azure VM on its way out, and those of a VM that
# is neither starting nor running for Octoprox.
_AZURE_DELETING = "Deleting"
_AZURE_DEAD = frozenset({_AZURE_DELETING, "Failed"})

# Cached Squid setup script template
_SQUID_SETUP_TEMPLATE: str | None = None


def _load_squid_setup_template() -> str:
    """Load the Squid setup script template from config file.

    Raises:
        FileNotFoundError: If the squid_setup.sh config file is not found.
    """
    global _SQUID_SETUP_TEMPLATE
    if _SQUID_SETUP_TEMPLATE is None:
        script_path = Path(__file__).parent.parent.parent / "config" / "squid_setup.sh"
        if not script_path.exists():
            raise FileNotFoundError(
                f"Squid setup script not found at {script_path}. "
                "This file is required for cloud provider instances."
            )
        _SQUID_SETUP_TEMPLATE = script_path.read_text()
    return _SQUID_SETUP_TEMPLATE


@dataclass(frozen=True)
class ProxyCredentials:
    """An instance's proxy credentials, random per instance and baked into its start-up script, where its Squid checks them."""

    username: str
    password: str

    @classmethod
    def generate(cls) -> ProxyCredentials:
        """New random credentials: a username of eight letters and digits, starting with ``u``, and a password of 24."""
        alphabet = string.ascii_letters + string.digits
        username = "u" + "".join(secrets.choice(alphabet) for _ in range(7))
        return cls(username, "".join(secrets.choice(alphabet) for _ in range(24)))


# Auth config: squid.conf directives for header-based authentication.
# Uses req_header ACL to match the exact Proxy-Authorization header value
# instead of auth_param basic, which avoids 407 challenge responses that
# reveal the server is a proxy. Invalid/missing auth gets TCP_RESET.
_AUTH_CONFIG = """\
# Header-based proxy authentication (no 407 challenges)
acl has_valid_auth req_header Proxy-Authorization ^Basic\\s+{auth_b64}$
http_access allow has_valid_auth
deny_info TCP_RESET all
http_access deny all"""

# No-auth config: allow all connections (original behavior)
_NO_AUTH_CONFIG = """\
# Allow all connections
http_access allow all"""


def build_squid_setup_script(credentials: ProxyCredentials | None = None) -> str:
    """The bash setup script of an instance's Squid, with its credentials baked in.

    Args:
        credentials: None lets every connection through.
    """
    template = _load_squid_setup_template()

    if credentials is not None:
        # Squid matches the Proxy-Authorization header against the base64 of
        # "username:password", + escaped: the one regex-special character base64 holds.
        auth_b64 = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode()
        auth_config = _AUTH_CONFIG.format(auth_b64=auth_b64.replace("+", "\\+"))
    else:
        auth_config = _NO_AUTH_CONFIG

    script = template.replace("##OCTOPROX_AUTH_CONFIG##", auth_config)
    return script


@dataclass(frozen=True)
class CloudInstance:
    """A connector's instance as its cloud lists it, booting or running: its creation name, and the ID its row has or would have."""

    name: str
    proxy_id: str


class CloudProvider(ProxyProvider, ABC):
    """A cloud connector's account: creates, lists and terminates its instances, each a proxy once it runs.

    Terminating is safe to repeat, so scale runs of one connector that overlap need not agree on who terminates.
    """

    # The longest creation name the cloud takes: its instance name limit, or
    # on AWS the client token's.
    NAME_LIMIT: ClassVar[int]
    # The start of an instance's proxy ID; the rest is the instance's key: its
    # ID on AWS and its name elsewhere.
    ID_PREFIX: ClassVar[str]
    # The tag an instance's row carries for its cloud, beside its place, and
    # the metadata field its key is kept in.
    ROW_TAG: ClassVar[str]
    KEY_FIELD: ClassVar[str]
    _config: AWSConnectorConfig | GCPConnectorConfig | AzureConnectorConfig

    @staticmethod
    def of(connector: Connector, credential: Credential) -> CloudProvider | None:
        """The cloud API of the credential's type; None for a type that is no cloud."""
        provider = _PROVIDERS.get(credential.type)
        return provider(connector, credential) if provider is not None else None

    @property
    def config(self) -> CloudConnectorConfig:
        """The connector's cloud config: its pool's size and rotation periods, and where and what to create."""
        return self._config

    @property
    @abstractmethod
    def place(self) -> str:
        """Where the connector's instances run: its region, zone or location, which their rows are tagged with."""

    def creation_name(self, now: float) -> str:
        """A new instance's name, ``<instance_name>-<epoch s>-<6 hex>``, its prefix cut so the whole fits ``NAME_LIMIT``."""
        suffix = f"-{int(now)}-{secrets.token_hex(3)}"
        return self._config.instance_name[: self.NAME_LIMIT - len(suffix)] + suffix

    @abstractmethod
    async def create_instance(self, name: str) -> Proxy:
        """Create the instance ``name``, tagged with the connector, and wait until it runs; its proxy, raising when the cloud refuses or fails."""

    @abstractmethod
    async def tagged_instances(self) -> list[CloudInstance]:
        """Every pending or running instance tagged with this connector."""

    @abstractmethod
    async def terminate_instance(self, proxy_id: str) -> None:
        """Terminate the instance ``proxy_id`` names, returning once the cloud reports it stopping; one gone or stopping already counts."""

    def _proxy(self, key: str, host: str, credentials: ProxyCredentials, **metadata: str | None) -> Proxy:
        """The row of the instance ``key`` as it is first saved, initializing until a health check judges it."""
        return Proxy(
            id=self._proxy_id(key),
            host=host,
            port=PROXY_PORT,
            username=credentials.username,
            password=credentials.password,
            connector_id=self.connector.id,
            status=ProxyStatus.INITIALIZING,
            tags=[self.ROW_TAG, self.place],
            metadata={self.KEY_FIELD: key, **metadata},
        )

    def _proxy_id(self, key: str) -> str:
        """The ID of the row of the instance ``key``: ``ID_PREFIX``, then the instance's ID on AWS or its name elsewhere."""
        return f"{self.ID_PREFIX}{key}"

    def _key(self, proxy_id: str) -> str:
        """The instance ``proxy_id`` names, as its cloud knows it: its ID on AWS, its name elsewhere."""
        return proxy_id.removeprefix(self.ID_PREFIX)


def _assumed_role_provider(fetcher: Any) -> Any:
    """botocore credential provider that yields refreshable credentials from ``fetcher``.

    Holds only the fetcher, so the provider does not keep the session-building scope alive for as long as the session
    exists.
    """
    from botocore.credentials import (  # type: ignore[import-untyped]
        CredentialProvider,
        DeferredRefreshableCredentials,
    )

    class AssumedRoleProvider(CredentialProvider):  # type: ignore[misc]
        METHOD = "assume-role"

        def __init__(self, fetcher: Any) -> None:
            super().__init__()
            self._fetcher = fetcher

        def load(self) -> Any:
            return DeferredRefreshableCredentials(
                refresh_using=self._fetcher.fetch_credentials, method=self.METHOD
            )

    return AssumedRoleProvider(fetcher)


class AWSProvider(CloudProvider):
    """EC2 instances in the connector's region, on its Ubuntu 24.04 image, through boto3 (the ``cloud`` extra).

    The credential's ``auth_mode`` picks the base identity, stored keys or the host's role, from which ``role_arn``,
    when set, is assumed.
    """

    # RunInstances takes a client token of up to 64 characters.
    NAME_LIMIT = 64
    ID_PREFIX = "aws-"
    ROW_TAG = "aws"
    KEY_FIELD = "instance_id"
    _config: AWSConnectorConfig

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, AWSConnectorConfig):
            raise ValueError("AWSProvider requires an AWS connector configuration")

        self._config = cloud_config
        self._ec2_client = None
        self._ec2_resource = None

    @property
    def place(self) -> str:
        """The connector's region."""
        return self._config.region

    def _role_session_name(self) -> str:
        """sts:AssumeRole session name: ``octoprox-<credential id>``, within AWS limits."""
        raw = f"octoprox-{self.credential.id if self.credential else 'default'}"
        return re.sub(r"[^\w+=,.@-]", "-", raw)[:64]

    def _build_session(self, boto3: Any) -> Any:
        """The boto3 session the EC2 clients are made from, on the base identity of the credential's auth mode.

        With a ``role_arn``, it carries botocore's refreshable assume-role credentials, which re-assume the role before
        they expire.
        """
        cred_config = self.credential.config if self.credential else {}
        region = self._config.region
        auth_mode = cred_config.get("auth_mode")
        access_key = cred_config.get("access_key")
        secret_key = cred_config.get("secret_key")

        # Configs saved before auth_mode existed carry no mode; they used the
        # stored keys when present and the host chain otherwise.
        has_keys = bool(access_key and secret_key)
        if auth_mode == AWSAuthMode.HOST_ROLE or (auth_mode is None and not has_keys):
            # Default credential chain: instance profile, task role, pod
            # identity, environment variables, shared config.
            base = boto3.Session(region_name=region)
        elif has_keys:
            base = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
            )
        else:
            # Explicit access_keys mode with no keys must never fall through
            # to the host identity: that would provision in the wrong account.
            raise ValueError("AWS credential is in access_keys mode but has no access_key/secret_key")

        role_arn = cred_config.get("role_arn")
        if not role_arn:
            return base

        import botocore.session  # type: ignore[import-untyped]
        from botocore.credentials import AssumeRoleCredentialFetcher
        from botocore.exceptions import NoCredentialsError  # type: ignore[import-untyped]

        source_credentials = base.get_credentials()
        if source_credentials is None:
            raise NoCredentialsError()

        extra_args: dict[str, Any] = {"RoleSessionName": self._role_session_name()}
        external_id = cred_config.get("external_id")
        if external_id:
            extra_args["ExternalId"] = external_id
        logger.debug("Assuming AWS role", role_arn=role_arn, connector_id=self.connector.id)
        fetcher = AssumeRoleCredentialFetcher(
            client_creator=base.client,
            source_credentials=source_credentials,
            role_arn=role_arn,
            extra_args=extra_args,
        )
        assumed_session = botocore.session.Session()
        # Put the assumed role at the head of the session's credential chain.
        # The resolver is botocore's extension point for custom credential
        # sources, so no private session state is touched.
        resolver = assumed_session.get_component("credential_provider")
        resolver.insert_before("env", _assumed_role_provider(fetcher))
        return boto3.Session(botocore_session=assumed_session, region_name=region)

    def _get_boto3_clients(self) -> tuple[Any, Any]:
        """The EC2 client and resource, made on first use; resolving the identity may call the metadata service, so callers on the event loop run this in a thread."""
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("boto3 is not installed: install octoprox[cloud]") from exc

        if self._ec2_client is None:
            session = self._build_session(boto3)
            self._ec2_client = session.client("ec2")
            self._ec2_resource = session.resource("ec2")
        return self._ec2_client, self._ec2_resource

    async def create_instance(self, name: str) -> Proxy:
        """Launch an EC2 instance named ``name``, its client token too, and wait until it runs."""
        ami_id = self._config.get_ami()
        if not ami_id:
            raise ValueError(
                f"No Ubuntu image for region {self._config.region} and instance type {self._config.instance_type}"
            )
        _, ec2_resource = await asyncio.to_thread(self._get_boto3_clients)
        credentials = ProxyCredentials.generate()
        # The connector's own tags never replace the name or the tag a scale run lists by.
        tags = {"ManagedBy": "octoprox", **self._config.tags, "Name": name, CONNECTOR_TAG: self.connector.id}
        run_args: dict[str, Any] = {
            "ImageId": ami_id,
            "InstanceType": self._config.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": build_squid_setup_script(credentials),
            # A launch retried with the same token gets the same instance back.
            "ClientToken": name,
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": [{"Key": key, "Value": str(value)} for key, value in tags.items()]}
            ],
        }
        # The security group determines which VPC the instance is launched in.
        security_group = self._config.security_group.strip()
        if security_group:
            run_args["SecurityGroupIds"] = [security_group]
        if self._config.key_pair_name:
            run_args["KeyName"] = self._config.key_pair_name
        logger.debug(
            "Creating EC2 instance", connector_id=self.connector.id, name=name, ami_id=ami_id,
            instance_type=self._config.instance_type,
        )

        def launch() -> Any:
            instance = ec2_resource.create_instances(**run_args)[0]
            instance.wait_until_running()
            instance.reload()
            return instance

        instance = await asyncio.to_thread(launch)
        host = instance.public_ip_address or instance.private_ip_address
        if not host:
            raise RuntimeError(f"AWS instance {instance.id} runs without an address")
        logger.info("Created AWS proxy instance", instance_id=instance.id, name=name, host=host)
        return self._proxy(instance.id, host, credentials)

    async def tagged_instances(self) -> list[CloudInstance]:
        """Every pending or running instance tagged with this connector, every page of them, each named by its client token (empty for one launched without)."""

        def listed() -> list[CloudInstance]:
            ec2_client, _ = self._get_boto3_clients()
            filters = [
                {"Name": f"tag:{CONNECTOR_TAG}", "Values": [self.connector.id]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ]
            return [
                CloudInstance(instance.get("ClientToken", ""), self._proxy_id(instance["InstanceId"]))
                for page in ec2_client.get_paginator("describe_instances").paginate(Filters=filters)
                for reservation in page["Reservations"]
                for instance in reservation["Instances"]
            ]

        return await asyncio.to_thread(listed)

    async def terminate_instance(self, proxy_id: str) -> None:
        """Terminate the instance; AWS answers once it is shutting down, and an instance it no longer knows counts as terminated."""
        instance_id = self._key(proxy_id)
        ec2_client, _ = await asyncio.to_thread(self._get_boto3_clients)
        from botocore.exceptions import ClientError

        try:
            await asyncio.to_thread(ec2_client.terminate_instances, InstanceIds=[instance_id])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "InvalidInstanceID.NotFound":
                raise
        logger.info("Terminated AWS instance", instance_id=instance_id)


class GCPProvider(CloudProvider):
    """Compute Engine instances in the connector's zone, on its Ubuntu 24.04 image, through google-cloud-compute (the ``cloud`` extra).

    The credential's ``auth_mode`` picks a stored key or the host's identity, which may impersonate a service account.
    """

    # Compute Engine instance names run to 63 characters.
    NAME_LIMIT = 63
    ID_PREFIX = "gcp-"
    ROW_TAG = "gcp"
    KEY_FIELD = "instance_name"
    _config: GCPConnectorConfig

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, GCPConnectorConfig):
            raise ValueError("GCPProvider requires a GCP connector configuration")

        self._config = cloud_config
        self._instances_client = None

    @property
    def place(self) -> str:
        """The connector's zone."""
        return self._config.zone

    def _build_credentials(self) -> Any:
        """The google-auth credentials the Compute Engine client uses, on the base identity of the credential's auth mode.

        With a service account to impersonate, they mint short-lived tokens for it through the IAM Credentials API and
        refresh them on their own.
        """
        import google.auth
        from google.auth import impersonated_credentials
        from google.oauth2 import service_account

        cred_config = self.credential.config if self.credential else {}
        auth_mode = cred_config.get("auth_mode")
        service_account_json = cred_config.get("service_account_json")
        scopes = [_GCP_CLOUD_PLATFORM_SCOPE]

        # Configs saved before auth_mode existed carry no mode; they used the
        # stored key when present and Application Default Credentials otherwise.
        if auth_mode == GCPAuthMode.HOST_IDENTITY or (auth_mode is None and not service_account_json):
            base, _ = google.auth.default(scopes=scopes)
        elif service_account_json:
            info = json.loads(service_account_json) if isinstance(service_account_json, str) else service_account_json
            base = service_account.Credentials.from_service_account_info(info, scopes=scopes)  # type: ignore[no-untyped-call]
        else:
            # Explicit key mode with no key must never fall through to the
            # host identity: that would provision in the wrong project.
            raise ValueError("GCP credential is in service_account_key mode but has no service_account_json")

        target = cred_config.get("impersonate_service_account")
        if not target:
            return base
        logger.debug("Impersonating GCP service account", target=target, connector_id=self.connector.id)
        return impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
            source_credentials=base, target_principal=target, target_scopes=scopes
        )

    def _get_compute_client(self) -> Any:
        """The instances client, made on first use; resolving the host identity may call the metadata server, so callers on the event loop run this in a thread."""
        try:
            from google.cloud import compute_v1
        except ImportError as exc:
            raise RuntimeError("google-cloud-compute is not installed: install octoprox[cloud]") from exc

        if self._instances_client is None:
            self._instances_client = compute_v1.InstancesClient(credentials=self._build_credentials())  # type: ignore[assignment]
        return self._instances_client

    async def create_instance(self, name: str) -> Proxy:
        """Create the instance ``name`` and wait for its insert operation."""
        client = await asyncio.to_thread(self._get_compute_client)
        from google.cloud import compute_v1

        source_image = self._config.get_source_image()
        credentials = ProxyCredentials.generate()
        logger.debug(
            "Creating GCP instance", connector_id=self.connector.id, name=name, source_image=source_image,
            zone=self._config.zone,
        )

        instance = compute_v1.Instance()
        instance.name = name
        instance.machine_type = f"zones/{self._config.zone}/machineTypes/{self._config.machine_type}"

        # Boot disk
        disk = compute_v1.AttachedDisk()
        disk.boot = True
        disk.auto_delete = True
        initialize_params = compute_v1.AttachedDiskInitializeParams()
        initialize_params.source_image = source_image
        initialize_params.disk_size_gb = 10
        disk.initialize_params = initialize_params
        instance.disks = [disk]

        # Network interface, with an external IP
        network_interface = compute_v1.NetworkInterface()
        network_interface.network = f"global/networks/{self._config.network}"
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = "ONE_TO_ONE_NAT"
        network_interface.access_configs = [access_config]
        instance.network_interfaces = [network_interface]

        # Labels: lowercase letters, digits, hyphens and underscores, at most
        # 63 characters. The connector's own never replace the one a scale run
        # lists by.
        labels = {"managed-by": "octoprox"}
        for key, value in self._config.tags.items():
            label_key = key.lower().replace(" ", "-").replace("_", "-")[:63]
            labels[label_key] = str(value).lower().replace(" ", "-").replace("_", "-")[:63]
        labels[CONNECTOR_TAG] = self.connector.id
        instance.labels = labels

        metadata = compute_v1.Metadata()
        metadata.items = [compute_v1.Items(key="startup-script", value=build_squid_setup_script(credentials))]
        instance.metadata = metadata

        def insert() -> Any:
            # The Compute Engine SDK is synchronous; result() waits for the
            # insert operation and raises the error it ended with.
            client.insert(project=self._config.project_id, zone=self._config.zone, instance_resource=instance).result()
            return client.get(project=self._config.project_id, zone=self._config.zone, instance=name)

        created = await asyncio.to_thread(insert)
        host = self._address(created)
        if not host:
            raise RuntimeError(f"GCP instance {name} runs without an address")
        logger.info("Created GCP proxy instance", instance_name=name, host=host)
        return self._proxy(name, host, credentials)

    async def tagged_instances(self) -> list[CloudInstance]:
        """Every provisioning, staging or running instance labelled with this connector."""
        client = await asyncio.to_thread(self._get_compute_client)
        from google.cloud import compute_v1

        def listed() -> list[Any]:
            # The client takes a filter only inside the request: as a keyword beside project and zone it is refused.
            request = compute_v1.ListInstancesRequest(
                project=self._config.project_id,
                zone=self._config.zone,
                filter=f'labels.{CONNECTOR_TAG} = "{self.connector.id}"',
            )
            return list(client.list(request=request))

        return [
            CloudInstance(instance.name, self._proxy_id(instance.name))
            for instance in await asyncio.to_thread(listed)
            if instance.status in _GCP_LIVE
        ]

    async def terminate_instance(self, proxy_id: str) -> None:
        """Delete the instance and wait for the delete operation; an instance gone, or stopping under another delete, counts as terminated."""
        name = self._key(proxy_id)
        client = await asyncio.to_thread(self._get_compute_client)
        from google.api_core.exceptions import GoogleAPICallError

        def delete() -> None:
            try:
                client.delete(project=self._config.project_id, zone=self._config.zone, instance=name).result()
            except GoogleAPICallError:
                instance = self._get(client, name)
                if instance is not None and instance.status != "STOPPING":
                    raise

        await asyncio.to_thread(delete)
        logger.info("Terminated GCP instance", instance_name=name)

    def _get(self, client: Any, name: str) -> Any:
        """The instance ``name``; None when the zone has none."""
        from google.api_core.exceptions import NotFound

        try:
            return client.get(project=self._config.project_id, zone=self._config.zone, instance=name)
        except NotFound:
            return None

    @staticmethod
    def _address(instance: Any) -> str | None:
        """An instance's external IP, or its internal one while it has none."""
        interfaces = list(instance.network_interfaces)
        external = (config.nat_i_p for interface in interfaces for config in interface.access_configs)
        internal = (interface.network_i_p for interface in interfaces)
        return next((address for address in (*external, *internal) if address), None)


class AzureProvider(CloudProvider):
    """Azure VMs in the connector's resource group, on its Ubuntu 24.04 image, through azure-mgmt-compute and azure-identity.

    The credential's ``auth_mode`` picks a stored secret or the host's identity, which may federate into another tenant.
    """

    # Linux VM names run to 64 characters, and the -osdisk, -nic and -pip
    # names made from them stay within their 80.
    NAME_LIMIT = 64
    ID_PREFIX = "azure-"
    ROW_TAG = "azure"
    KEY_FIELD = "vm_name"
    _config: AzureConnectorConfig

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, AzureConnectorConfig):
            raise ValueError("AzureProvider requires an Azure connector configuration")

        self._config = cloud_config
        self._compute_client = None
        self._network_client = None

    @property
    def place(self) -> str:
        """The connector's location."""
        return self._config.location

    def _build_credential(self) -> Any:
        """The azure-identity credential the management clients use, on the base identity of the credential's auth mode.

        In host mode it may name an app registration of another tenant that trusts the host identity, whose token
        azure-identity then exchanges the host's for, again whenever it needs refreshing.
        """
        from azure.identity import (
            ClientAssertionCredential,
            ClientSecretCredential,
            DefaultAzureCredential,
        )

        cred_config = self.credential.config if self.credential else {}
        auth_mode = cred_config.get("auth_mode")
        client_id = cred_config.get("client_id")
        client_secret = cred_config.get("client_secret")
        tenant_id = cred_config.get("tenant_id")
        has_secret = bool(client_id and client_secret and tenant_id)

        base: Any
        # Configs saved before auth_mode existed carry no mode; they used the
        # stored secret when present and the default chain otherwise.
        if auth_mode == AzureAuthMode.HOST_IDENTITY or (auth_mode is None and not has_secret):
            # Default chain: workload identity, managed identity, AZURE_* env vars.
            kwargs: dict[str, Any] = {}
            identity_client_id = cred_config.get("managed_identity_client_id")
            if identity_client_id:
                kwargs = {
                    "managed_identity_client_id": identity_client_id,
                    "workload_identity_client_id": identity_client_id,
                }
            base = DefaultAzureCredential(**kwargs)
        elif has_secret:
            base = ClientSecretCredential(tenant_id=str(tenant_id), client_id=str(client_id), client_secret=str(client_secret))
        else:
            # Explicit secret mode with no secret must never fall through to
            # the host identity: that would provision in the wrong tenant.
            raise ValueError("Azure credential is in client_secret mode but has no tenant_id/client_id/client_secret")

        target_client_id = cred_config.get("target_client_id")
        if not target_client_id:
            return base
        target_tenant_id = cred_config.get("target_tenant_id")
        if not target_tenant_id:
            raise ValueError("Azure credential names target_client_id without target_tenant_id")
        logger.debug(
            "Federating Azure host identity into target app",
            target_tenant_id=target_tenant_id,
            target_client_id=target_client_id,
            connector_id=self.connector.id,
        )

        def host_assertion() -> str:
            return str(base.get_token(_AZURE_TOKEN_EXCHANGE_SCOPE).token)

        return ClientAssertionCredential(tenant_id=str(target_tenant_id), client_id=str(target_client_id), func=host_assertion)

    def _get_azure_clients(self) -> tuple[Any, Any]:
        """The compute and network management clients, made on first use."""
        try:
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient

            if self._compute_client is None:
                # azure-identity is a separate package from azure-mgmt-*;
                # _build_credential imports it, so keep it under this guard.
                azure_credential = self._build_credential()
        except ImportError as exc:
            raise RuntimeError("the Azure SDK is not installed: install octoprox[cloud]") from exc

        if self._compute_client is None:
            self._compute_client = ComputeManagementClient(  # type: ignore[assignment]
                azure_credential, self._config.subscription_id
            )
            self._network_client = NetworkManagementClient(  # type: ignore[assignment]
                azure_credential, self._config.subscription_id
            )
        return self._compute_client, self._network_client

    async def create_instance(self, name: str) -> Proxy:
        """Create the VM ``name`` with its public IP and network interface, and wait until it is provisioned."""
        compute_client, network_client = self._get_azure_clients()
        if not self._config.vnet_name or not self._config.subnet_name:
            raise ValueError("vnet_name and subnet_name are required to create a VM")

        image_reference = self._config.get_image_reference()
        pip_name = f"{name}-pip"
        nic_name = f"{name}-nic"
        logger.debug(
            "Creating Azure VM", connector_id=self.connector.id, vm_name=name, image_reference=image_reference,
            location=self._config.location,
        )
        credentials = ProxyCredentials.generate()
        custom_data_b64 = base64.b64encode(build_squid_setup_script(credentials).encode()).decode()
        # The connector's own tags never replace the one a scale run lists by.
        tags = {"managed-by": "octoprox", **self._config.tags, CONNECTOR_TAG: self.connector.id}

        try:
            # Run all blocking Azure SDK calls in a thread to avoid blocking
            # the event loop. Each .result() call blocks waiting for the Azure
            # operation to complete (5-30+ seconds each). The request bodies
            # are the REST API's JSON (camelCase, nested under "properties"):
            # the SDK sends a dict as it is, without mapping attribute names.
            def _provision_vm() -> tuple[Any, str | None]:
                # Get subnet
                subnet = network_client.subnets.get(
                    self._config.resource_group, self._config.vnet_name, self._config.subnet_name
                )

                # Create public IP
                # Note: Standard SKU is required (Basic SKU is deprecated)
                # Standard SKU requires Static allocation
                pip_params = {
                    "location": self._config.location,
                    "sku": {"name": "Standard"},
                    "properties": {"publicIPAllocationMethod": "Static"},
                }
                pip_result = network_client.public_ip_addresses.begin_create_or_update(
                    self._config.resource_group, pip_name, pip_params
                ).result()

                # Create NIC
                nic_params = {
                    "location": self._config.location,
                    "properties": {
                        "ipConfigurations": [
                            {
                                "name": "ipconfig1",
                                "properties": {
                                    "subnet": {"id": subnet.id},
                                    "publicIPAddress": {"id": pip_result.id},
                                },
                            }
                        ],
                    },
                }
                nic_result = network_client.network_interfaces.begin_create_or_update(
                    self._config.resource_group, nic_name, nic_params
                ).result()

                # Create VM
                vm_params = {
                    "location": self._config.location,
                    "tags": tags,
                    "properties": {
                        "hardwareProfile": {"vmSize": self._config.vm_size},
                        "storageProfile": {
                            "imageReference": image_reference,
                            "osDisk": {
                                "name": f"{name}-osdisk",
                                "caching": "ReadWrite",
                                "createOption": "FromImage",
                                "managedDisk": {"storageAccountType": "Standard_LRS"},
                            },
                        },
                        "osProfile": {
                            "computerName": name,
                            "adminUsername": "octoprox",
                            "customData": custom_data_b64,
                            "linuxConfiguration": {
                                "disablePasswordAuthentication": bool(self._config.ssh_public_key),
                                "ssh": {
                                    "publicKeys": [
                                        {
                                            "path": "/home/octoprox/.ssh/authorized_keys",
                                            "keyData": self._config.ssh_public_key,
                                        }
                                    ] if self._config.ssh_public_key else [],
                                },
                            },
                        },
                        "networkProfile": {
                            "networkInterfaces": [{"id": nic_result.id, "properties": {"primary": True}}]
                        },
                    },
                }

                vm_result = compute_client.virtual_machines.begin_create_or_update(
                    self._config.resource_group, name, vm_params
                ).result()

                # Get public IP (may need to wait for allocation)
                pip = network_client.public_ip_addresses.get(self._config.resource_group, pip_name)
                host = pip.ip_address

                if not host:
                    # Try to get private IP
                    nic = network_client.network_interfaces.get(self._config.resource_group, nic_name)
                    host = nic.ip_configurations[0].private_ip_address

                return vm_result, host

            vm_result, host = await asyncio.to_thread(_provision_vm)

        except Exception:
            # Clean up any resources that were created before the failure
            # Must delete NIC before public IP (NIC references the public IP)
            # Azure reserves the NIC for 180 seconds after a failed VM creation,
            # so we wait and retry to ensure cleanup
            nic_deleted = False
            # Try immediately first, then wait 180 seconds and retry if needed
            for attempt in range(2):
                try:
                    if attempt > 0:
                        logger.info(
                            "Waiting 180 seconds for Azure NIC reservation to expire",
                            nic_name=nic_name,
                        )
                        await asyncio.sleep(180)
                    logger.debug("Cleaning up NIC after VM creation failure", nic_name=nic_name)
                    await asyncio.to_thread(
                        lambda: network_client.network_interfaces.begin_delete(
                            self._config.resource_group, nic_name
                        ).result()
                    )
                    nic_deleted = True
                    logger.info("Successfully cleaned up NIC", nic_name=nic_name)
                    break
                except Exception as cleanup_error:
                    error_str = str(cleanup_error)
                    if "NicReservedForAnotherVm" in error_str and attempt == 0:
                        # Azure reserves NIC for 180 seconds, will retry after wait
                        continue
                    logger.warning(
                        "Failed to clean up NIC after VM creation failure",
                        nic_name=nic_name,
                        error=error_str,
                    )

            if not nic_deleted:
                # If NIC wasn't deleted, we can't delete the public IP either
                logger.warning(
                    "Cannot clean up public IP because NIC cleanup failed",
                    pip_name=pip_name,
                    nic_name=nic_name,
                )
            else:
                try:
                    logger.debug("Cleaning up public IP after VM creation failure", pip_name=pip_name)
                    await asyncio.to_thread(
                        lambda: network_client.public_ip_addresses.begin_delete(
                            self._config.resource_group, pip_name
                        ).result()
                    )
                    logger.info("Successfully cleaned up public IP", pip_name=pip_name)
                except Exception as cleanup_error:
                    logger.warning(
                        "Failed to clean up public IP after VM creation failure",
                        pip_name=pip_name,
                        error=str(cleanup_error),
                    )

            raise

        if not host:
            raise RuntimeError(f"Azure VM {name} runs without an address")
        logger.info("Created Azure proxy VM", vm_name=name, host=host)
        return self._proxy(name, host, credentials, vm_id=vm_result.vm_id)

    async def tagged_instances(self) -> list[CloudInstance]:
        """Every VM of the resource group tagged with this connector, unless it is being deleted or failed to provision."""
        compute_client, _ = self._get_azure_clients()

        def listed() -> list[CloudInstance]:
            return [
                CloudInstance(vm.name, self._proxy_id(vm.name))
                for vm in compute_client.virtual_machines.list(self._config.resource_group)
                if (vm.tags or {}).get(CONNECTOR_TAG) == self.connector.id and vm.provisioning_state not in _AZURE_DEAD
            ]

        return await asyncio.to_thread(listed)

    async def terminate_instance(self, proxy_id: str) -> None:
        """Delete the VM and wait until it is gone, then its network interface and public IP; a VM gone, or under another delete, counts as terminated."""
        from azure.core.exceptions import HttpResponseError

        name = self._key(proxy_id)
        compute_client, network_client = self._get_azure_clients()

        def delete() -> None:
            try:
                compute_client.virtual_machines.begin_delete(self._config.resource_group, name).result()
            except HttpResponseError:
                state = self._provisioning_state(compute_client, name)
                if state == _AZURE_DELETING:
                    # The other delete removes the network resources once the VM is gone.
                    return
                if state is not None:
                    raise
            # The interface goes first: it holds the public IP.
            for operations, resource in (
                (network_client.network_interfaces, f"{name}-nic"),
                (network_client.public_ip_addresses, f"{name}-pip"),
            ):
                try:
                    operations.begin_delete(self._config.resource_group, resource).result()
                except Exception as cleanup_error:
                    logger.warning(
                        "Failed to clean up a network resource of a terminated VM",
                        resource=resource,
                        error=error_text(cleanup_error),
                    )

        await asyncio.to_thread(delete)
        logger.info("Terminated Azure VM", vm_name=name)

    def _provisioning_state(self, compute_client: Any, name: str) -> str | None:
        """The VM's provisioning state; None when the resource group has no such VM."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            vm = compute_client.virtual_machines.get(self._config.resource_group, name)
        except ResourceNotFoundError:
            return None
        return str(vm.provisioning_state or "")


# The cloud API of each cloud credential type.
_PROVIDERS: dict[str, type[CloudProvider]] = {
    CredentialType.AWS: AWSProvider,
    CredentialType.GCP: GCPProvider,
    CredentialType.AZURE: AzureProvider,
}
