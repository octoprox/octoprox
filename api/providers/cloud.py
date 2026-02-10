"""Cloud provider integrations for dynamic proxy instances.

This module provides base classes and stubs for cloud provider integrations.
Actual implementations require the 'cloud' optional dependencies.
"""

from abc import abstractmethod

import structlog

from api.models.proxy import Proxy
from api.models.source import ProxySource
from api.providers.base import ProxyProvider

logger = structlog.get_logger()


class CloudProvider(ProxyProvider):
    """Base class for cloud-based proxy providers."""
    
    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
        self._region = source.config.get("region", "us-east-1")
        self._instance_type = source.config.get("instance_type", "t3.micro")
        self._max_instances = source.config.get("max_instances", 5)
    
    @abstractmethod
    async def create_instance(self) -> Proxy | None:
        """Create a new proxy instance in the cloud."""
        ...
    
    @abstractmethod
    async def terminate_instance(self, instance_id: str) -> bool:
        """Terminate a cloud proxy instance."""
        ...
    
    @abstractmethod
    async def list_instances(self) -> list[dict]:
        """List all proxy instances."""
        ...


class AWSProvider(CloudProvider):
    """AWS EC2-based proxy provider.

    Requires boto3 to be installed (pip install octoprox[cloud]).

    Config options:
        - region: AWS region (default: us-east-1)
        - instance_type: EC2 instance type (default: t3.micro)
        - max_instances: Maximum number of proxy instances (default: 5)
        - ami_id: AMI ID for proxy instances (required for create)
        - security_group_id: Security group ID (required for create)
        - subnet_id: Subnet ID (optional)
        - key_name: SSH key pair name (optional)
        - proxy_port: Port the proxy runs on (default: 8080)
        - tag_filter: Tag to identify proxy instances (default: octoprox-proxy)
    """

    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
        self._ami_id = source.config.get("ami_id")
        self._security_group_id = source.config.get("security_group_id")
        self._subnet_id = source.config.get("subnet_id")
        self._key_name = source.config.get("key_name")
        self._proxy_port = source.config.get("proxy_port", 8080)
        self._tag_filter = source.config.get("tag_filter", "octoprox-proxy")
        self._ec2_client = None
        self._ec2_resource = None

    def _get_boto3_clients(self):
        """Get or create boto3 clients."""
        try:
            import boto3
            if self._ec2_client is None:
                self._ec2_client = boto3.client("ec2", region_name=self._region)
                self._ec2_resource = boto3.resource("ec2", region_name=self._region)
            return self._ec2_client, self._ec2_resource
        except ImportError:
            logger.error("boto3 not installed. Install with: pip install octoprox[cloud]")
            return None, None

    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from AWS EC2 instances tagged as proxies."""
        ec2_client, _ = self._get_boto3_clients()
        if not ec2_client:
            return []

        proxies: list[Proxy] = []

        try:
            # Find instances with our tag that are running
            response = ec2_client.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": [self._tag_filter]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )

            for reservation in response.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = instance["InstanceId"]
                    public_ip = instance.get("PublicIpAddress")
                    private_ip = instance.get("PrivateIpAddress")

                    # Prefer public IP, fall back to private
                    host = public_ip or private_ip
                    if not host:
                        continue

                    proxy = Proxy(
                        id=f"aws-{instance_id}",
                        host=host,
                        port=self._proxy_port,
                        source_id=self.source.id,
                        tags=["aws", self._region],
                        metadata={
                            "instance_id": instance_id,
                            "instance_type": instance.get("InstanceType"),
                            "availability_zone": instance["Placement"]["AvailabilityZone"],
                            "launch_time": str(instance.get("LaunchTime")),
                        },
                    )
                    proxies.append(proxy)

            logger.info("Fetched AWS proxies", count=len(proxies), region=self._region)

        except Exception as e:
            logger.error("Failed to fetch AWS proxies", error=str(e))

        return proxies

    async def validate(self) -> bool:
        """Validate AWS configuration and credentials."""
        ec2_client, _ = self._get_boto3_clients()
        if not ec2_client:
            return False

        try:
            # Try to describe instances to validate credentials
            ec2_client.describe_instances(MaxResults=5)
            return True
        except Exception as e:
            logger.error("AWS validation failed", error=str(e))
            return False

    async def create_instance(self) -> Proxy | None:
        """Create a new EC2 instance configured as a proxy."""
        _, ec2_resource = self._get_boto3_clients()
        if not ec2_resource:
            return None

        if not self._ami_id or not self._security_group_id:
            logger.error("ami_id and security_group_id required for instance creation")
            return None

        try:
            # User data script to set up a simple proxy
            user_data = f"""#!/bin/bash
