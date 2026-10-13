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
from api.models.credential import (
    AWSAuthMode,
    AzureAuthMode,
    CredentialType,
    GCPAuthMode,
    validate_credential_config,
)
from api.providers.sdk.descriptor import Condition, FieldSpec, OptionSpec


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

_AWS_USES_ACCESS_KEYS = Condition(field="credential.auth_mode", equals=AWSAuthMode.ACCESS_KEYS.value)

AWS_CREDENTIAL_FIELDS = [
    FieldSpec(
        key="auth_mode",
        label="Authentication",
        type="select",
        required=True,
        default=AWSAuthMode.ACCESS_KEYS.value,
        options=[
            OptionSpec(
                value=AWSAuthMode.ACCESS_KEYS.value,
                label="Access keys",
                description="An IAM user's access key and secret key, stored encrypted.",
            ),
            OptionSpec(
                value=AWSAuthMode.HOST_ROLE.value,
                label="Host IAM role",
                description="The identity Octoprox runs with (instance profile, task role, pod identity). Nothing is stored.",
            ),
        ],
        help="Host IAM role needs no stored secrets and is the recommended choice when Octoprox runs on AWS.",
        details=(
            "With Host IAM role, boto3's default credential chain is used: the EC2 instance "
            "profile, ECS task role, EKS pod identity or IRSA, or AWS_ACCESS_KEY_ID style "
            "environment variables on the Octoprox host. See the AWS setup guide."
        ),
    ),
    FieldSpec(key="access_key", label="Access key", required=True, show_when=_AWS_USES_ACCESS_KEYS),
    FieldSpec(
        key="secret_key",
        label="Secret key",
        type="password",
        secret=True,
        required=True,
        show_when=_AWS_USES_ACCESS_KEYS,
    ),
    FieldSpec(
        key="role_arn",
        label="Role to assume",
        placeholder="arn:aws:iam::123456789012:role/octoprox",
        help="Optional. Octoprox assumes this role with sts:AssumeRole and provisions EC2 with its temporary credentials.",
        details=(
            "Use this to run in a different account than the one the credentials above belong to, "
            "or to keep the base identity free of EC2 permissions. The role's trust policy must "
            "allow the base identity to assume it."
        ),
    ),
    FieldSpec(
        key="external_id",
        label="External ID",
        show_when=Condition(field="credential.role_arn"),
        help="Optional. Sent as ExternalId when assuming the role; set it if the role's trust policy requires one.",
    ),
]

_GCP_USES_KEY = Condition(field="credential.auth_mode", equals=GCPAuthMode.SERVICE_ACCOUNT_KEY.value)

GCP_CREDENTIAL_FIELDS = [
    FieldSpec(
        key="auth_mode",
        label="Authentication",
        type="select",
        required=True,
        default=GCPAuthMode.SERVICE_ACCOUNT_KEY.value,
        options=[
            OptionSpec(
                value=GCPAuthMode.SERVICE_ACCOUNT_KEY.value,
                label="Service account key",
                description="A service account JSON key, stored encrypted.",
            ),
            OptionSpec(
                value=GCPAuthMode.HOST_IDENTITY.value,
                label="Host identity",
                description="The service account Octoprox runs as (attached service account, Workload Identity). Nothing is stored.",
            ),
        ],
        help="Host identity needs no stored secrets and is the recommended choice when Octoprox runs on GCP.",
        details=(
            "With Host identity, Application Default Credentials are used: the service account "
            "attached to the GCE VM or GKE workload, Workload Identity, or the file named by "
            "GOOGLE_APPLICATION_CREDENTIALS on the Octoprox host. See the GCP setup guide."
        ),
    ),
    FieldSpec(
        key="service_account_json",
        label="Service account JSON",
        type="textarea",
        secret=True,
        required=True,
        placeholder="Paste service account JSON here",
        show_when=_GCP_USES_KEY,
    ),
    FieldSpec(key="project_id", label="Project ID", required=True, show_when=_GCP_USES_KEY),
    FieldSpec(
        key="impersonate_service_account",
        label="Service account to impersonate",
        placeholder="octoprox@customer-project.iam.gserviceaccount.com",
        help="Optional. Octoprox mints short-lived tokens for this service account and provisions Compute Engine as it.",
        details=(
            "Use this to provision in a project the base identity has no roles in. The base "
            "identity needs roles/iam.serviceAccountTokenCreator on the target service account; "
            "the target carries the Compute Engine roles."
        ),
    ),
]

