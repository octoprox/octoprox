# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Registry entries for the providers implemented in code (cloud + static).

These are not descriptor-driven, but they still publish field schemas so the
UI can render credential forms uniformly, and they route validation to the
existing Pydantic config models.
"""

from __future__ import annotations

from typing import Any

from api.models.connector import validate_connector_config
from api.models.credential import CredentialType, validate_credential_config
from api.providers.sdk.descriptor import FieldSpec


def _credential_validator(type_id: str) -> Any:
    def validate(config: dict[str, Any]) -> dict[str, Any]:
        return validate_credential_config(type_id, config)

    return validate


def _connector_validator(type_id: str) -> Any:
    def validate(config: dict[str, Any], _credential_config: dict[str, Any] | None = None) -> dict[str, Any]:
        return validate_connector_config(type_id, config)

    return validate


STATIC_CREDENTIAL_FIELDS = [
    FieldSpec(key="username", label="Default username", help="Used for proxies without their own credentials."),
    FieldSpec(key="password", label="Default password", type="password", secret=True),
]

AWS_CREDENTIAL_FIELDS = [
    FieldSpec(key="access_key", label="Access key", required=True),
    FieldSpec(key="secret_key", label="Secret key", type="password", secret=True, required=True),
]

GCP_CREDENTIAL_FIELDS = [
    FieldSpec(
        key="service_account_json",
        label="Service account JSON",
        type="textarea",
        secret=True,
        required=True,
        placeholder="Paste service account JSON here",
    ),
    FieldSpec(key="project_id", label="Project ID", required=True),
]

AZURE_CREDENTIAL_FIELDS = [
    FieldSpec(key="subscription_id", label="Subscription ID", required=True),
    FieldSpec(key="tenant_id", label="Tenant ID", required=True),
    FieldSpec(key="client_id", label="Client ID", required=True),
    FieldSpec(key="client_secret", label="Client secret", type="password", secret=True, required=True),
    FieldSpec(key="key_vault_name", label="Key vault name"),
]

CLOUD_SCALING_FIELDS = [
    FieldSpec(key="min_proxies", label="Min proxies", type="number", default=1, min=0, group="scaling"),
    FieldSpec(key="max_proxies", label="Max proxies", type="number", default=10, min=1, group="scaling"),
    FieldSpec(
        key="min_rotation_period_minutes", label="Min rotation (min)", type="number", default=60, group="scaling"
    ),
    FieldSpec(
        key="max_rotation_period_minutes", label="Max rotation (min)", type="number", default=1440, group="scaling"
    ),
]

AWS_CONNECTOR_FIELDS = [
    FieldSpec(key="instance_name", label="Instance name", required=True),
    FieldSpec(key="region", label="Region", required=True),
    FieldSpec(key="instance_type", label="Instance type", required=True),
    FieldSpec(key="key_pair_name", label="Key pair", required=True),
    FieldSpec(key="security_group", label="Security group", required=True),
    *CLOUD_SCALING_FIELDS,
]

GCP_CONNECTOR_FIELDS = [
    FieldSpec(key="project_id", label="Project ID", required=True),
    FieldSpec(key="instance_name", label="Instance name", required=True),
    FieldSpec(key="zone", label="Zone", required=True, default="us-central1-a"),
    FieldSpec(key="machine_type", label="Machine type", required=True, default="e2-micro"),
    FieldSpec(key="network", label="Network", default="default"),
    *CLOUD_SCALING_FIELDS,
]

AZURE_CONNECTOR_FIELDS = [
    FieldSpec(key="subscription_id", label="Subscription ID", required=True),
    FieldSpec(key="resource_group", label="Resource group", required=True),
    FieldSpec(key="instance_name", label="Instance name", required=True),
    FieldSpec(key="location", label="Location", required=True, default="eastus"),
    FieldSpec(key="vm_size", label="VM size", required=True, default="Standard_B2ls_v2"),
    FieldSpec(key="vnet_name", label="Virtual network"),
    FieldSpec(key="subnet_name", label="Subnet"),
    FieldSpec(key="ssh_public_key", label="SSH public key", type="textarea", required=True),
    *CLOUD_SCALING_FIELDS,
]


def code_provider_definitions() -> list[dict[str, Any]]:
    """Static metadata for the code-implemented provider types."""
    return [
        {
            "id": CredentialType.STATIC_PROXY_PROVIDER.value,
            "name": "Static Proxy Provider",
            "description": "Manually managed proxy servers",
            "credential_fields": STATIC_CREDENTIAL_FIELDS,
            "connector_fields": [],
            "cloud": False,
        },
        {
            "id": CredentialType.AWS.value,
            "name": "Amazon Web Services",
            "description": "EC2 instances as proxy servers",
            "credential_fields": AWS_CREDENTIAL_FIELDS,
            "connector_fields": AWS_CONNECTOR_FIELDS,
            "cloud": True,
        },
        {
            "id": CredentialType.GCP.value,
            "name": "Google Cloud Platform",
            "description": "Compute Engine VMs as proxy servers",
            "credential_fields": GCP_CREDENTIAL_FIELDS,
            "connector_fields": GCP_CONNECTOR_FIELDS,
            "cloud": True,
        },
        {
            "id": CredentialType.AZURE.value,
            "name": "Microsoft Azure",
            "description": "Virtual Machines as proxy servers",
            "credential_fields": AZURE_CREDENTIAL_FIELDS,
            "connector_fields": AZURE_CONNECTOR_FIELDS,
            "cloud": True,
        },
    ]


def code_validators(type_id: str) -> tuple[Any, Any]:
    return _credential_validator(type_id), _connector_validator(type_id)
