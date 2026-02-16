"""Cloud provider integrations for dynamic proxy instances.

This module provides base classes and stubs for cloud provider integrations.
Actual implementations require the 'cloud' optional dependencies.
"""

from abc import abstractmethod
from pathlib import Path

import structlog

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy, ProxyStatus
from api.providers.base import ProxyProvider

logger = structlog.get_logger()

# Standard proxy port for all cloud providers
PROXY_PORT = 3128

# Load Squid setup script from config file
_SQUID_SETUP_SCRIPT: str | None = None


def get_squid_setup_script() -> str:
    """Load the Squid setup script from config file.

    Raises:
        FileNotFoundError: If the squid_setup.sh config file is not found.
    """
    global _SQUID_SETUP_SCRIPT
    if _SQUID_SETUP_SCRIPT is None:
        script_path = Path(__file__).parent.parent.parent / "config" / "squid_setup.sh"
        if not script_path.exists():
            raise FileNotFoundError(
                f"Squid setup script not found at {script_path}. "
                "This file is required for cloud provider instances."
            )
        _SQUID_SETUP_SCRIPT = script_path.read_text()
    return _SQUID_SETUP_SCRIPT


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
        - instance_type: EC2 instance type (default: t3.micro)
        - instance_name: Name prefix for created instances (required)
        - ami_id: AMI ID for proxy instances (required)
        - security_group: Security group ID (required)
        - key_pair_name: SSH key pair name (required)
        - tags: Custom tags to apply to instances (optional, dict)

    Credential config (secrets):
        - access_key: AWS access key ID
        - secret_key: AWS secret access key
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # AWS-specific config from connector
        self._region = connector.config.get("region", "us-east-1")
        self._instance_type = connector.config.get("instance_type", "t3.micro")
        self._instance_name = connector.config.get("instance_name")
        self._ami_id = connector.config.get("ami_id")
        self._security_group = connector.config.get("security_group")
        self._key_pair_name = connector.config.get("key_pair_name")
        self._tags = connector.config.get("tags", {})
        self._ec2_client = None
        self._ec2_resource = None

    def _get_boto3_clients(self):
        """Get or create boto3 clients."""
        try:
            import boto3

            if self._ec2_client is None:
                # Get credentials from credential config
                access_key = self.credential.config.get("access_key")
                secret_key = self.credential.config.get("secret_key")

                if access_key and secret_key:
                    self._ec2_client = boto3.client(
                        "ec2",
                        region_name=self._region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                    )
                    self._ec2_resource = boto3.resource(
                        "ec2",
                        region_name=self._region,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                    )
                else:
                    # Fall back to default credentials (env vars, IAM role, etc.)
                    self._ec2_client = boto3.client("ec2", region_name=self._region)
                    self._ec2_resource = boto3.resource("ec2", region_name=self._region)
            return self._ec2_client, self._ec2_resource
        except ImportError:
            logger.error("boto3 not installed. Install with: pip install octoprox[cloud]")
            return None, None

    async def create_instance(self) -> Proxy | None:
        """Create a new EC2 instance configured as a proxy."""
        logger.debug(
            "AWS create_instance called",
            connector_id=self.connector.id,
            ami_id=self._ami_id,
            security_group=self._security_group,
            instance_name=self._instance_name,
            raw_config=self.connector.config,
        )

        _, ec2_resource = self._get_boto3_clients()
        if not ec2_resource:
            return None

        # Validate required fields (check for None and empty strings)
        if not self._ami_id or not self._ami_id.strip():
            logger.error("ami_id required for instance creation")
            return None
        if not self._instance_name or not self._instance_name.strip():
            logger.error("instance_name required in connector config for instance creation")
            return None

        try:
            import time

            # Use the external Squid setup script
            user_data = get_squid_setup_script()

            # Build instance name with timestamp for uniqueness
            instance_name = f"{self._instance_name}-{int(time.time())}"

            # Build tags: start with required tags, then add custom tags
            tags = [
                {"Key": "Name", "Value": instance_name},
                {"Key": "ManagedBy", "Value": "octoprox"},
            ]
            # Add custom tags from connector config
            for key, value in self._tags.items():
                tags.append({"Key": key, "Value": str(value)})

            run_args = {
                "ImageId": self._ami_id,
                "InstanceType": self._instance_type,
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
            security_group = self._security_group.strip() if self._security_group else None

            if security_group:
                run_args["SecurityGroupIds"] = [security_group]

            if self._key_pair_name:
                run_args["KeyName"] = self._key_pair_name

            logger.debug(
                "Creating EC2 instance",
                ami_id=self._ami_id,
                instance_type=self._instance_type,
                security_group=self._security_group,
            )

            instances = ec2_resource.create_instances(**run_args)
            instance = instances[0]

            # Wait for instance to be running
            instance.wait_until_running()
            instance.reload()

            host = instance.public_ip_address or instance.private_ip_address
            if not host:
                logger.error("Instance created but no IP address available")
                return None

            proxy = Proxy(
                id=f"aws-{instance.id}",
                host=host,
                port=PROXY_PORT,
                connector_id=self.connector.id,
                status=ProxyStatus.INITIALIZING,
                tags=["aws", self._region],
                metadata={"instance_id": instance.id},
            )

            logger.info("Created AWS proxy instance", instance_id=instance.id, host=host)
            return proxy

        except Exception as e:
            logger.error("Failed to create AWS instance", error=str(e))
            return None

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate an EC2 instance."""
        ec2_client, _ = self._get_boto3_clients()
        if not ec2_client:
            return False

        try:
            # Remove aws- prefix if present
            ec2_instance_id = instance_id.replace("aws-", "")
            ec2_client.terminate_instances(InstanceIds=[ec2_instance_id])
            logger.info("Terminated AWS instance", instance_id=ec2_instance_id)
            return True
        except Exception as e:
            logger.error("Failed to terminate AWS instance", error=str(e))
            return False


class GCPProvider(CloudProvider):
    """Google Cloud Compute Engine proxy provider.

    Requires google-cloud-compute to be installed (pip install octoprox[cloud]).

    Connector config options:
        - project_id: GCP project ID (required)
        - zone: GCP zone (default: us-central1-a)
        - machine_type: Machine type (default: e2-micro)
        - instance_name: Name prefix for created instances (required)
        - network: VPC network name (default: default)
        - subnet: Subnet name (optional)
        - source_image: Source image for instances (default: debian-cloud/debian-11)
        - tags: Custom labels to apply to instances (optional, dict)

    Credential config (secrets):
        - service_account_json: GCP service account JSON key (optional, uses default credentials if not provided)
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # GCP-specific config from connector
        self._project_id = connector.config.get("project_id")
        self._zone = connector.config.get("zone", "us-central1-a")
        self._machine_type = connector.config.get("machine_type", "e2-micro")
        self._instance_name = connector.config.get("instance_name")
        self._network = connector.config.get("network", "default")
        self._subnet = connector.config.get("subnet")
        self._source_image = connector.config.get(
            "source_image", "projects/debian-cloud/global/images/family/debian-11"
        )
        self._tags = connector.config.get("tags", {})
        self._instances_client = None

    def _get_compute_client(self):
        """Get or create GCP compute client."""
        try:
            from google.cloud import compute_v1

            if self._instances_client is None:
                # Check if service account JSON is provided in credentials
                service_account_json = self.credential.config.get("service_account_json")
                if service_account_json:
                    import json
                    import tempfile
                    import os

                    # Write service account JSON to temp file for authentication
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                        if isinstance(service_account_json, str):
                            f.write(service_account_json)
                        else:
                            json.dump(service_account_json, f)
                        temp_path = f.name

                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_path

                self._instances_client = compute_v1.InstancesClient()
            return self._instances_client
        except ImportError:
            logger.error("google-cloud-compute not installed. Install with: pip install octoprox[cloud]")
            return None

    async def create_instance(self) -> Proxy | None:
        """Create a new GCP Compute Engine instance configured as a proxy."""
        client = self._get_compute_client()
        if not client or not self._project_id:
            return None

        if not self._instance_name:
            logger.error("instance_name required in connector config for instance creation")
            return None

        try:
            from google.cloud import compute_v1
            import time

            # Build instance name with timestamp for uniqueness
            instance_name = f"{self._instance_name}-{int(time.time())}"

            # Use the external Squid setup script
            startup_script = get_squid_setup_script()

            # Build instance config
            instance = compute_v1.Instance()
            instance.name = instance_name
            instance.machine_type = f"zones/{self._zone}/machineTypes/{self._machine_type}"

            # Boot disk
            disk = compute_v1.AttachedDisk()
            disk.boot = True
            disk.auto_delete = True
            initialize_params = compute_v1.AttachedDiskInitializeParams()
            initialize_params.source_image = self._source_image
            initialize_params.disk_size_gb = 10
            disk.initialize_params = initialize_params
            instance.disks = [disk]

            # Network interface
            network_interface = compute_v1.NetworkInterface()
            network_interface.network = f"global/networks/{self._network}"
            if self._subnet:
                # Extract region from zone (e.g., us-central1-a -> us-central1)
                region = "-".join(self._zone.split("-")[:-1])
                network_interface.subnetwork = f"regions/{region}/subnetworks/{self._subnet}"

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
            for key, value in self._tags.items():
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

            # Create the instance
            operation = client.insert(
                project=self._project_id,
                zone=self._zone,
                instance_resource=instance,
            )

            # Wait for operation to complete
            from google.cloud.compute_v1.services.zone_operations import ZoneOperationsClient

            ops_client = ZoneOperationsClient()
            while operation.status != compute_v1.Operation.Status.DONE:
                operation = ops_client.get(
                    project=self._project_id,
                    zone=self._zone,
                    operation=operation.name,
                )

            # Get the created instance
            created_instance = client.get(
                project=self._project_id,
                zone=self._zone,
                instance=instance_name,
            )

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
                connector_id=self.connector.id,
                status=ProxyStatus.INITIALIZING,
                tags=["gcp", self._zone],
                metadata={"instance_name": instance_name},
            )

            logger.info("Created GCP proxy instance", instance_name=instance_name, host=host)
            return proxy

        except Exception as e:
            logger.error("Failed to create GCP instance", error=str(e))
            return None

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate a GCP Compute Engine instance."""
        client = self._get_compute_client()
        if not client or not self._project_id:
            return False

        try:
            # Remove gcp- prefix if present
            instance_name = instance_id.replace("gcp-", "")
            client.delete(
                project=self._project_id,
                zone=self._zone,
                instance=instance_name,
            )
            logger.info("Terminated GCP instance", instance_name=instance_name)
            return True
        except Exception as e:
            logger.error("Failed to terminate GCP instance", error=str(e))
            return False


class AzureProvider(CloudProvider):
    """Azure VM proxy provider.

    Requires azure-mgmt-compute and azure-identity to be installed (pip install octoprox[cloud]).

    Connector config options:
        - subscription_id: Azure subscription ID (required)
        - resource_group: Resource group name (required)
        - location: Azure region (default: eastus)
        - vm_size: VM size (default: Standard_B1s)
        - instance_name: Name prefix for created VMs (required)
        - vnet_name: Virtual network name (required for create)
        - subnet_name: Subnet name (required for create)
        - tags: Custom tags to apply to VMs (optional, dict)

    Credential config (secrets):
        - client_id: Azure service principal client ID
        - client_secret: Azure service principal client secret
        - tenant_id: Azure tenant ID
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)
        # Azure-specific config from connector
        self._subscription_id = connector.config.get("subscription_id")
        self._resource_group = connector.config.get("resource_group")
        self._location = connector.config.get("location", "eastus")
        self._vm_size = connector.config.get("vm_size", "Standard_B1s")
        self._instance_name = connector.config.get("instance_name")
        self._vnet_name = connector.config.get("vnet_name")
        self._subnet_name = connector.config.get("subnet_name")
        self._tags = connector.config.get("tags", {})
        self._compute_client = None
        self._network_client = None

    def _get_azure_clients(self):
        """Get or create Azure clients."""
        try:
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient

            if self._compute_client is None:
                # Check if service principal credentials are provided
                client_id = self.credential.config.get("client_id")
                client_secret = self.credential.config.get("client_secret")
                tenant_id = self.credential.config.get("tenant_id")

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

                self._compute_client = ComputeManagementClient(
                    azure_credential, self._subscription_id
                )
                self._network_client = NetworkManagementClient(
                    azure_credential, self._subscription_id
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

        if not self._vnet_name or not self._subnet_name:
            logger.error("vnet_name and subnet_name required for VM creation")
            return None

        if not self._instance_name:
            logger.error("instance_name required in connector config for VM creation")
            return None

        try:
            import base64
            import time

            # Build VM name with timestamp for uniqueness
            vm_name = f"{self._instance_name}-{int(time.time())}"

            # Get subnet
            subnet = network_client.subnets.get(
                self._resource_group, self._vnet_name, self._subnet_name
            )

            # Create public IP
            pip_name = f"{vm_name}-pip"
            pip_params = {
                "location": self._location,
                "sku": {"name": "Basic"},
                "public_ip_allocation_method": "Dynamic",
            }
            pip_result = network_client.public_ip_addresses.begin_create_or_update(
                self._resource_group, pip_name, pip_params
            ).result()

            # Create NIC
            nic_name = f"{vm_name}-nic"
            nic_params = {
                "location": self._location,
                "ip_configurations": [
                    {
                        "name": "ipconfig1",
                        "subnet": {"id": subnet.id},
                        "public_ip_address": {"id": pip_result.id},
                    }
                ],
            }
            nic_result = network_client.network_interfaces.begin_create_or_update(
                self._resource_group, nic_name, nic_params
            ).result()

            # Use the external Squid setup script
            custom_data = get_squid_setup_script()
            custom_data_b64 = base64.b64encode(custom_data.encode()).decode()

            # Build tags: start with required tags, then add custom tags
            tags = {
                "managed-by": "octoprox",
            }
            # Add custom tags from connector config
            for key, value in self._tags.items():
                tags[key] = str(value)

            # Create VM
            vm_params = {
                "location": self._location,
                "tags": tags,
                "hardware_profile": {"vm_size": self._vm_size},
                "storage_profile": {
                    "image_reference": {
                        "publisher": "Canonical",
                        "offer": "0001-com-ubuntu-server-jammy",
                        "sku": "22_04-lts",
                        "version": "latest",
                    },
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
                        "disable_password_authentication": True,
                        "ssh": {
                            "public_keys": []  # Would need SSH key config
                        },
                    },
                },
                "network_profile": {
                    "network_interfaces": [{"id": nic_result.id, "primary": True}]
                },
            }

            vm_result = compute_client.virtual_machines.begin_create_or_update(
                self._resource_group, vm_name, vm_params
            ).result()

            # Get public IP (may need to wait for allocation)
            pip = network_client.public_ip_addresses.get(self._resource_group, pip_name)
            host = pip.ip_address

            if not host:
                logger.warning("VM created but public IP not yet allocated")
                # Try to get private IP
                nic = network_client.network_interfaces.get(self._resource_group, nic_name)
                host = nic.ip_configurations[0].private_ip_address

            if not host:
                logger.error("VM created but no IP address available")
                return None

            proxy = Proxy(
                id=f"azure-{vm_name}",
                host=host,
                port=PROXY_PORT,
                connector_id=self.connector.id,
                status=ProxyStatus.INITIALIZING,
                tags=["azure", self._location],
                metadata={"vm_name": vm_name, "vm_id": vm_result.vm_id},
            )

            logger.info("Created Azure proxy VM", vm_name=vm_name, host=host)
            return proxy

        except Exception as e:
            logger.error("Failed to create Azure VM", error=str(e))
            return None

    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate an Azure VM."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client:
            return False

        try:
            # Remove azure- prefix if present
            vm_name = instance_id.replace("azure-", "")

            # Delete VM
            compute_client.virtual_machines.begin_delete(
                self._resource_group, vm_name
            ).result()

            # Clean up NIC and public IP
            nic_name = f"{vm_name}-nic"
            pip_name = f"{vm_name}-pip"

            try:
                network_client.network_interfaces.begin_delete(
                    self._resource_group, nic_name
                ).result()
            except Exception:
                pass

            try:
                network_client.public_ip_addresses.begin_delete(
                    self._resource_group, pip_name
                ).result()
            except Exception:
                pass

            logger.info("Terminated Azure VM", vm_name=vm_name)
            return True
        except Exception as e:
            logger.error("Failed to terminate Azure VM", error=str(e))
            return False