yum update -y || apt-get update -y
yum install -y squid || apt-get install -y squid
sed -i 's/http_port 3128/http_port {self._proxy_port}/' /etc/squid/squid.conf
echo "http_access allow all" >> /etc/squid/squid.conf
systemctl enable squid
systemctl start squid
"""

            run_args = {
                "ImageId": self._ami_id,
                "InstanceType": self._instance_type,
                "MinCount": 1,
                "MaxCount": 1,
                "SecurityGroupIds": [self._security_group_id],
                "UserData": user_data,
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": self._tag_filter},
                            {"Key": "ManagedBy", "Value": "octoprox"},
                        ],
                    }
                ],
            }

            if self._subnet_id:
                run_args["SubnetId"] = self._subnet_id
            if self._key_name:
                run_args["KeyName"] = self._key_name

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
                port=self._proxy_port,
                source_id=self.source.id,
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

    async def list_instances(self) -> list[dict]:
        """List all EC2 proxy instances."""
        ec2_client, _ = self._get_boto3_clients()
        if not ec2_client:
            return []

        try:
            response = ec2_client.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": [self._tag_filter]},
                ]
            )

            instances = []
            for reservation in response.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instances.append({
                        "id": instance["InstanceId"],
                        "state": instance["State"]["Name"],
                        "type": instance.get("InstanceType"),
                        "public_ip": instance.get("PublicIpAddress"),
                        "private_ip": instance.get("PrivateIpAddress"),
                        "launch_time": str(instance.get("LaunchTime")),
                    })

            return instances
        except Exception as e:
            logger.error("Failed to list AWS instances", error=str(e))
            return []


class GCPProvider(CloudProvider):
    """Google Cloud Compute Engine proxy provider.

    Requires google-cloud-compute to be installed (pip install octoprox[cloud]).

    Config options:
        - project_id: GCP project ID (required)
        - region: GCP region (default: us-central1)
        - zone: GCP zone (default: us-central1-a)
        - machine_type: Machine type (default: e2-micro)
        - max_instances: Maximum number of proxy instances (default: 5)
        - network: VPC network name (default: default)
        - subnet: Subnet name (optional)
        - proxy_port: Port the proxy runs on (default: 8080)
        - label_filter: Label to identify proxy instances (default: octoprox-proxy)
        - source_image: Source image for instances (default: debian-cloud/debian-11)
    """

    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
        self._project_id = source.config.get("project_id")
        self._zone = source.config.get("zone", "us-central1-a")
        self._machine_type = source.config.get("machine_type", "e2-micro")
        self._network = source.config.get("network", "default")
        self._subnet = source.config.get("subnet")
        self._proxy_port = source.config.get("proxy_port", 8080)
        self._label_filter = source.config.get("label_filter", "octoprox-proxy")
        self._source_image = source.config.get(
            "source_image", "projects/debian-cloud/global/images/family/debian-11"
        )
        self._instances_client = None

    def _get_compute_client(self):
        """Get or create GCP compute client."""
        try:
            from google.cloud import compute_v1
            if self._instances_client is None:
                self._instances_client = compute_v1.InstancesClient()
            return self._instances_client
        except ImportError:
            logger.error("google-cloud-compute not installed. Install with: pip install octoprox[cloud]")
            return None

    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from GCP Compute Engine instances."""
        client = self._get_compute_client()
        if not client or not self._project_id:
            return []

        proxies: list[Proxy] = []

        try:
            # List instances with our label
            request = {
                "project": self._project_id,
                "zone": self._zone,
                "filter": f"labels.managed-by=octoprox AND labels.role={self._label_filter}",
            }

            instances = client.list(**request)

            for instance in instances:
                if instance.status != "RUNNING":
                    continue

                # Get external IP if available
                external_ip = None
                internal_ip = None

                for interface in instance.network_interfaces:
                    internal_ip = interface.network_i_p
                    for access_config in interface.access_configs:
                        if access_config.nat_i_p:
                            external_ip = access_config.nat_i_p
                            break

                host = external_ip or internal_ip
                if not host:
                    continue

                proxy = Proxy(
                    id=f"gcp-{instance.name}",
                    host=host,
                    port=self._proxy_port,
                    source_id=self.source.id,
                    tags=["gcp", self._zone],
                    metadata={
                        "instance_name": instance.name,
                        "machine_type": instance.machine_type,
                        "zone": self._zone,
                        "creation_timestamp": instance.creation_timestamp,
                    },
                )
                proxies.append(proxy)

            logger.info("Fetched GCP proxies", count=len(proxies), zone=self._zone)

        except Exception as e:
            logger.error("Failed to fetch GCP proxies", error=str(e))

        return proxies

    async def validate(self) -> bool:
        """Validate GCP configuration and credentials."""
        client = self._get_compute_client()
        if not client:
            return False

        if not self._project_id:
            logger.error("GCP project_id is required")
            return False

        try:
            # Try to list instances to validate credentials
            request = {"project": self._project_id, "zone": self._zone, "max_results": 1}
            list(client.list(**request))
            return True
        except Exception as e:
            logger.error("GCP validation failed", error=str(e))
            return False

    async def create_instance(self) -> Proxy | None:
        """Create a new GCP Compute Engine instance configured as a proxy."""
        client = self._get_compute_client()
        if not client or not self._project_id:
            return None

        try:
            from google.cloud import compute_v1

            instance_name = f"octoprox-proxy-{int(__import__('time').time())}"

            # Startup script to set up proxy
            startup_script = f"""#!/bin/bash
apt-get update
apt-get install -y squid
sed -i 's/http_port 3128/http_port {self._proxy_port}/' /etc/squid/squid.conf
echo "http_access allow all" >> /etc/squid/squid.conf
systemctl enable squid
systemctl start squid
"""

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
                network_interface.subnetwork = f"regions/{self._region}/subnetworks/{self._subnet}"

            # Add external IP
            access_config = compute_v1.AccessConfig()
            access_config.name = "External NAT"
            access_config.type_ = "ONE_TO_ONE_NAT"
            network_interface.access_configs = [access_config]
            instance.network_interfaces = [network_interface]

            # Labels
            instance.labels = {
                "managed-by": "octoprox",
                "role": self._label_filter,
            }

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
                port=self._proxy_port,
                source_id=self.source.id,
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

    async def list_instances(self) -> list[dict]:
        """List all GCP proxy instances."""
        client = self._get_compute_client()
        if not client or not self._project_id:
            return []

        try:
            request = {
                "project": self._project_id,
                "zone": self._zone,
                "filter": f"labels.managed-by=octoprox AND labels.role={self._label_filter}",
            }

            instances = []
            for instance in client.list(**request):
                external_ip = None
                internal_ip = None

                for interface in instance.network_interfaces:
                    internal_ip = interface.network_i_p
                    for access_config in interface.access_configs:
                        if access_config.nat_i_p:
                            external_ip = access_config.nat_i_p

                instances.append({
                    "id": instance.name,
                    "state": instance.status,
                    "machine_type": instance.machine_type.split("/")[-1],
                    "external_ip": external_ip,
                    "internal_ip": internal_ip,
                    "creation_timestamp": instance.creation_timestamp,
                })

            return instances
        except Exception as e:
            logger.error("Failed to list GCP instances", error=str(e))
            return []


