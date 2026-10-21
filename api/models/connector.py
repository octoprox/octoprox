# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector model definitions."""

import math
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from api.core import utc_now
from api.core.demand_tracker import DemandLevel
from api.models.cloud_options import (
    AWS_INSTANCE_TYPES,
    AWS_REGIONS,
    AWS_UBUNTU_AMIS,
    AZURE_LOCATIONS,
    AZURE_UBUNTU_IMAGE_ARM,
    AZURE_UBUNTU_IMAGE_X86,
    AZURE_VM_SIZES,
    COUNTRIES,
    GCP_MACHINE_TYPES,
    GCP_UBUNTU_IMAGE_ARM,
    GCP_UBUNTU_IMAGE_X86,
    GCP_ZONES,
    CountryOption,
    InstanceTypeOption,
    RegionOption,
    get_aws_architecture,
    get_azure_architecture,
    get_gcp_architecture,
)
from api.models.credential import CredentialType
from api.models.location import normalize_country_list

# Ceiling of a cloud connector's backoff after cloud errors, in minutes: 2^errors minutes up to this.
MAX_ERROR_BACKOFF_MINUTES = 30

# --- Typed Config Models for Validation ---

class StaticProxyProviderConnectorConfig(BaseModel):
    """Configuration for Static Proxy Provider connectors.

    ``countries`` declares which countries the manually added proxies exit
    from, so clients can pick them with a ``-cc-<code>`` username suffix.
    """
    countries: list[str] = Field(default_factory=list)

    @field_validator('countries', mode='before')
    @classmethod
    def _normalize_countries(cls, value: Any) -> list[str]:
        return normalize_country_list(value)


class CloudConnectorConfig(BaseModel):
    """Base configuration for cloud connectors with common scaling fields.

    ``countries`` is optional: set it when the region's instances should be
    selectable with a ``-cc-<code>`` username suffix.
    """
    min_proxies: int = 1
    max_proxies: int = 10
    min_rotation_period_minutes: int = 60
    max_rotation_period_minutes: int = 1440
    countries: list[str] = Field(default_factory=list)

    @field_validator('countries', mode='before')
    @classmethod
    def _normalize_countries(cls, value: Any) -> list[str]:
        return normalize_country_list(value)

    @property
    def max_steps(self) -> tuple[int, int]:
        """The most instances one scaling action adds and retires: a third and a sixth of the range, at least one."""
        pool_range = self.max_proxies - self.min_proxies
        return max(1, math.ceil(pool_range / 3)), max(1, math.ceil(pool_range / 6))

    def target_count(self, level: DemandLevel) -> int:
        """The pool size a demand level asks for: the minimum, the midpoint or the maximum."""
        if level == DemandLevel.LOW:
            return self.min_proxies
        if level == DemandLevel.MEDIUM:
            return (self.min_proxies + self.max_proxies) // 2
        return self.max_proxies


class AWSConnectorConfig(CloudConnectorConfig):
    """Configuration for AWS connectors: where and what to launch.

    Every instance runs Ubuntu 24.04 LTS, its AMI picked by region and by the instance type's architecture.
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
    """Configuration for GCP connectors: where and what to create.

    Every instance runs Ubuntu 24.04 LTS, its source image picked by the machine type's architecture.
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


DEFAULT_ROUTING_WEIGHT = 1
MAX_ROUTING_WEIGHT = 100


