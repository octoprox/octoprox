# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential model definitions for provider authentication.

``Credential.type`` is an open string: it names a provider type in the
provider registry. The :class:`CredentialType` enum only enumerates the
code-implemented types (static lists and the three clouds); every other type
comes from a provider descriptor and is validated by the registry.
"""

import json
import re
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.core import utc_now


class CredentialType(str, Enum):
    """Code-implemented credential types (descriptor providers are open strings)."""
    STATIC_PROXY_PROVIDER = "static_proxy_provider"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


# --- Typed Config Models for Validation ---

class StaticProxyProviderConfig(BaseModel):
    """Configuration for Static Proxy Provider credentials."""
    username: str | None = None
    password: str | None = None


class AWSAuthMode(str, Enum):
    """How an AWS credential obtains its base identity.

    ``ACCESS_KEYS`` stores an IAM user's key pair. ``HOST_ROLE`` stores nothing
    and uses the identity of the environment Octoprox runs in (EC2 instance
    profile, ECS task role, EKS pod identity, or the AWS_* environment
    variables). Either mode may then assume a role (``role_arn``), which is
    how customers grant access to an account without handing out long-lived
    keys.
    """
    ACCESS_KEYS = "access_keys"
    HOST_ROLE = "host_role"


# arn:aws:iam::123456789012:role/path/Name (also aws-cn, aws-us-gov partitions);
# the path is zero or more non-empty segments, so "role//x" and "role/x/" are rejected
AWS_ROLE_ARN_PATTERN = re.compile(r"^arn:aws(-[a-z]+)*:iam::\d{12}:role/(?:[\w+=,.@-]+/)*[\w+=,.@-]+$")
# ExternalId as accepted by sts:AssumeRole
AWS_EXTERNAL_ID_PATTERN = re.compile(r"^[\w+=,.@:/-]{2,1224}$")


class AWSCredentialConfig(BaseModel):
    """Configuration for AWS credentials.

    ``access_key``/``secret_key`` are required in ``access_keys`` mode and
    dropped in ``host_role`` mode so a stale key pair never lingers in the
    stored config. ``role_arn`` is optional in both modes; ``external_id``
    only makes sense alongside it.
    """
    # Store the plain string so the config dict stays JSON-native. The default
    # is only converted when validate_default is on; without it an omitted
    # auth_mode would dump as the enum member.
    model_config = ConfigDict(use_enum_values=True, validate_default=True)

    auth_mode: AWSAuthMode = AWSAuthMode.ACCESS_KEYS
    access_key: str | None = None
    secret_key: str | None = None
    role_arn: str | None = None
    external_id: str | None = None

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AWSCredentialConfig':
        """Check the fields the selected auth mode needs and normalise the rest."""
        if self.auth_mode == AWSAuthMode.ACCESS_KEYS:
            if not self.access_key or not self.access_key.strip():
                raise ValueError('access_key is required and cannot be empty')
            if not self.secret_key or not self.secret_key.strip():
                raise ValueError('secret_key is required and cannot be empty')
            self.access_key = self.access_key.strip()
            self.secret_key = self.secret_key.strip()
        else:
            self.access_key = None
            self.secret_key = None

        self.role_arn = (self.role_arn or "").strip() or None
        if self.role_arn is not None and not AWS_ROLE_ARN_PATTERN.match(self.role_arn):
            raise ValueError(
                'role_arn must be an IAM role ARN such as arn:aws:iam::123456789012:role/octoprox'
            )

        self.external_id = (self.external_id or "").strip() or None
        if self.external_id is not None:
            if self.role_arn is None:
                raise ValueError('external_id requires role_arn')
            if not AWS_EXTERNAL_ID_PATTERN.match(self.external_id):
                raise ValueError('external_id contains characters AWS does not accept')
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


_CODE_CREDENTIAL_MODELS: dict[str, type[BaseModel]] = {
    CredentialType.STATIC_PROXY_PROVIDER.value: StaticProxyProviderConfig,
    CredentialType.AWS.value: AWSCredentialConfig,
    CredentialType.GCP.value: GCPCredentialConfig,
    CredentialType.AZURE.value: AzureCredentialConfig,
}


def is_code_credential_type(credential_type: str) -> bool:
    """True for the types validated by the Pydantic models in this module."""
    return str(credential_type) in _CODE_CREDENTIAL_MODELS


def validate_credential_config(credential_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """Validate a code-implemented credential type's config and return the normalised dict.

    Descriptor-driven types are validated by the provider registry, not here.
    """
    model = _CODE_CREDENTIAL_MODELS.get(str(credential_type))
    if model is None:
        raise ValueError(f"Unknown credential type: {credential_type}")
    return model(**config).model_dump(exclude_none=True)


class Credential(BaseModel):
    """Represents authentication credentials for a provider."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    type: str
    project_id: str
    config: dict[str, Any] = Field(default_factory=dict)

    # Metadata
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode='after')
    def _normalise_type(self) -> 'Credential':
        self.type = str(self.type.value) if isinstance(self.type, Enum) else str(self.type)
        return self


class CredentialCreate(BaseModel):
    """Schema for creating a new credential.

    Config validation happens in the route via the provider registry, which
    knows every registered type (code and descriptor).
    """
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


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