class AzureProvider(CloudProvider):
    """Azure VM proxy provider.

    Requires azure-mgmt-compute and azure-identity to be installed (pip install octoprox[cloud]).

    Config options:
        - subscription_id: Azure subscription ID (required)
        - resource_group: Resource group name (required)
        - location: Azure region (default: eastus)
        - vm_size: VM size (default: Standard_B1s)
        - max_instances: Maximum number of proxy instances (default: 5)
        - vnet_name: Virtual network name (required for create)
        - subnet_name: Subnet name (required for create)
        - proxy_port: Port the proxy runs on (default: 8080)
        - tag_filter: Tag to identify proxy VMs (default: octoprox-proxy)
        - image_reference: VM image reference (default: Ubuntu 22.04 LTS)
    """

    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
        self._subscription_id = source.config.get("subscription_id")
        self._resource_group = source.config.get("resource_group")
        self._location = source.config.get("location", "eastus")
        self._vm_size = source.config.get("vm_size", "Standard_B1s")
        self._vnet_name = source.config.get("vnet_name")
        self._subnet_name = source.config.get("subnet_name")
        self._proxy_port = source.config.get("proxy_port", 8080)
        self._tag_filter = source.config.get("tag_filter", "octoprox-proxy")
        self._compute_client = None
        self._network_client = None

    def _get_azure_clients(self):
        """Get or create Azure clients."""
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient

            if self._compute_client is None:
                credential = DefaultAzureCredential()
                self._compute_client = ComputeManagementClient(
                    credential, self._subscription_id
                )
                self._network_client = NetworkManagementClient(
                    credential, self._subscription_id
                )
            return self._compute_client, self._network_client
        except ImportError:
            logger.error(
                "Azure SDK not installed. Install with: pip install octoprox[cloud] azure-identity"
            )
            return None, None

    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from Azure VMs tagged as proxies."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client or not self._resource_group:
            return []

        proxies: list[Proxy] = []

        try:
            # List VMs in resource group
            vms = compute_client.virtual_machines.list(self._resource_group)

            for vm in vms:
                # Check if VM has our tag
                if not vm.tags or vm.tags.get("role") != self._tag_filter:
                    continue

                # Get VM instance view for power state
                instance_view = compute_client.virtual_machines.instance_view(
                    self._resource_group, vm.name
                )

                # Check if running
                is_running = False
                for status in instance_view.statuses:
                    if status.code == "PowerState/running":
                        is_running = True
                        break

                if not is_running:
                    continue

                # Get public IP if available
                public_ip = None
                private_ip = None

                if vm.network_profile and vm.network_profile.network_interfaces:
                    for nic_ref in vm.network_profile.network_interfaces:
                        nic_name = nic_ref.id.split("/")[-1]
                        nic = network_client.network_interfaces.get(
                            self._resource_group, nic_name
                        )

                        for ip_config in nic.ip_configurations:
                            private_ip = ip_config.private_ip_address

                            if ip_config.public_ip_address:
                                pip_name = ip_config.public_ip_address.id.split("/")[-1]
                                pip = network_client.public_ip_addresses.get(
                                    self._resource_group, pip_name
                                )
                                public_ip = pip.ip_address

                host = public_ip or private_ip
                if not host:
                    continue

                proxy = Proxy(
                    id=f"azure-{vm.name}",
                    host=host,
                    port=self._proxy_port,
                    source_id=self.source.id,
                    tags=["azure", self._location],
                    metadata={
                        "vm_name": vm.name,
                        "vm_size": vm.hardware_profile.vm_size,
                        "location": vm.location,
                        "vm_id": vm.vm_id,
                    },
                )
                proxies.append(proxy)

            logger.info("Fetched Azure proxies", count=len(proxies), location=self._location)

        except Exception as e:
            logger.error("Failed to fetch Azure proxies", error=str(e))

        return proxies

    async def validate(self) -> bool:
        """Validate Azure configuration and credentials."""
        compute_client, _ = self._get_azure_clients()
        if not compute_client:
            return False

        if not self._subscription_id or not self._resource_group:
            logger.error("Azure subscription_id and resource_group are required")
            return False

        try:
            # Try to list VMs to validate credentials
            list(compute_client.virtual_machines.list(self._resource_group))
            return True
        except Exception as e:
            logger.error("Azure validation failed", error=str(e))
            return False

    async def create_instance(self) -> Proxy | None:
        """Create a new Azure VM configured as a proxy."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client or not network_client:
            return None

        if not self._vnet_name or not self._subnet_name:
            logger.error("vnet_name and subnet_name required for VM creation")
            return None

        try:
            import time
            vm_name = f"octoprox-proxy-{int(time.time())}"

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

            # Custom data script to set up proxy
            import base64
            custom_data = f"""#!/bin/bash
