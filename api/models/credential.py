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

def _blank_to_none(value: str | None) -> str | None:
    """Strip an optional field; empty or whitespace-only becomes None."""
    return (value or "").strip() or None


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

        self.role_arn = _blank_to_none(self.role_arn)
        if self.role_arn is not None and not AWS_ROLE_ARN_PATTERN.match(self.role_arn):
            raise ValueError(
                'role_arn must be an IAM role ARN such as arn:aws:iam::123456789012:role/octoprox'
            )

        self.external_id = _blank_to_none(self.external_id)
        if self.external_id is not None:
            if self.role_arn is None:
                raise ValueError('external_id requires role_arn')
            if not AWS_EXTERNAL_ID_PATTERN.match(self.external_id):
                raise ValueError('external_id contains characters AWS does not accept')
        return self


class GCPAuthMode(str, Enum):
    """How a GCP credential obtains its base identity.

    ``SERVICE_ACCOUNT_KEY`` stores a service account JSON key.
    ``HOST_IDENTITY`` stores nothing and uses Application Default
    Credentials: the service account attached to the GCE VM or GKE workload,
    Workload Identity, or GOOGLE_APPLICATION_CREDENTIALS. Either mode may
    then impersonate another service account, which is how a customer grants
    access to their project without issuing a key.
    """
    SERVICE_ACCOUNT_KEY = "service_account_key"
    HOST_IDENTITY = "host_identity"


# name@project.iam.gserviceaccount.com, or the default
# 123456789-compute@developer.gserviceaccount.com
GCP_SERVICE_ACCOUNT_EMAIL_PATTERN = re.compile(r"^[a-z0-9-]+@[a-z0-9.-]+\.gserviceaccount\.com$")


class GCPCredentialConfig(BaseModel):
    """Configuration for GCP credentials.

    ``service_account_json`` and ``project_id`` are required in
    ``service_account_key`` mode and dropped in ``host_identity`` mode.
    ``impersonate_service_account`` is optional in both modes.
    """
    model_config = ConfigDict(use_enum_values=True, validate_default=True)

    auth_mode: GCPAuthMode = GCPAuthMode.SERVICE_ACCOUNT_KEY
    service_account_json: str | None = None
    project_id: str | None = None
    impersonate_service_account: str | None = None

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'GCPCredentialConfig':
        """Check the fields the selected auth mode needs and normalise the rest."""
        if self.auth_mode == GCPAuthMode.SERVICE_ACCOUNT_KEY:
            if not self.service_account_json or not self.service_account_json.strip():
                raise ValueError('service_account_json is required and cannot be empty')
            if not self.project_id or not self.project_id.strip():
                raise ValueError('project_id is required and cannot be empty')
            self.project_id = self.project_id.strip()

            # Validate that service_account_json is valid JSON
            try:
                parsed = json.loads(self.service_account_json)
                if not isinstance(parsed, dict):
                    raise ValueError('service_account_json must be a valid JSON object')
            except json.JSONDecodeError as e:
                raise ValueError(f'service_account_json contains invalid JSON: {e}') from None
        else:
            self.service_account_json = None
            self.project_id = None

        self.impersonate_service_account = _blank_to_none(self.impersonate_service_account)
        if (
            self.impersonate_service_account is not None
            and not GCP_SERVICE_ACCOUNT_EMAIL_PATTERN.match(self.impersonate_service_account)
        ):
            raise ValueError(
                'impersonate_service_account must be a service account email such as '
                'octoprox@my-project.iam.gserviceaccount.com'
            )
        return self


class AzureAuthMode(str, Enum):
    """How an Azure credential obtains its base identity.

    ``CLIENT_SECRET`` stores a service principal's client secret.
    ``HOST_IDENTITY`` stores nothing and uses the managed identity or
    workload identity of the host Octoprox runs on. In host mode the
    credential may also name an app registration in another tenant that
    trusts the host identity through a federated credential, which is how a
    customer grants access without issuing a secret.
    """
    CLIENT_SECRET = "client_secret"
    HOST_IDENTITY = "host_identity"


AZURE_GUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
# A tenant GUID or a verified domain such as customer.onmicrosoft.com; the
# character set is the one azure-identity's validate_tenant_id accepts.
AZURE_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


class AzureCredentialConfig(BaseModel):
    """Configuration for Azure credentials.

    The service principal fields are required in ``client_secret`` mode and
    dropped in ``host_identity`` mode. ``managed_identity_client_id`` and the
    ``target_*`` federation fields only apply in ``host_identity`` mode.
    """
    model_config = ConfigDict(use_enum_values=True, validate_default=True)

    auth_mode: AzureAuthMode = AzureAuthMode.CLIENT_SECRET
    subscription_id: str | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    key_vault_name: str | None = None
    managed_identity_client_id: str | None = None
    target_tenant_id: str | None = None
    target_client_id: str | None = None

    @model_validator(mode='after')
    def validate_required_fields(self) -> 'AzureCredentialConfig':
        """Check the fields the selected auth mode needs and normalise the rest."""
        self.managed_identity_client_id = _blank_to_none(self.managed_identity_client_id)
        self.target_tenant_id = _blank_to_none(self.target_tenant_id)
        self.target_client_id = _blank_to_none(self.target_client_id)

        if self.auth_mode == AzureAuthMode.CLIENT_SECRET:
            for key in ("subscription_id", "tenant_id", "client_id", "client_secret"):
                value = _blank_to_none(getattr(self, key))
                if value is None:
                    raise ValueError(f'{key} is required and cannot be empty')
                setattr(self, key, value)
            if self.managed_identity_client_id or self.target_tenant_id or self.target_client_id:
                raise ValueError(
                    'managed_identity_client_id, target_tenant_id and target_client_id '
                    'only apply in host_identity mode'
                )
            return self

        self.subscription_id = None
        self.tenant_id = None
        self.client_id = None
        self.client_secret = None
        self.key_vault_name = None

        if self.managed_identity_client_id is not None and not AZURE_GUID_PATTERN.match(
            self.managed_identity_client_id
        ):
            raise ValueError('managed_identity_client_id must be a client ID GUID')
        if (self.target_tenant_id is None) != (self.target_client_id is None):
            raise ValueError('target_tenant_id and target_client_id must be set together')
        if self.target_tenant_id is not None and not AZURE_TENANT_ID_PATTERN.match(self.target_tenant_id):
            raise ValueError('target_tenant_id must be a tenant ID GUID or a tenant domain')
        if self.target_client_id is not None and not AZURE_GUID_PATTERN.match(self.target_client_id):
            raise ValueError('target_client_id must be an application (client) ID GUID')
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
