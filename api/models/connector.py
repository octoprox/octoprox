# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.core import utc_now
from api.models.cloud_options import (
    AWS_INSTANCE_TYPES,
    AWS_REGIONS,
    AWS_UBUNTU_AMIS,
    AZURE_LOCATIONS,
    AZURE_UBUNTU_IMAGE_ARM,
    AZURE_UBUNTU_IMAGE_X86,
    AZURE_VM_SIZES,
    GCP_MACHINE_TYPES,
    GCP_UBUNTU_IMAGE_ARM,
    GCP_UBUNTU_IMAGE_X86,
    GCP_ZONES,
    OXYLABS_COUNTRIES,
    BrightDataZone,
    CountryOption,
    InstanceTypeOption,
    RegionOption,
    get_aws_architecture,
    get_azure_architecture,
    get_gcp_architecture,
)
from api.models.credential import CredentialType, OxylabsProxyType

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
    """Configuration for AWS connectors.

    Note: AMI is automatically determined based on region and instance type
    (architecture). Ubuntu 24.04 LTS is used for all instances.
    """
    instance_name: str
    region: str
    instance_type: str
    key_pair_name: str
    security_group: str
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
        return self

    def get_ami(self) -> str | None:
        """Get the Ubuntu 24.04 LTS AMI ID for this config's region and instance type.

        Returns None if the region/architecture combination is not supported.
        """
        arch = get_aws_architecture(self.instance_type)
        return AWS_UBUNTU_AMIS.get((self.region, arch))


class GCPConnectorConfig(CloudConnectorConfig):
    """Configuration for GCP connectors.

    Note: Source image is automatically determined based on machine type
    (architecture). Ubuntu 24.04 LTS is used for all instances.
    """
    project_id: str
    instance_name: str
    zone: str = "us-central1-a"
    machine_type: str = "e2-micro"
    network: str = "default"
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

    def get_source_image(self) -> str:
        """Get the Ubuntu 24.04 LTS source image for this config's machine type."""
        arch = get_gcp_architecture(self.machine_type)
        if arch == "arm64":
            return GCP_UBUNTU_IMAGE_ARM
        return GCP_UBUNTU_IMAGE_X86


class AzureConnectorConfig(CloudConnectorConfig):
    """Configuration for Azure connectors."""
    subscription_id: str
    resource_group: str
    instance_name: str
    location: str = "eastus"
    vm_size: str = "Standard_B2ls_v2"
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

    def get_image_reference(self) -> dict[str, str]:
        """Get the Ubuntu 24.04 LTS image reference for this config's VM size."""
        arch = get_azure_architecture(self.vm_size)
        if arch == "arm64":
            return AZURE_UBUNTU_IMAGE_ARM.copy()
        return AZURE_UBUNTU_IMAGE_X86.copy()


class OxylabsConnectorConfig(BaseModel):
    """Configuration for Oxylabs connectors.

    Supports both session-based (residential, mobile) and port-based
    (isp, dedicated_isp, datacenter, datacenter_dedicated) proxy types.
    """
    num_proxies: int = Field(default=1, ge=1, le=1000)
    # Country code is only used for session-based types (residential, mobile)
    # None or empty string means "All" (no geo-targeting)
    country_code: str | None = None
    # Session duration is only used for session-based types (residential, mobile)
    # Default 10 minutes, max 30 minutes
    session_duration_minutes: int = Field(default=10, ge=1, le=30)

    @model_validator(mode='after')
    def validate_fields(self) -> 'OxylabsConnectorConfig':
        """Validate connector fields."""
        if self.country_code is not None:
            # Normalize empty string to None
            if not self.country_code.strip():
                self.country_code = None
            else:
                # Validate country code format (2 uppercase letters)
                code = self.country_code.strip().upper()
                if len(code) != 2 or not code.isalpha():
                    raise ValueError('country_code must be a 2-letter country code (e.g., US, GB)')
                self.country_code = code
        return self

    def is_session_based(self, proxy_type: OxylabsProxyType) -> bool:
        """Check if the proxy type uses session-based routing."""
        return proxy_type in (OxylabsProxyType.RESIDENTIAL, OxylabsProxyType.MOBILE)


class BrightDataProxyType(str, Enum):
    """Types of BrightData proxies."""
    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    ISP = "isp"
    DATACENTER = "datacenter"


class BrightDataConnectorConfig(BaseModel):
    """Configuration for BrightData connectors.

    Supports both session-based (residential, mobile) and port-based
    (isp, datacenter) proxy types. All types support country selection.
    """
    zone_name: str  # User-editable
    zone_password: str  # User-editable, auto-populated from API
    proxy_type: BrightDataProxyType  # Derived from zone, fixed after creation
    num_proxies: int = Field(default=1, ge=1, le=1000)
    country_code: str | None = None  # Optional, all types support it
    healthcheck_url: str | None = None  # Optional custom healthcheck URL (default: https://httpbin.org/ip)

    @model_validator(mode='after')
    def validate_fields(self) -> 'BrightDataConnectorConfig':
        """Validate connector fields."""
        if not self.zone_name or not self.zone_name.strip():
            raise ValueError('zone_name is required and cannot be empty')
        if not self.zone_password or not self.zone_password.strip():
            raise ValueError('zone_password is required and cannot be empty')
        # Normalize country code (same as Oxylabs)
        if self.country_code is not None:
            if not self.country_code.strip():
                self.country_code = None
            else:
                code = self.country_code.strip().upper()
                if len(code) != 2 or not code.isalpha():
                    raise ValueError('country_code must be a 2-letter country code')
                self.country_code = code
        return self

    def is_session_based(self) -> bool:
        """Check if the proxy type uses session-based routing."""
        return self.proxy_type in (BrightDataProxyType.RESIDENTIAL, BrightDataProxyType.MOBILE)


