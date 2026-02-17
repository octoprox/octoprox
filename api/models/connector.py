"""Connector model definitions."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.core import utc_now

from api.models.credential import CredentialType


# --- Cloud Provider Options ---

AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "af-south-1", "ap-east-1", "ap-south-1", "ap-south-2",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ca-central-1", "ca-west-1",
    "eu-central-1", "eu-central-2", "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-south-1", "eu-south-2", "eu-north-1",
    "il-central-1", "me-south-1", "me-central-1",
    "sa-east-1",
]

AWS_INSTANCE_TYPES = [
    "t2.micro", "t2.small", "t2.medium", "t2.large", "t2.xlarge", "t2.2xlarge",
    "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge", "t3.2xlarge",
    "t3a.micro", "t3a.small", "t3a.medium", "t3a.large", "t3a.xlarge", "t3a.2xlarge",
    "m5.large", "m5.xlarge", "m5.2xlarge", "m5.4xlarge",
    "m6i.large", "m6i.xlarge", "m6i.2xlarge", "m6i.4xlarge",
    "c5.large", "c5.xlarge", "c5.2xlarge", "c5.4xlarge",
    "c6i.large", "c6i.xlarge", "c6i.2xlarge", "c6i.4xlarge",
]

GCP_REGIONS = [
    "us-central1", "us-east1", "us-east4", "us-east5", "us-south1", "us-west1", "us-west2", "us-west3", "us-west4",
    "northamerica-northeast1", "northamerica-northeast2", "southamerica-east1", "southamerica-west1",
    "europe-central2", "europe-north1", "europe-southwest1", "europe-west1", "europe-west2", "europe-west3",
    "europe-west4", "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12",
    "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2", "asia-northeast3",
    "asia-south1", "asia-south2", "asia-southeast1", "asia-southeast2",
    "australia-southeast1", "australia-southeast2",
    "me-central1", "me-central2", "me-west1",
    "africa-south1",
]

GCP_MACHINE_TYPES = [
    "e2-micro", "e2-small", "e2-medium", "e2-standard-2", "e2-standard-4", "e2-standard-8",
    "n1-standard-1", "n1-standard-2", "n1-standard-4", "n1-standard-8",
    "n2-standard-2", "n2-standard-4", "n2-standard-8", "n2-standard-16",
    "n2d-standard-2", "n2d-standard-4", "n2d-standard-8", "n2d-standard-16",
    "c2-standard-4", "c2-standard-8", "c2-standard-16",
    "c3-standard-4", "c3-standard-8", "c3-standard-22",
]

AZURE_REGIONS = [
    "eastus", "eastus2", "southcentralus", "westus2", "westus3",
    "australiaeast", "southeastasia", "northeurope", "swedencentral", "uksouth",
    "westeurope", "centralus", "southafricanorth", "centralindia", "eastasia",
    "japaneast", "koreacentral", "canadacentral", "francecentral", "germanywestcentral",
    "italynorth", "norwayeast", "polandcentral", "switzerlandnorth", "uaenorth",
    "brazilsouth", "israelcentral", "qatarcentral", "centralusstage", "eastusstage",
    "westus", "northcentralus", "westcentralus", "australiasoutheast", "japanwest",
    "koreasouth", "southindia", "westindia", "canadaeast", "ukwest",
]

AZURE_VM_SIZES = [
    "Standard_B1s", "Standard_B1ms", "Standard_B2s", "Standard_B2ms", "Standard_B4ms",
    "Standard_D2s_v3", "Standard_D4s_v3", "Standard_D8s_v3", "Standard_D16s_v3",
    "Standard_D2s_v4", "Standard_D4s_v4", "Standard_D8s_v4", "Standard_D16s_v4",
    "Standard_D2s_v5", "Standard_D4s_v5", "Standard_D8s_v5", "Standard_D16s_v5",
    "Standard_E2s_v3", "Standard_E4s_v3", "Standard_E8s_v3",
    "Standard_F2s_v2", "Standard_F4s_v2", "Standard_F8s_v2", "Standard_F16s_v2",
]


# --- Typed Config Models for Validation ---

class StaticProxyProviderConnectorConfig(BaseModel):
    """Configuration for Static Proxy Provider connectors (no additional config needed)."""
    pass


class CloudConnectorConfig(BaseModel):
    """Base configuration for cloud connectors with common scaling fields."""
    min_proxies: int = 1
    max_proxies: int = 10
    min_rotation_period_minutes: int = 60
    max_rotation_period_minutes: int = 1440


class AWSConnectorConfig(CloudConnectorConfig):
    """Configuration for AWS connectors."""
    instance_name: str
    region: str
    instance_type: str
    key_pair_name: str
    security_group: str
    ami_id: str
    tags: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AWSConnectorConfig':
        """Ensure required fields are not empty."""
        if not self.instance_name or not self.instance_name.strip():
            raise ValueError('instance_name is required and cannot be empty')
        if not self.region or not self.region.strip():
            raise ValueError('region is required and cannot be empty')
        if not self.instance_type or not self.instance_type.strip():
            raise ValueError('instance_type is required and cannot be empty')
        if not self.key_pair_name or not self.key_pair_name.strip():
            raise ValueError('key_pair_name is required and cannot be empty')
        if not self.security_group or not self.security_group.strip():
            raise ValueError('security_group is required and cannot be empty')
        if not self.ami_id or not self.ami_id.strip():
            raise ValueError('ami_id is required and cannot be empty')
        return self


class GCPConnectorConfig(CloudConnectorConfig):
    """Configuration for GCP connectors."""
    project_id: str
    instance_name: str
    zone: str = "us-central1-a"
    machine_type: str = "e2-micro"
    network: str = "default"
    subnetwork: str | None = None
    source_image: str = "projects/debian-cloud/global/images/family/debian-11"
    ssh_key: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'GCPConnectorConfig':
        """Ensure required fields are not empty."""
        if not self.project_id or not self.project_id.strip():
            raise ValueError('project_id is required and cannot be empty')
        if not self.instance_name or not self.instance_name.strip():
            raise ValueError('instance_name is required and cannot be empty')
        if not self.zone or not self.zone.strip():
            raise ValueError('zone is required and cannot be empty')
        if not self.machine_type or not self.machine_type.strip():
            raise ValueError('machine_type is required and cannot be empty')
        return self


class AzureConnectorConfig(CloudConnectorConfig):
    """Configuration for Azure connectors."""
    subscription_id: str
    resource_group: str
    instance_name: str
    location: str = "eastus"
    vm_size: str = "Standard_B1s"
    vnet_name: str | None = None
    subnet_name: str | None = None
    ssh_public_key: str
    tags: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AzureConnectorConfig':
        """Ensure required fields are not empty."""
        if not self.subscription_id or not self.subscription_id.strip():
            raise ValueError('subscription_id is required and cannot be empty')
        if not self.resource_group or not self.resource_group.strip():
            raise ValueError('resource_group is required and cannot be empty')
        if not self.instance_name or not self.instance_name.strip():
            raise ValueError('instance_name is required and cannot be empty')
        if not self.vm_size or not self.vm_size.strip():
            raise ValueError('vm_size is required and cannot be empty')
        if not self.ssh_public_key or not self.ssh_public_key.strip():
            raise ValueError('ssh_public_key is required and cannot be empty')
        return self


def validate_connector_config(credential_type: CredentialType, config: dict[str, Any]) -> dict[str, Any]:
    """Validate connector config based on credential type and return validated config."""
    if credential_type == CredentialType.STATIC_PROXY_PROVIDER:
        validated = StaticProxyProviderConnectorConfig(**config)
    elif credential_type == CredentialType.AWS:
        validated = AWSConnectorConfig(**config)
    elif credential_type == CredentialType.GCP:
        validated = GCPConnectorConfig(**config)
    elif credential_type == CredentialType.AZURE:
        validated = AzureConnectorConfig(**config)
    else:
        raise ValueError(f"Unknown credential type: {credential_type}")
    return validated.model_dump(exclude_none=True)


def get_cloud_config(
    credential_type: CredentialType, config: dict[str, Any]
) -> CloudConnectorConfig | None:
    """Get a typed cloud config model from a raw config dict.

    Returns the appropriate CloudConnectorConfig subclass for cloud providers
    (AWS, GCP, Azure), or None for non-cloud providers.

    Args:
        credential_type: The type of credential.
        config: The raw configuration dictionary.

    Returns:
        A CloudConnectorConfig (or subclass) if it's a cloud provider, None otherwise.
    """
    if credential_type == CredentialType.AWS:
        return AWSConnectorConfig(**config)
    elif credential_type == CredentialType.GCP:
        return GCPConnectorConfig(**config)
    elif credential_type == CredentialType.AZURE:
        return AzureConnectorConfig(**config)
    else:
        return None


class Connector(BaseModel):
    """Represents a connector that provides proxies."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    credential_id: str
    credential_type: CredentialType
    project_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    # Statistics
    proxy_count: int = 0

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def cloud_config(self) -> CloudConnectorConfig | None:
        """Get the cloud config if this is a cloud connector, None otherwise."""
        return get_cloud_config(self.credential_type, self.config)


class ConnectorCreate(BaseModel):
    """Schema for creating a new connector."""
    name: str
    credential_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # Note: config validation happens in the route after fetching credential type


class ConnectorUpdate(BaseModel):
    """Schema for updating a connector."""
    name: str | None = None
    credential_id: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class ConnectorResponse(BaseModel):
    """Schema for connector API responses."""
    id: str
    name: str
    credential_id: str
    credential_name: str | None = None
    credential_type: str | None = None
    project_id: str
    config: dict[str, Any]
    enabled: bool
    proxy_count: int
    created_at: datetime
    updated_at: datetime


# --- Options Response for Frontend ---

class ConnectorOptionsResponse(BaseModel):
    """Response containing available options for connector configuration."""
    aws_regions: list[str] = AWS_REGIONS
    aws_instance_types: list[str] = AWS_INSTANCE_TYPES
    gcp_regions: list[str] = GCP_REGIONS
    gcp_machine_types: list[str] = GCP_MACHINE_TYPES
    azure_regions: list[str] = AZURE_REGIONS
    azure_vm_sizes: list[str] = AZURE_VM_SIZES