_AZURE_USES_SECRET = Condition(field="credential.auth_mode", equals=AzureAuthMode.CLIENT_SECRET.value)
_AZURE_USES_HOST = Condition(field="credential.auth_mode", equals=AzureAuthMode.HOST_IDENTITY.value)

AZURE_CREDENTIAL_FIELDS = [
    FieldSpec(
        key="auth_mode",
        label="Authentication",
        type="select",
        required=True,
        default=AzureAuthMode.CLIENT_SECRET.value,
        options=[
            OptionSpec(
                value=AzureAuthMode.CLIENT_SECRET.value,
                label="Client secret",
                description="A service principal's client secret, stored encrypted.",
            ),
            OptionSpec(
                value=AzureAuthMode.HOST_IDENTITY.value,
                label="Host identity",
                description="The managed identity or workload identity Octoprox runs with. Nothing is stored.",
            ),
        ],
        help="Host identity needs no stored secrets and is the recommended choice when Octoprox runs on Azure.",
        details=(
            "With Host identity, azure-identity's default chain is used: AKS workload identity, "
            "a system- or user-assigned managed identity, or AZURE_* environment variables on "
            "the Octoprox host. See the Azure setup guide."
        ),
    ),
    FieldSpec(key="subscription_id", label="Subscription ID", required=True, show_when=_AZURE_USES_SECRET),
    FieldSpec(key="tenant_id", label="Tenant ID", required=True, show_when=_AZURE_USES_SECRET),
    FieldSpec(key="client_id", label="Client ID", required=True, show_when=_AZURE_USES_SECRET),
    FieldSpec(
        key="client_secret",
        label="Client secret",
        type="password",
        secret=True,
        required=True,
        show_when=_AZURE_USES_SECRET,
    ),
    FieldSpec(key="key_vault_name", label="Key vault name", show_when=_AZURE_USES_SECRET),
    FieldSpec(
        key="managed_identity_client_id",
        label="User-assigned identity client ID",
        show_when=_AZURE_USES_HOST,
        help="Optional. Leave empty for a system-assigned identity or when the host already sets AZURE_CLIENT_ID.",
    ),
    FieldSpec(
        key="target_tenant_id",
        label="Target tenant ID",
        show_when=_AZURE_USES_HOST,
        help="Optional. Tenant of an app registration that trusts the host identity through a federated credential.",
    ),
    FieldSpec(
        key="target_client_id",
        label="Target app (client) ID",
        show_when=_AZURE_USES_HOST,
        help="Optional. Octoprox exchanges the host identity's token for this app's token and provisions VMs as it.",
        details=(
            "Use this to provision in another Entra tenant. The app registration in that tenant "
            "needs a federated credential whose subject is the host identity's principal ID, and "
            "its service principal carries the role assignment."
        ),
    ),
]

COUNTRIES_FIELD = FieldSpec(
    key="countries",
    label="Countries",
    type="country",
    help="Countries these proxies exit from. Clients select one with a -cc-<code> username suffix.",
)

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
    COUNTRIES_FIELD,
    *CLOUD_SCALING_FIELDS,
]

GCP_CONNECTOR_FIELDS = [
    FieldSpec(key="project_id", label="Project ID", required=True),
    FieldSpec(key="instance_name", label="Instance name", required=True),
    FieldSpec(key="zone", label="Zone", required=True, default="us-central1-a"),
    FieldSpec(key="machine_type", label="Machine type", required=True, default="e2-micro"),
    FieldSpec(key="network", label="Network", default="default"),
    COUNTRIES_FIELD,
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
    COUNTRIES_FIELD,
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
            "connector_fields": [COUNTRIES_FIELD],
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