def validate_connector_config(credential_type: CredentialType, config: dict[str, Any]) -> dict[str, Any]:
    """Validate connector config based on credential type and return validated config."""
    validated: StaticProxyProviderConnectorConfig | AWSConnectorConfig | GCPConnectorConfig | AzureConnectorConfig | OxylabsConnectorConfig | BrightDataConnectorConfig
    if credential_type == CredentialType.STATIC_PROXY_PROVIDER:
        validated = StaticProxyProviderConnectorConfig(**config)
    elif credential_type == CredentialType.AWS:
        validated = AWSConnectorConfig(**config)
    elif credential_type == CredentialType.GCP:
        validated = GCPConnectorConfig(**config)
    elif credential_type == CredentialType.AZURE:
        validated = AzureConnectorConfig(**config)
    elif credential_type == CredentialType.OXYLABS:
        validated = OxylabsConnectorConfig(**config)
    elif credential_type == CredentialType.BRIGHTDATA:
        validated = BrightDataConnectorConfig(**config)
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


def get_oxylabs_config(
    credential_type: CredentialType, config: dict[str, Any]
) -> OxylabsConnectorConfig | None:
    """Get a typed Oxylabs config model from a raw config dict.

    Args:
        credential_type: The type of credential.
        config: The raw configuration dictionary.

    Returns:
        An OxylabsConnectorConfig if it's an Oxylabs provider, None otherwise.
    """
    if credential_type == CredentialType.OXYLABS:
        return OxylabsConnectorConfig(**config)
    return None


def get_brightdata_config(
    credential_type: CredentialType, config: dict[str, Any]
) -> BrightDataConnectorConfig | None:
    """Get a typed BrightData config model from a raw config dict.

    Args:
        credential_type: The type of credential.
        config: The raw configuration dictionary.

    Returns:
        A BrightDataConnectorConfig if it's a BrightData provider, None otherwise.
    """
    if credential_type == CredentialType.BRIGHTDATA:
        return BrightDataConnectorConfig(**config)
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
    pending_deletion: bool = False  # Set when connector is marked for async deletion

    # Cloud provider error tracking
    last_error: str | None = None  # Last error message from cloud provider operations
    last_error_at: datetime | None = None  # When the error occurred
    consecutive_errors: int = 0  # Count of consecutive failures (for backoff)

    # Statistics (computed dynamically, not persisted)
    proxy_count: int = 0

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def cloud_config(self) -> CloudConnectorConfig | None:
        """Get the cloud config if this is a cloud connector, None otherwise."""
        return get_cloud_config(self.credential_type, self.config)

    @property
    def oxylabs_config(self) -> OxylabsConnectorConfig | None:
        """Get the Oxylabs config if this is an Oxylabs connector, None otherwise."""
        return get_oxylabs_config(self.credential_type, self.config)

    @property
    def brightdata_config(self) -> BrightDataConnectorConfig | None:
        """Get the BrightData config if this is a BrightData connector, None otherwise."""
        return get_brightdata_config(self.credential_type, self.config)


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
    # Cloud provider error tracking
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_errors: int = 0
    created_at: datetime
    updated_at: datetime


# --- Options Response for Frontend ---

class ConnectorOptionsResponse(BaseModel):
    """Response containing available options for connector configuration.

    All options are rich objects with metadata for frontend display:
    - Regions/zones/locations include code and friendly name
    - Instance types include code, vCPUs, memory, architecture, and description
    - Countries include code and friendly name for Oxylabs/BrightData geo-targeting
    - BrightData zones include name, type, and password
    """
    aws_regions: list[RegionOption] = Field(default_factory=lambda: AWS_REGIONS)
    aws_instance_types: list[InstanceTypeOption] = Field(default_factory=lambda: AWS_INSTANCE_TYPES)
    gcp_zones: list[RegionOption] = Field(default_factory=lambda: GCP_ZONES)
    gcp_machine_types: list[InstanceTypeOption] = Field(default_factory=lambda: GCP_MACHINE_TYPES)
    azure_locations: list[RegionOption] = Field(default_factory=lambda: AZURE_LOCATIONS)
    azure_vm_sizes: list[InstanceTypeOption] = Field(default_factory=lambda: AZURE_VM_SIZES)
    oxylabs_countries: list[CountryOption] = Field(default_factory=lambda: OXYLABS_COUNTRIES)
    brightdata_zones: list[BrightDataZone] = Field(default_factory=list)

