# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential model definitions for provider authentication."""

import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.core import utc_now


class CredentialType(str, Enum):
    """Types of credentials for different providers."""
    STATIC_PROXY_PROVIDER = "static_proxy_provider"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


# --- Typed Config Models for Validation ---

class StaticProxyProviderConfig(BaseModel):
    """Configuration for Static Proxy Provider credentials."""
    username: str | None = None
    password: str | None = None


class AWSCredentialConfig(BaseModel):
    """Configuration for AWS credentials."""
    access_key: str
    secret_key: str

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AWSCredentialConfig':
        """Ensure required fields are not empty."""
        if not self.access_key or not self.access_key.strip():
            raise ValueError('access_key is required and cannot be empty')
        if not self.secret_key or not self.secret_key.strip():
            raise ValueError('secret_key is required and cannot be empty')
        return self


class GCPCredentialConfig(BaseModel):
    """Configuration for GCP credentials."""
    service_account_json: str
    project_id: str

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'GCPCredentialConfig':
        """Ensure required fields are not empty and service_account_json is valid JSON."""
        if not self.service_account_json or not self.service_account_json.strip():
            raise ValueError('service_account_json is required and cannot be empty')
        if not self.project_id or not self.project_id.strip():
            raise ValueError('project_id is required and cannot be empty')

        # Validate that service_account_json is valid JSON
        try:
            parsed = json.loads(self.service_account_json)
            if not isinstance(parsed, dict):
                raise ValueError('service_account_json must be a valid JSON object')
        except json.JSONDecodeError as e:
            raise ValueError(f'service_account_json contains invalid JSON: {e}') from None

        return self


class AzureCredentialConfig(BaseModel):
    """Configuration for Azure credentials."""
    subscription_id: str
    tenant_id: str
    client_id: str
    client_secret: str
    key_vault_name: str | None = None

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AzureCredentialConfig':
        """Ensure required fields are not empty."""
        if not self.subscription_id or not self.subscription_id.strip():
            raise ValueError('subscription_id is required and cannot be empty')
        if not self.tenant_id or not self.tenant_id.strip():
            raise ValueError('tenant_id is required and cannot be empty')
        if not self.client_id or not self.client_id.strip():
            raise ValueError('client_id is required and cannot be empty')
        if not self.client_secret or not self.client_secret.strip():
            raise ValueError('client_secret is required and cannot be empty')
        return self


def validate_credential_config(credential_type: CredentialType, config: dict[str, Any]) -> dict[str, Any]:
    """Validate credential config based on type and return validated config."""
    validated: StaticProxyProviderConfig | AWSCredentialConfig | GCPCredentialConfig | AzureCredentialConfig
    if credential_type == CredentialType.STATIC_PROXY_PROVIDER:
        validated = StaticProxyProviderConfig(**config)
    elif credential_type == CredentialType.AWS:
        validated = AWSCredentialConfig(**config)
    elif credential_type == CredentialType.GCP:
        validated = GCPCredentialConfig(**config)
    elif credential_type == CredentialType.AZURE:
        validated = AzureCredentialConfig(**config)
    else:
        raise ValueError(f"Unknown credential type: {credential_type}")
    return validated.model_dump(exclude_none=True)


class Credential(BaseModel):
    """Represents authentication credentials for a provider."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    type: CredentialType
    project_id: str
    config: dict[str, Any] = Field(default_factory=dict)

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CredentialCreate(BaseModel):
    """Schema for creating a new credential."""
    name: str
    type: CredentialType
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_config(self) -> 'CredentialCreate':
        """Validate config based on credential type."""
        self.config = validate_credential_config(self.type, self.config)
        return self


class CredentialUpdate(BaseModel):
    """Schema for updating a credential."""
    name: str | None = None
    config: dict[str, Any] | None = None
    # Note: type cannot be changed after creation


class CredentialResponse(BaseModel):
    """Schema for credential API responses."""
    id: str
    name: str
    type: str
    project_id: str
    # Note: config is intentionally excluded from response for security
    # Only include non-sensitive metadata about the config
    has_username: bool = False
    has_password: bool = False
    created_at: datetime
    updated_at: datetime


class CredentialDetailResponse(BaseModel):
    """Schema for credential detail API responses (includes config)."""
    id: str
    name: str
    type: str
    project_id: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