class RoutingConfig(BaseModel):
    """Routing configuration: the target domains a connector's proxies serve, and its share of the project's traffic."""
    # The domains served, or refused; at most one of the two is set. An entry
    # matches the domain and every subdomain: 'bing.com' matches 'www.bing.com'.
    domain_whitelist: list[str] = Field(default_factory=list)
    domain_blacklist: list[str] = Field(default_factory=list)
    # The share of the project's traffic against the other connectors that can
    # serve a request: a ratio, not a percentage (weights 1 and 3 split requests
    # 25/75), whatever the proxies each holds, so a dynamic sessions gateway of
    # one row and a pool of fifty slots split evenly at equal weights.
    weight: int = Field(default=DEFAULT_ROUTING_WEIGHT, ge=1, le=MAX_ROUTING_WEIGHT)

    @model_validator(mode='after')
    def validate_mutual_exclusivity(self) -> 'RoutingConfig':
        """Ensure whitelist and blacklist are mutually exclusive."""
        # Normalize: strip whitespace, lowercase, remove empty strings
        self.domain_whitelist = [d.strip().lower() for d in self.domain_whitelist if d.strip()]
        self.domain_blacklist = [d.strip().lower() for d in self.domain_blacklist if d.strip()]
        if self.domain_whitelist and self.domain_blacklist:
            raise ValueError('domain_whitelist and domain_blacklist are mutually exclusive; only one can be set')
        return self


class RateLimitConfig(BaseModel):
    """Per-proxy rate limiting: a proxy that handles ``max_requests`` within a sliding ``window_seconds`` is quarantined.

    The quarantine lasts a random time between ``quarantine_seconds_min`` and ``quarantine_seconds_max``.
    """
    max_requests: int = Field(ge=1)
    window_seconds: int = Field(ge=1, le=86400)
    quarantine_seconds_min: int = Field(ge=1, le=86400)
    quarantine_seconds_max: int = Field(ge=1, le=86400)
    # Under sticky session routing, a quarantined proxy is not replaced for its
    # session: the request is refused (429), so a client wanting another IP
    # starts a new session.
    sticky_quarantine: bool = False

    @model_validator(mode='after')
    def validate_quarantine_range(self) -> 'RateLimitConfig':
        """Ensure quarantine min <= max."""
        if self.quarantine_seconds_min > self.quarantine_seconds_max:
            raise ValueError('quarantine_seconds_min must be <= quarantine_seconds_max')
        return self


class TrafficPeriod(StrEnum):
    """How often a connector's traffic usage starts over."""
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class TrafficLimitAction(StrEnum):
    """What happens when a connector's traffic reaches its limit."""
    # Keeps routing, and only flags the connector.
    ALERT = "alert"
    # Hands the connector no new requests; running transfers finish.
    BLOCK = "block"
    # Also cuts the transfers still running.
    INTERRUPT = "interrupt"


# Status sent to a client when every connector that could serve its request is
# over its traffic limit. 509 Bandwidth Limit Exceeded cannot be mistaken for
# the rate limiter's 429 or for a misconfiguration; 429 is offered for clients
# hard-wired to it.
DEFAULT_TRAFFIC_LIMIT_STATUS = 509
TRAFFIC_LIMIT_STATUSES: tuple[int, ...] = (429, 509)
# Vendors bill decimal gigabytes. The UI enters limits and prices in these.
BYTES_PER_GB = 1_000_000_000