apt-get update
apt-get install -y squid
sed -i 's/http_port 3128/http_port {self._proxy_port}/' /etc/squid/squid.conf
echo "http_access allow all" >> /etc/squid/squid.conf
systemctl enable squid
systemctl start squid
"""
            custom_data_b64 = base64.b64encode(custom_data.encode()).decode()

            # Create VM
            vm_params = {
                "location": self._location,
                "tags": {
                    "managed-by": "octoprox",
                    "role": self._tag_filter,
                },
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
                port=self._proxy_port,
                source_id=self.source.id,
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

    async def list_instances(self) -> list[dict]:
        """List all Azure proxy VMs."""
        compute_client, network_client = self._get_azure_clients()
        if not compute_client or not self._resource_group:
            return []

        try:
            instances = []
            vms = compute_client.virtual_machines.list(self._resource_group)

            for vm in vms:
                if not vm.tags or vm.tags.get("role") != self._tag_filter:
                    continue

                # Get power state
                instance_view = compute_client.virtual_machines.instance_view(
                    self._resource_group, vm.name
                )
                power_state = "unknown"
                for status in instance_view.statuses:
                    if status.code.startswith("PowerState/"):
                        power_state = status.code.replace("PowerState/", "")

                # Get IPs
                public_ip = None
                private_ip = None

                if vm.network_profile and vm.network_profile.network_interfaces:
                    for nic_ref in vm.network_profile.network_interfaces:
                        nic_name = nic_ref.id.split("/")[-1]
                        try:
                            nic = network_client.network_interfaces.get(
                                self._resource_group, nic_name
                            )
                            for ip_config in nic.ip_configurations:
                                private_ip = ip_config.private_ip_address
                                if ip_config.public_ip_address:
                                    pip_name = ip_config.public_ip_address.id.split("/")[-1]
                                    pip = network_client.public_ip_addresses.get(
                                        self._resource_group, pip_name
                                    )
                                    public_ip = pip.ip_address
                        except Exception:
                            pass

                instances.append({
                    "id": vm.name,
                    "state": power_state,
                    "vm_size": vm.hardware_profile.vm_size,
                    "public_ip": public_ip,
                    "private_ip": private_ip,
                    "location": vm.location,
                })

            return instances
        except Exception as e:
            logger.error("Failed to list Azure VMs", error=str(e))
            return []

