# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Cloud provider integrations for dynamic proxy instances.

This module provides base classes and stubs for cloud provider integrations.
Actual implementations require the 'cloud' optional dependencies.
"""

import asyncio
import base64
import secrets
import string
from abc import abstractmethod
from pathlib import Path
from typing import Any

import structlog

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy, ProxyStatus
from api.providers.base import ProxyProvider

logger = structlog.get_logger()

# Standard proxy port for all cloud providers
PROXY_PORT = 3128

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


def _generate_proxy_credentials() -> tuple[str, str]:
    """Generate random username and password for proxy authentication."""
    alphabet = string.ascii_letters + string.digits
    username = "u" + "".join(secrets.choice(alphabet) for _ in range(7))
    password = "".join(secrets.choice(alphabet) for _ in range(24))
    return username, password


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


def build_squid_setup_script(
    username: str | None = None, password: str | None = None
) -> str:
    """Build a Squid setup script, optionally with per-instance authentication.

    Args:
        username: Proxy auth username. If None, proxy allows all connections.
        password: Proxy auth password. Required if username is provided.

    Returns:
        Complete bash setup script with auth configuration baked in.
    """
    template = _load_squid_setup_template()

    if username and password:
        # Compute base64-encoded credentials for the Proxy-Authorization header
        auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
        # Escape regex-special chars in base64 output (+ is the only one possible)
        auth_b64_escaped = auth_b64.replace("+", "\\+")
        auth_config = _AUTH_CONFIG.format(auth_b64=auth_b64_escaped)
    else:
        auth_config = _NO_AUTH_CONFIG

    script = template.replace("##OCTOPROX_AUTH_CONFIG##", auth_config)
    return script


class CloudProvider(ProxyProvider):
    """Abstract base class for cloud-based proxy providers.

    Cloud providers manage instances dynamically via create_instance/terminate_instance.
    Each cloud provider implementation should define its own provider-specific
    attributes (region, instance type, etc.) in its __init__ method.
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)

    @abstractmethod
    async def create_instance(self) -> Proxy | None:
        """Create a new proxy instance in the cloud."""
        ...

    @abstractmethod
    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate a cloud proxy instance."""
        ...


class AWSProvider(CloudProvider):
    """AWS EC2-based proxy provider.

    Requires boto3 to be installed (pip install octoprox[cloud]).

    Connector config options:
        - region: AWS region (default: us-east-1)
        - instance_type: EC2 instance type (t3.* for x86_64, t4g.* for arm64)
        - instance_name: Name prefix for created instances (required)
        - security_group: Security group ID (required)
        - key_pair_name: SSH key pair name (required)
        - tags: Custom tags to apply to instances (optional, dict)

    Note: AMI is automatically determined based on region and instance type.
    Ubuntu 24.04 LTS is used for all instances.

    Credential config (secrets):
        - access_key: AWS access key ID
        - secret_key: AWS secret access key
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # AWS-specific config from connector using typed cloud_config
        from api.models.connector import AWSConnectorConfig

        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, AWSConnectorConfig):
            raise ValueError("AWSProvider requires an AWS connector configuration")

        self._config = cloud_config
        self._ec2_client = None
        self._ec2_resource = None

    def _get_boto3_clients(self) -> tuple[Any, Any]:
        """Get or create boto3 clients."""
        try:
            import boto3  # type: ignore[import-untyped]

            if self._ec2_client is None:
                # Get credentials from credential config
                cred_config = self.credential.config if self.credential else {}
                access_key = cred_config.get("access_key")
                secret_key = cred_config.get("secret_key")

                if access_key and secret_key:
                    self._ec2_client = boto3.client(
                        "ec2",
                        region_name=self._config.region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                    )
                    self._ec2_resource = boto3.resource(
                        "ec2",
                        region_name=self._config.region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                    )
                else:
                    # Fall back to default credentials (env vars, IAM role, etc.)
                    self._ec2_client = boto3.client("ec2", region_name=self._config.region)
                    self._ec2_resource = boto3.resource("ec2", region_name=self._config.region)
            return self._ec2_client, self._ec2_resource
        except ImportError:
            logger.error("boto3 not installed. Install with: pip install octoprox[cloud]")
            return None, None

    async def create_instance(self) -> Proxy | None:
        """Create a new EC2 instance configured as a proxy."""
        # Get the AMI for this region and instance type from the config
        ami_id = self._config.get_ami()
        if not ami_id:
            logger.error(
                "No AMI found for region/instance type combination",
                region=self._config.region,
                instance_type=self._config.instance_type,
            )
            return None

        logger.debug(
            "AWS create_instance called",
            connector_id=self.connector.id,
            ami_id=ami_id,
            security_group=self._config.security_group,
            instance_name=self._config.instance_name,
        )

        _, ec2_resource = self._get_boto3_clients()
        if not ec2_resource:
            return None

        import time

        # Generate per-instance proxy credentials and build setup script
        proxy_username, proxy_password = _generate_proxy_credentials()
        user_data = build_squid_setup_script(proxy_username, proxy_password)

        # Build instance name with timestamp for uniqueness
        instance_name = f"{self._config.instance_name}-{int(time.time())}"

        # Build tags: start with required tags, then add custom tags
        tags = [
            {"Key": "Name", "Value": instance_name},
            {"Key": "ManagedBy", "Value": "octoprox"},
        ]
        # Add custom tags from connector config
        for key, value in self._config.tags.items():
            tags.append({"Key": key, "Value": str(value)})

        run_args = {
            "ImageId": ami_id,
            "InstanceType": self._config.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": user_data,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": tags,
                }
            ],
        }

        # Handle security group configuration
        # The security group determines which VPC the instance will be launched in
        security_group = self._config.security_group.strip()

        if security_group:
            run_args["SecurityGroupIds"] = [security_group]

        if self._config.key_pair_name:
            run_args["KeyName"] = self._config.key_pair_name

        logger.debug(
            "Creating EC2 instance",
            ami_id=ami_id,
            instance_type=self._config.instance_type,
            security_group=self._config.security_group,
        )

        def _create_and_wait() -> Any:
            instances = ec2_resource.create_instances(**run_args)
            instance = instances[0]
            instance.wait_until_running()
            instance.reload()
            return instance

        instance = await asyncio.to_thread(_create_and_wait)

        host = instance.public_ip_address or instance.private_ip_address
        if not host:
            logger.error("Instance created but no IP address available")
            return None

        proxy = Proxy(
            id=f"aws-{instance.id}",
            host=host,
            port=PROXY_PORT,
            username=proxy_username,
            password=proxy_password,
            connector_id=self.connector.id,
            status=ProxyStatus.INITIALIZING,
            tags=["aws", self._config.region],
            metadata={"instance_id": instance.id},
        )

        logger.info("Created AWS proxy instance", instance_id=instance.id, host=host)
        return proxy

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate an EC2 instance."""
        ec2_client, _ = self._get_boto3_clients()
        if not ec2_client:
            return False

        # Remove aws- prefix if present
        ec2_instance_id = instance_id.replace("aws-", "")
        await asyncio.to_thread(
            ec2_client.terminate_instances, InstanceIds=[ec2_instance_id]
        )
        logger.info("Terminated AWS instance", instance_id=ec2_instance_id)
        return True


class GCPProvider(CloudProvider):
    """Google Cloud Compute Engine proxy provider.

    Requires google-cloud-compute to be installed (pip install octoprox[cloud]).

    Connector config options:
        - project_id: GCP project ID (required)
        - zone: GCP zone (default: us-central1-a)
        - machine_type: Machine type (e2-* for x86_64, t2a-* for arm64)
        - instance_name: Name prefix for created instances (required)
        - network: VPC network name (default: default)
        - subnet: Subnet name (optional)
        - tags: Custom labels to apply to instances (optional, dict)

    Note: Source image is automatically determined based on machine type.
    Ubuntu 24.04 LTS is used for all instances.

    Credential config (secrets):
        - service_account_json: GCP service account JSON key (optional, uses default credentials if not provided)
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # GCP-specific config from connector using typed cloud_config
        from api.models.connector import GCPConnectorConfig

        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, GCPConnectorConfig):
            raise ValueError("GCPProvider requires a GCP connector configuration")

        self._config = cloud_config
        self._instances_client = None

    def _get_compute_client(self) -> Any:
        """Get or create GCP compute client.

        Note: Client creation involves synchronous file I/O when service account
        JSON is provided. This is acceptable because it only runs once (cached),
        and the temp file write is fast. The actual blocking API calls in
        create_instance/terminate_instance are offloaded via asyncio.to_thread.
        """
        try:
            from google.cloud import compute_v1

            if self._instances_client is None:
                # Check if service account JSON is provided in credentials
                cred_config = self.credential.config if self.credential else {}
                service_account_json = cred_config.get("service_account_json")
                if service_account_json:
                    import json
                    import os
                    import tempfile

                    # Write service account JSON to temp file for authentication
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                        if isinstance(service_account_json, str):
                            f.write(service_account_json)
                        else:
                            json.dump(service_account_json, f)
                        temp_path = f.name

                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_path

                self._instances_client = compute_v1.InstancesClient()  # type: ignore[assignment]
            return self._instances_client
        except ImportError:
            logger.error("google-cloud-compute not installed. Install with: pip install octoprox[cloud]")
            return None

    async def create_instance(self) -> Proxy | None:
        """Create a new GCP Compute Engine instance configured as a proxy."""
        client = self._get_compute_client()
        if not client:
            return None

        # Get the source image for this machine type from the config
        source_image = self._config.get_source_image()

        import time

        from google.cloud import compute_v1

        # Build instance name with timestamp for uniqueness
        instance_name = f"{self._config.instance_name}-{int(time.time())}"

        # Generate per-instance proxy credentials and build setup script
        proxy_username, proxy_password = _generate_proxy_credentials()
        startup_script = build_squid_setup_script(proxy_username, proxy_password)

        logger.debug(
            "GCP create_instance called",
            connector_id=self.connector.id,
            source_image=source_image,
            zone=self._config.zone,
            instance_name=instance_name,
        )

        # Build instance config
        instance = compute_v1.Instance()
        instance.name = instance_name
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

        # Network interface
        network_interface = compute_v1.NetworkInterface()
        network_interface.network = f"global/networks/{self._config.network}"

        # Add external IP
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = "ONE_TO_ONE_NAT"
        network_interface.access_configs = [access_config]
        instance.network_interfaces = [network_interface]

        # Labels - start with required labels, then add custom labels from tags
        # GCP labels must be lowercase with hyphens
        labels = {
            "managed-by": "octoprox",
        }
        # Add custom labels from connector config tags
        for key, value in self._config.tags.items():
            # GCP labels must be lowercase, max 63 chars, only lowercase letters, numbers, hyphens
            label_key = key.lower().replace(" ", "-").replace("_", "-")[:63]
            label_value = str(value).lower().replace(" ", "-").replace("_", "-")[:63]
            labels[label_key] = label_value
        instance.labels = labels

        # Metadata with startup script
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script)
        ]
        instance.metadata = metadata

        # Create the instance and wait for completion in a thread to avoid
        # blocking the event loop (GCP SDK is synchronous)
        def _insert_and_wait() -> Any:
            import time as _time

            from google.cloud.compute_v1.services.zone_operations import (
                ZoneOperationsClient,
            )

            operation = client.insert(
                project=self._config.project_id,
                zone=self._config.zone,
                instance_resource=instance,
            )
            ops_client = ZoneOperationsClient()
            while operation.status != compute_v1.Operation.Status.DONE:
                _time.sleep(2)
                operation = ops_client.get(
                    project=self._config.project_id,
                    zone=self._config.zone,
                    operation=operation.name,
                )
            return client.get(
                project=self._config.project_id,
                zone=self._config.zone,
                instance=instance_name,
            )

        created_instance = await asyncio.to_thread(_insert_and_wait)

        # Get IP address
        host = None
        for interface in created_instance.network_interfaces:
            for access_config in interface.access_configs:
                if access_config.nat_i_p:
                    host = access_config.nat_i_p
                    break
            if not host:
                host = interface.network_i_p

        if not host:
            logger.error("Instance created but no IP address available")
            return None

        proxy = Proxy(
            id=f"gcp-{instance_name}",
            host=host,
            port=PROXY_PORT,
            username=proxy_username,
            password=proxy_password,
            connector_id=self.connector.id,
            status=ProxyStatus.INITIALIZING,
            tags=["gcp", self._config.zone],
            metadata={"instance_name": instance_name},
        )

        logger.info("Created GCP proxy instance", instance_name=instance_name, host=host)
        return proxy

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate a GCP Compute Engine instance."""
        client = self._get_compute_client()
        if not client:
            return False

        # Remove gcp- prefix if present
        instance_name = instance_id.replace("gcp-", "")
        await asyncio.to_thread(
            client.delete,
            project=self._config.project_id,
            zone=self._config.zone,
            instance=instance_name,
        )
        logger.info("Terminated GCP instance", instance_name=instance_name)
        return True


class AzureProvider(CloudProvider):
    """Azure VM proxy provider.

    Requires azure-mgmt-compute and azure-identity to be installed (pip install octoprox[cloud]).

    Connector config options:
        - subscription_id: Azure subscription ID (required)
        - resource_group: Resource group name (required)
        - location: Azure region (default: eastus)
        - vm_size: VM size (B-series for x86_64, Bps v2 for arm64)
        - instance_name: Name prefix for created VMs (required)
        - vnet_name: Virtual network name (required for create)
        - subnet_name: Subnet name (required for create)
        - tags: Custom tags to apply to VMs (optional, dict)

    Note: OS image is automatically determined based on VM size.
    Ubuntu 24.04 LTS is used for all instances.

    Credential config (secrets):
        - client_id: Azure service principal client ID
        - client_secret: Azure service principal client secret
        - tenant_id: Azure tenant ID
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # Azure-specific config from connector using typed cloud_config
        from api.models.connector import AzureConnectorConfig

        cloud_config = connector.cloud_config
        if not isinstance(cloud_config, AzureConnectorConfig):
            raise ValueError("AzureProvider requires an Azure connector configuration")

        self._config = cloud_config
        self._compute_client = None
        self._network_client = None

    def _get_azure_clients(self) -> tuple[Any, Any]:
        """Get or create Azure clients."""
        try:
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient

            if self._compute_client is None:
                # Check if service principal credentials are provided
                cred_config = self.credential.config if self.credential else {}
                client_id = cred_config.get("client_id")
                client_secret = cred_config.get("client_secret")
                tenant_id = cred_config.get("tenant_id")

                azure_credential: Any
                if client_id and client_secret and tenant_id:
                    from azure.identity import ClientSecretCredential

                    azure_credential = ClientSecretCredential(
                        tenant_id=tenant_id,
                        client_id=client_id,
                        client_secret=client_secret,
                    )
                else:
                    # Fall back to default credentials
                    from azure.identity import DefaultAzureCredential

                    azure_credential = DefaultAzureCredential()

                self._compute_client = ComputeManagementClient(  # type: ignore[assignment]
                    azure_credential, self._config.subscription_id
                )
                self._network_client = NetworkManagementClient(  # type: ignore[assignment]
                    azure_credential, self._config.subscription_id
                )
            return self._compute_client, self._network_client
        except ImportError:
            logger.error(
                "Azure SDK not installed. Install with: pip install octoprox[cloud]"
            )
            return None, None

    async def create_instance(self) -> Proxy | None:
        """Create a new Azure VM configured as a proxy."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client or not network_client:
            return None

        if not self._config.vnet_name or not self._config.subnet_name:
            logger.error("vnet_name and subnet_name required for VM creation")
            return None

        # Get the image reference for this VM size from the config
        image_reference = self._config.get_image_reference()

        # Track created resources for cleanup on failure
        pip_name: str | None = None
        nic_name: str | None = None

        try:
            import base64
            import time

            # Build VM name with timestamp for uniqueness
            vm_name = f"{self._config.instance_name}-{int(time.time())}"

            logger.debug(
                "Azure create_instance called",
                connector_id=self.connector.id,
                image_reference=image_reference,
                location=self._config.location,
                vm_name=vm_name,
            )

            # Generate per-instance proxy credentials and build setup script
            proxy_username, proxy_password = _generate_proxy_credentials()
            custom_data = build_squid_setup_script(proxy_username, proxy_password)
            custom_data_b64 = base64.b64encode(custom_data.encode()).decode()

            # Build tags: start with required tags, then add custom tags
            tags = {
                "managed-by": "octoprox",
            }
            # Add custom tags from connector config
            for key, value in self._config.tags.items():
                tags[key] = str(value)

            # Run all blocking Azure SDK calls in a thread to avoid blocking
            # the event loop. Each .result() call blocks waiting for the Azure
            # operation to complete (5-30+ seconds each).
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
                    "public_ip_allocation_method": "Static",
                }
                pip_result = network_client.public_ip_addresses.begin_create_or_update(
                    self._config.resource_group, pip_name, pip_params
                ).result()

                # Create NIC
                nic_params = {
                    "location": self._config.location,
                    "ip_configurations": [
                        {
                            "name": "ipconfig1",
                            "subnet": {"id": subnet.id},
                            "public_ip_address": {"id": pip_result.id},
                        }
                    ],
                }
                nic_result = network_client.network_interfaces.begin_create_or_update(
                    self._config.resource_group, nic_name, nic_params
                ).result()

                # Create VM
                vm_params = {
                    "location": self._config.location,
                    "tags": tags,
                    "hardware_profile": {"vm_size": self._config.vm_size},
                    "storage_profile": {
                        "image_reference": image_reference,
                        "os_disk": {
                            "name": f"{vm_name}-osdisk",
                            "caching": "ReadWrite",
                            "create_option": "FromImage",
                            "managed_disk": {"storage_account_type": "Standard_LRS"},
                        },
                    },
                    "os_profile": {
                        "computer_name": vm_name,
                        "admin_username": "octoprox",
                        "custom_data": custom_data_b64,
                        "linux_configuration": {
                            "disable_password_authentication": bool(self._config.ssh_public_key),
                            "ssh": {
                                "public_keys": [
                                    {
                                        "path": "/home/octoprox/.ssh/authorized_keys",
                                        "key_data": self._config.ssh_public_key,
                                    }
                                ] if self._config.ssh_public_key else [],
                            },
                        },
                    },
                    "network_profile": {
                        "network_interfaces": [{"id": nic_result.id, "primary": True}]
                    },
                }

                vm_result = compute_client.virtual_machines.begin_create_or_update(
                    self._config.resource_group, vm_name, vm_params
                ).result()

                # Get public IP (may need to wait for allocation)
                pip = network_client.public_ip_addresses.get(self._config.resource_group, pip_name)
                host = pip.ip_address

                if not host:
                    # Try to get private IP
                    nic = network_client.network_interfaces.get(self._config.resource_group, nic_name)
                    host = nic.ip_configurations[0].private_ip_address

                return vm_result, host

            pip_name = f"{vm_name}-pip"
            nic_name = f"{vm_name}-nic"

            vm_result, host = await asyncio.to_thread(_provision_vm)

            if not host:
                logger.error("VM created but no IP address available")
                return None

            proxy = Proxy(
                id=f"azure-{vm_name}",
                host=host,
                port=PROXY_PORT,
                username=proxy_username,
                password=proxy_password,
                connector_id=self.connector.id,
                status=ProxyStatus.INITIALIZING,
                tags=["azure", self._config.location],
                metadata={"vm_name": vm_name, "vm_id": vm_result.vm_id},
            )

            logger.info("Created Azure proxy VM", vm_name=vm_name, host=host)
            return proxy

        except Exception:
            # Clean up any resources that were created before the failure
            # Must delete NIC before public IP (NIC references the public IP)
            # Azure reserves the NIC for 180 seconds after a failed VM creation,
            # so we wait and retry to ensure cleanup
            nic_deleted = False
            if nic_name:
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

            if pip_name:
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

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate an Azure VM."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client:
            return False

        # Remove azure- prefix if present
        vm_name = instance_id.replace("azure-", "")

        # Run all blocking Azure SDK delete calls in a thread
        def _delete_vm_resources() -> None:
            # Delete VM
            compute_client.virtual_machines.begin_delete(
                self._config.resource_group, vm_name
            ).result()

            # Clean up NIC and public IP
            nic_name = f"{vm_name}-nic"
            pip_name = f"{vm_name}-pip"

            try:
                network_client.network_interfaces.begin_delete(
                    self._config.resource_group, nic_name
                ).result()
            except Exception as cleanup_error:
                logger.warning(
                    "Failed to clean up NIC during VM termination",
                    nic_name=nic_name,
                    error=str(cleanup_error),
                )

            try:
                network_client.public_ip_addresses.begin_delete(
                    self._config.resource_group, pip_name
                ).result()
            except Exception as cleanup_error:
                logger.warning(
                    "Failed to clean up public IP during VM termination",
                    pip_name=pip_name,
                    error=str(cleanup_error),
                )

        await asyncio.to_thread(_delete_vm_resources)

        logger.info("Terminated Azure VM", vm_name=vm_name)
        return True