class TrafficConfig(BaseModel):
    """Traffic accounting, limit and pricing for one connector: the bytes its proxies carry both ways, summed per period."""
    # None meters and prices only; past ``warn_percent`` of it the connector is
    # flagged, and at it ``action`` applies.
    limit_bytes: int | None = Field(default=None, ge=1)
    # The vendor's billing cycle: a calendar day, a week from ``reset_day``
    # (1 = Monday), or a month from that day of the month, at most the 28th so
    # every month has it.
    period: TrafficPeriod = TrafficPeriod.MONTH
    reset_day: int = Field(default=1, ge=1, le=28)
    action: TrafficLimitAction = TrafficLimitAction.ALERT
    warn_percent: int = Field(default=80, ge=1, le=100)
    # What clients get when no connector is left to serve them.
    limit_status: int = DEFAULT_TRAFFIC_LIMIT_STATUS
    # Turns usage into spend at display time, never stored with the metrics:
    # past periods are priced at the current rate.
    price_per_gb: float | None = Field(default=None, ge=0)
    currency: str = "USD"

    @field_validator("limit_status")
    @classmethod
    def _validate_status(cls, value: int) -> int:
        if value not in TRAFFIC_LIMIT_STATUSES:
            allowed = ", ".join(str(s) for s in TRAFFIC_LIMIT_STATUSES)
            raise ValueError(f"limit_status must be one of {allowed}")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _validate_currency(cls, value: Any) -> str:
        code = str(value or "").strip().upper()
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            raise ValueError("currency must be a three-letter ISO 4217 code")
        return code

    @model_validator(mode="after")
    def _validate_reset_day(self) -> "TrafficConfig":
        if self.period == TrafficPeriod.WEEK and self.reset_day > 7:
            raise ValueError("reset_day must be 1 (Monday) to 7 (Sunday) for a weekly period")
        return self

    @property
    def limit_enabled(self) -> bool:
        """Whether the connector has a limit to enforce, not just metering."""
        return self.limit_bytes is not None

    @property
    def warn_bytes(self) -> int | None:
        """Usage at which the connector is flagged as approaching its limit."""
        if self.limit_bytes is None:
            return None
        return self.limit_bytes * self.warn_percent // 100

    def cost_of(self, total_bytes: int) -> float | None:
        """Spend for ``total_bytes`` at the configured price, or None when unpriced."""
        if self.price_per_gb is None:
            return None
        return round(total_bytes / BYTES_PER_GB * self.price_per_gb, 4)

    def blocks_alike(self, other: "TrafficConfig") -> bool:
        """Whether ``other`` blocks a connector exactly when this config does: the same limit, action, period and reset day.

        The price, the currency, the warning line and the status clients get while blocked decide no block.
        """
        mine = (self.limit_bytes, self.action, self.period, self.reset_day)
        return mine == (other.limit_bytes, other.action, other.period, other.reset_day)

    def period_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        """Start (inclusive) and end (exclusive) of the period containing ``now``, UTC."""
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.period == TrafficPeriod.DAY:
            return day, day + timedelta(days=1)
        if self.period == TrafficPeriod.WEEK:
            offset = (day.isoweekday() - self.reset_day) % 7
            start = day - timedelta(days=offset)
            return start, start + timedelta(days=7)
        reset_day = self.reset_day
        if day.day >= reset_day:
            start = day.replace(day=reset_day)
        else:
            last_of_previous = day.replace(day=1) - timedelta(days=1)
            start = last_of_previous.replace(day=reset_day)
        following = (start.replace(day=1) + timedelta(days=32)).replace(day=reset_day)
        return start, following

    def usage_window(self, now: datetime, reset_at: datetime | None) -> tuple[datetime, datetime]:
        """The span usage is summed over: the period containing ``now``, cut at a manual reset inside it."""
        start, end = self.period_bounds(now)
        if reset_at is not None and start < reset_at < end:
            start = reset_at
        return start, end


class TrafficUsage(BaseModel):
    """A connector's traffic in the current period, against its limit and price."""
    period: TrafficPeriod
    period_start: datetime
    period_end: datetime
    bytes_sent: int
    bytes_received: int
    total_bytes: int
    limit_bytes: int | None = None
    # total_bytes as a share of the limit, 0-100 and beyond; None without a limit.
    percent: float | None = None
    action: TrafficLimitAction
    status: Literal["ok", "warning", "exceeded"]
    # True while the connector takes no new requests because of its limit: the
    # block the traffic job stored, which gateways enforce.
    blocked: bool
    # True from a traffic reset or a limit edit until a worker stores the
    # traffic decision it asked for; blocked is the stored block meanwhile.
    pending: bool
    price_per_gb: float | None = None
    currency: str
    cost: float | None = None
    # Usage counts from here when it is inside the period (a manual reset).
    reset_at: datetime | None = None


def validate_routing_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate routing config and return validated dict.

    Excludes empty lists from the output to keep the stored config clean.
    """
    validated = RoutingConfig(**config)
    result = validated.model_dump(exclude_none=True)
    if result.get("weight") == DEFAULT_ROUTING_WEIGHT:
        # The default weight is implied; store only explicit choices.
        del result["weight"]
    # Remove empty lists to keep config clean
    return {k: v for k, v in result.items() if v}


def validate_rate_limit_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate rate limit config and return validated dict.

    Returns an empty dict if the input is empty (rate limiting disabled).
    """
    if not config:
        return {}
    validated = RateLimitConfig(**config)
    return validated.model_dump()


def validate_traffic_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate traffic config and return the dict to store.

    Only explicit choices are kept: a key at its default value is dropped, so
    a connector left at the defaults stores ``{}`` like every other config.
    """
    if not config:
        return {}
    validated = TrafficConfig(**config)
    defaults = TrafficConfig().model_dump(mode="json")
    return {
        key: value
        for key, value in validated.model_dump(mode="json").items()
        if value is not None and value != defaults.get(key)
    }


_CODE_CONNECTOR_MODELS: dict[str, type[BaseModel]] = {
    CredentialType.STATIC_PROXY_PROVIDER.value: StaticProxyProviderConnectorConfig,
    CredentialType.AWS.value: AWSConnectorConfig,
    CredentialType.GCP.value: GCPConnectorConfig,
    CredentialType.AZURE.value: AzureConnectorConfig,
}


def validate_connector_config(credential_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Validate a code-implemented connector type's config and return the normalised dict.

    Descriptor-driven types are validated by the provider registry, not here.
    """
    model = _CODE_CONNECTOR_MODELS.get(credential_type)
    if model is None:
        raise ValueError(f"Unknown credential type: {credential_type}")
    result = model(**config).model_dump(exclude_none=True)
    if not result.get("countries"):
        # An empty list means "no country declared"; keep stored configs clean.
        result.pop("countries", None)
    return result


def get_cloud_config(
    credential_type: str, config: dict[str, Any]
) -> CloudConnectorConfig | None:
    """The typed cloud config a raw ``config`` holds, of the credential type's cloud; None for a type that is no cloud."""
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
    credential_type: str
    project_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    routing_config: dict[str, Any] = Field(default_factory=dict)
    rate_limit_config: dict[str, Any] = Field(default_factory=dict)
    traffic_config: dict[str, Any] = Field(default_factory=dict)
    # Usage in the current period counts from here when set inside it (manual reset).
    traffic_reset_at: datetime | None = None
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
    def in_error_backoff(self) -> bool:
        """Whether a cloud connector backs off after cloud errors: 2^errors minutes from the last, up to the ceiling."""
        if self.last_error_at is None or self.consecutive_errors == 0:
            return False
        minutes = min(2 ** self.consecutive_errors, MAX_ERROR_BACKOFF_MINUTES)
        return utc_now() < self.last_error_at + timedelta(minutes=minutes)


    @property
    def parsed_routing_config(self) -> RoutingConfig:
        """Get the typed routing config for this connector."""
        if not self.routing_config:
            return RoutingConfig()
        return RoutingConfig(**self.routing_config)

    @property
    def weight(self) -> int:
        """Relative share of project traffic against the project's other connectors (default 1).

        Parsed by ``RoutingConfig`` like every other routing key; a stored
        value the model rejects (hand-edited JSON) routes at the default.
        """
        if not self.routing_config:
            return DEFAULT_ROUTING_WEIGHT
        try:
            return RoutingConfig(**self.routing_config).weight
        except ValidationError:
            return DEFAULT_ROUTING_WEIGHT

    @property
    def countries(self) -> list[str]:
        """Countries the proxies exit from, for ``-cc-`` routing: ``config.countries`` or the provider's ``country_code``.

        Empty when it declares none: a descriptor pool whose credentials carry a country then serves any country on
        demand, and other connectors are matched per proxy on the discovered country.
        """
        if not self.config:
            return []
        for key in ("countries", "country_code"):
            value = self.config.get(key)
            if value is None or value == "" or value == []:
                continue
            try:
                return normalize_country_list(value)
            except ValueError:
                return []
        return []

    @property
    def parsed_rate_limit_config(self) -> RateLimitConfig | None:
        """Get the typed rate limit config, or None if not configured."""
        if not self.rate_limit_config:
            return None
        return RateLimitConfig(**self.rate_limit_config)

    @property
    def parsed_traffic_config(self) -> TrafficConfig:
        """The typed traffic config; every connector has one, at the defaults when unset.

        A stored value the model rejects (hand-edited JSON) reads as the defaults.
        """
        if not self.traffic_config:
            return TrafficConfig()
        try:
            return TrafficConfig(**self.traffic_config)
        except ValidationError:
            return TrafficConfig()


class ConnectorCreate(BaseModel):
    """Schema for creating a new connector."""
    name: str
    credential_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    routing_config: dict[str, Any] = Field(default_factory=dict)
    rate_limit_config: dict[str, Any] = Field(default_factory=dict)
    traffic_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # Note: config validation happens in the route after fetching credential type


class ConnectorUpdate(BaseModel):
    """Schema for updating a connector."""
    name: str | None = None
    credential_id: str | None = None
    config: dict[str, Any] | None = None
    routing_config: dict[str, Any] | None = None
    rate_limit_config: dict[str, Any] | None = None
    traffic_config: dict[str, Any] | None = None
    enabled: bool | None = None


class ProxyTarget(BaseModel):
    """How many proxies a connector is meant to hold, and how that number comes about."""
    # None when the connector has no target of its own (static connectors,
    # list-mode providers mirroring a vendor list).
    total: int | None = None
    # A provider connector's count per country group: the total is this times
    # the groups that exist (``countries``: listed on the connector, or made on
    # demand for an "all countries" pool, those also in ``on_demand``).
    per_country: int | None = None
    countries: list[str] = Field(default_factory=list)
    on_demand: list[str] = Field(default_factory=list)
    dynamic: bool = False
    exit_sample_percent: int | None = None


class ConnectorResponse(BaseModel):
    """Schema for connector API responses."""
    id: str
    name: str
    credential_id: str
    credential_name: str | None = None
    credential_type: str | None = None
    project_id: str
    config: dict[str, Any]
    routing_config: dict[str, Any] = Field(default_factory=dict)
    rate_limit_config: dict[str, Any] = Field(default_factory=dict)
    traffic_config: dict[str, Any] = Field(default_factory=dict)
    traffic_reset_at: datetime | None = None
    # Traffic in the current period against the limit and price; None only
    # when the serving instance has no usage yet for the connector.
    traffic_usage: TrafficUsage | None = None
    enabled: bool
    proxy_count: int
    # Intended pool size and how it is derived (None when there is no target)
    target: ProxyTarget | None = None
    # Cloud provider error tracking
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_errors: int = 0
    created_at: datetime
    updated_at: datetime


# --- Options Response for Frontend ---

class ConnectorOptionsResponse(BaseModel):
    """Response containing available options for connector configuration, each a rich object for the frontend to show."""
    aws_regions: list[RegionOption] = Field(default_factory=lambda: AWS_REGIONS)
    aws_instance_types: list[InstanceTypeOption] = Field(default_factory=lambda: AWS_INSTANCE_TYPES)
    gcp_zones: list[RegionOption] = Field(default_factory=lambda: GCP_ZONES)
    gcp_machine_types: list[InstanceTypeOption] = Field(default_factory=lambda: GCP_MACHINE_TYPES)
    azure_locations: list[RegionOption] = Field(default_factory=lambda: AZURE_LOCATIONS)
    azure_vm_sizes: list[InstanceTypeOption] = Field(default_factory=lambda: AZURE_VM_SIZES)
    countries: list[CountryOption] = Field(default_factory=lambda: COUNTRIES)

