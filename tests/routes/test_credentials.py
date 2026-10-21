# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for credential endpoints, on an api process: writes through the credential service, reads from Postgres."""

from datetime import datetime
from typing import Any

from sqlalchemy import update
from starlette.testclient import TestClient

from api.db.models import CredentialModel
from api.db.session import SessionFactory
from tests.conftest import api_runtime, on_app_loop


class TestCredentialEndpoints:
    """Tests for credential CRUD endpoints."""

    def test_list_credentials_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test listing credentials when none exist."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/credentials")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["credentials"] == []

    def test_list_credentials_order_is_stable(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_credential_data: dict[str, Any],
    ) -> None:
        """Listing order is creation order, whatever the order Postgres returns rows in.

        Timestamps are pinned equal to exercise the id tie-break, which is what
        keeps bulk-imported entries ordered identically on every api process.
        """
        project_id = created_project["id"]

        created_ids = []
        for i in range(3):
            credential_data = sample_credential_data.copy()
            credential_data["name"] = f"Credential {i}"
            response = authenticated_client.post(
                f"/api/v1/projects/{project_id}/credentials",
                json=credential_data,
            )
            assert response.status_code == 201
            created_ids.append(response.json()["id"])

        def listed_ids() -> list[str]:
            response = authenticated_client.get(f"/api/v1/projects/{project_id}/credentials")
            assert response.status_code == 200
            return [c["id"] for c in response.json()["credentials"]]

        assert listed_ids() == created_ids

        pinned = datetime(2026, 1, 1)
        on_app_loop(authenticated_client, _pin_created_at, api_runtime(authenticated_client).session_factory, created_ids, pinned)

        assert listed_ids() == sorted(created_ids)

    def test_list_credentials_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test listing credentials for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/credentials")

        assert response.status_code == 404

    def test_create_credential(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_credential_data: dict[str, Any],
    ) -> None:
        """Test creating a new credential."""
        project_id = created_project["id"]
        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json=sample_credential_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == sample_credential_data["name"]
        assert data["type"] == sample_credential_data["type"]
        assert data["project_id"] == project_id
        assert "id" in data

    def test_create_credential_project_not_found(
        self,
        authenticated_client: TestClient,
        sample_credential_data: dict[str, Any],
    ) -> None:
        """Test creating credential for non-existent project."""
        response = authenticated_client.post(
            "/api/v1/projects/non-existent/credentials",
            json=sample_credential_data,
        )

        assert response.status_code == 404

    def test_get_credential(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
    ) -> None:
        """Test getting a specific credential."""
        project_id = created_project["id"]
        credential_id = created_credential["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == credential_id
        assert "config" in data  # Detail response includes config

    def test_get_credential_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a non-existent credential."""
        project_id = created_project["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/credentials/non-existent"
        )

        assert response.status_code == 404

    def test_update_credential(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
    ) -> None:
        """Test updating a credential."""
        project_id = created_project["id"]
        credential_id = created_credential["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}",
            json={"name": "Updated Credential"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Credential"

    def test_duplicate_names_are_rejected_within_a_project(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_credential_data: dict[str, Any],
        sample_project_data: dict[str, Any],
    ) -> None:
        """The per-project unique index on lower(name) refuses the duplicate and the route makes it a 400."""
        project_id = created_project["id"]
        url = f"/api/v1/projects/{project_id}/credentials"
        duplicate = authenticated_client.post(url, json=sample_credential_data)
        assert duplicate.status_code == 400 and "already exists" in duplicate.json()["detail"]
        shouting = authenticated_client.post(url, json={**sample_credential_data, "name": sample_credential_data["name"].upper()})
        assert shouting.status_code == 400

        other = authenticated_client.post(url, json={**sample_credential_data, "name": "Second"})
        assert other.status_code == 201, other.text
        # Renaming onto an existing name is refused; keeping or changing case of your own name is not.
        rename = authenticated_client.patch(f"{url}/{other.json()['id']}", json={"name": sample_credential_data["name"]})
        assert rename.status_code == 400
        # The refused rename left the stored credential as it was.
        assert authenticated_client.get(f"{url}/{other.json()['id']}").json()["name"] == "Second"
        recase = authenticated_client.patch(f"{url}/{other.json()['id']}", json={"name": "SECOND"})
        assert recase.status_code == 200 and recase.json()["name"] == "SECOND"
        same = authenticated_client.patch(f"{url}/{created_credential['id']}", json={"name": created_credential["name"]})
        assert same.status_code == 200

        other_project = authenticated_client.post(
            "/api/v1/projects",
            json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
        )
        assert other_project.status_code == 201, other_project.text
        elsewhere = authenticated_client.post(f"/api/v1/projects/{other_project.json()['id']}/credentials", json=sample_credential_data)
        assert elsewhere.status_code == 201

    def test_delete_credential(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
    ) -> None:
        """Test deleting a credential."""
        project_id = created_project["id"]
        credential_id = created_credential["id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}"
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}"
        )
        assert get_response.status_code == 404

    def test_delete_credential_in_use(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test deleting a credential that's in use by a connector fails."""
        project_id = created_project["id"]
        credential_id = created_connector["credential_id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}"
        )

        assert response.status_code == 400
        assert "connector" in response.json()["detail"].lower()

    def test_update_credential_with_invalid_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test updating a credential with invalid config fails validation."""
        project_id = created_project["id"]

        # Create an AWS credential with valid config
        aws_credential_data = {
            "name": "AWS Test Credential",
            "type": "aws",
            "config": {
                "access_key": "AKIAIOSFODNN7EXAMPLE",
                "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            },
        }
        create_response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json=aws_credential_data,
        )
        assert create_response.status_code == 201
        credential_id = create_response.json()["id"]

        # Try to update with empty secret_key - should fail
        update_response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}",
            json={"config": {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": ""}},
        )

        assert update_response.status_code == 422
        assert "secret_key" in update_response.json()["detail"].lower()

    def test_update_credential_with_missing_required_field(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test updating a credential with missing required field fails validation."""
        project_id = created_project["id"]

        # Create an AWS credential with valid config
        aws_credential_data = {
            "name": "AWS Test Credential 2",
            "type": "aws",
            "config": {
                "access_key": "AKIAIOSFODNN7EXAMPLE",
                "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            },
        }
        create_response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json=aws_credential_data,
        )
        assert create_response.status_code == 201
        credential_id = create_response.json()["id"]

        # Try to update with missing secret_key - should fail
        update_response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}",
            json={"config": {"access_key": "AKIAIOSFODNN7EXAMPLE"}},
        )

        assert update_response.status_code == 422

    def test_update_credential_with_valid_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test updating a credential with valid config succeeds."""
        project_id = created_project["id"]

        # Create an AWS credential with valid config
        aws_credential_data = {
            "name": "AWS Test Credential 3",
            "type": "aws",
            "config": {
                "access_key": "AKIAIOSFODNN7EXAMPLE",
                "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            },
        }
        create_response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json=aws_credential_data,
        )
        assert create_response.status_code == 201
        credential_id = create_response.json()["id"]

        # Update with valid new config - should succeed
        update_response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/credentials/{credential_id}",
            json={
                "config": {
                    "access_key": "AKIANEWKEY12345678",
                    "secret_key": "newSecretKey123456789012345678901234567890",
                }
            },
        )

        assert update_response.status_code == 200
        data = update_response.json()
        assert data["config"]["access_key"] == "AKIANEWKEY12345678"


class TestCredentialProjectScope:
    """A credential belongs to one project: under another project's path it is not found."""

    def test_other_projects_credential_is_not_found(
        self,
        authenticated_client: TestClient,
        created_credential: dict[str, Any],
        sample_project_data: dict[str, Any],
    ) -> None:
        other = authenticated_client.post(
            "/api/v1/projects",
            json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
        )
        assert other.status_code == 201, other.text
        url = f"/api/v1/projects/{other.json()['id']}/credentials/{created_credential['id']}"

        assert authenticated_client.get(url).status_code == 404
        assert authenticated_client.patch(url, json={"name": "Taken over"}).status_code == 404
        assert authenticated_client.delete(url).status_code == 404
        mine = f"/api/v1/projects/{created_credential['project_id']}/credentials/{created_credential['id']}"
        assert authenticated_client.get(mine).json()["name"] == created_credential["name"]


class TestCredentialRoleAccess:
    """Tests for role-based access control on credential endpoints."""

    def test_viewer_cannot_create_credential(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot create credentials."""
        response = viewer_client.post(
            "/api/v1/projects/some-project-id/credentials",
            json={
                "name": "Viewer Credential",
                "type": "static_proxy_provider",
                "config": {},
            },
        )
        assert response.status_code == 403

    def test_viewer_cannot_update_credential(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot update credentials."""
        response = viewer_client.patch(
            "/api/v1/projects/some-project-id/credentials/some-credential-id",
            json={"name": "Updated"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_credential(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot delete credentials."""
        response = viewer_client.delete(
            "/api/v1/projects/some-project-id/credentials/some-credential-id"
        )
        assert response.status_code == 403



class TestAWSCredentialAuthModes:
    """AWS credentials: stored access keys, the host's IAM role, and role assumption."""

    @staticmethod
    def _create(client: TestClient, project_id: str, config: dict[str, Any]) -> Any:
        return client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": f"aws-{len(str(config))}", "type": "aws", "config": config},
        )

    def test_access_keys_is_the_default_mode(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        """A config without auth_mode (as saved by older versions) is treated as access keys."""
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"},
        )

        assert response.status_code == 201
        assert response.json()["config"]["auth_mode"] == "access_keys"

    def test_host_role_stores_no_secrets(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(authenticated_client, created_project["id"], {"auth_mode": "host_role"})

        assert response.status_code == 201
        assert response.json()["config"] == {"auth_mode": "host_role"}

    def test_host_role_with_assumed_role(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {
                "auth_mode": "host_role",
                "role_arn": " arn:aws:iam::123456789012:role/octoprox-proxies ",
                "external_id": "acme-7f3b9c",
            },
        )

        assert response.status_code == 201
        assert response.json()["config"] == {
            "auth_mode": "host_role",
            "role_arn": "arn:aws:iam::123456789012:role/octoprox-proxies",
            "external_id": "acme-7f3b9c",
        }

    def test_access_keys_with_assumed_role(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {
                "auth_mode": "access_keys",
                "access_key": "AKIAIOSFODNN7EXAMPLE",
                "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "role_arn": "arn:aws-us-gov:iam::123456789012:role/path/to/octoprox",
            },
        )

        assert response.status_code == 201
        assert response.json()["config"]["role_arn"] == "arn:aws-us-gov:iam::123456789012:role/path/to/octoprox"

    def test_access_keys_mode_requires_both_keys(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "access_keys", "access_key": "AKIAIOSFODNN7EXAMPLE"},
        )

        assert response.status_code == 422
        assert "secret_key" in response.json()["detail"]

    def test_rejects_malformed_role_arn(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_role", "role_arn": "arn:aws:iam::123456789012:user/not-a-role"},
        )

        assert response.status_code == 422
        assert "role_arn" in response.json()["detail"]

    def test_rejects_external_id_without_role(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_role", "external_id": "acme"},
        )

        assert response.status_code == 422
        assert "external_id requires role_arn" in response.json()["detail"]

    def test_rejects_unknown_auth_mode(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(authenticated_client, created_project["id"], {"auth_mode": "magic"})

        assert response.status_code == 422

    def test_switching_to_host_role_drops_stored_keys(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        created = self._create(
            authenticated_client,
            project_id,
            {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"},
        )
        assert created.status_code == 201

        # The UI omits hidden fields; the server must not keep the old keys either way.
        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/credentials/{created.json()['id']}",
            json={"config": {"auth_mode": "host_role", "access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "x"}},
        )

        assert response.status_code == 200
        assert response.json()["config"] == {"auth_mode": "host_role"}

    def test_catalog_describes_the_auth_mode_field(self, authenticated_client: TestClient) -> None:
        """The AWS credential form the catalog serves: its fields as the api's registry declares them."""
        aws = api_runtime(authenticated_client).provider_registry.require("aws")
        # As the catalog serialises them: JSON, by alias.
        fields = {f.key: f.model_dump(mode="json", by_alias=True) for f in aws.credential_fields}

        assert [o["value"] for o in fields["auth_mode"]["options"]] == ["access_keys", "host_role"]
        assert fields["auth_mode"]["default"] == "access_keys"
        assert fields["secret_key"]["show_when"] == {
            "field": "credential.auth_mode",
            "equals": "access_keys",
            "in": None,
            "negate": False,
        }
        assert fields["external_id"]["show_when"]["field"] == "credential.role_arn"


class TestGCPCredentialAuthModes:
    """GCP credentials: stored service account key, the host identity, and impersonation."""

    KEY = '{"type": "service_account", "project_id": "test"}'

    @staticmethod
    def _create(client: TestClient, project_id: str, config: dict[str, Any]) -> Any:
        return client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": f"gcp-{len(str(config))}", "type": "gcp", "config": config},
        )

    def test_service_account_key_is_the_default_mode(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client, created_project["id"], {"service_account_json": self.KEY, "project_id": "test"}
        )

        assert response.status_code == 201
        assert response.json()["config"]["auth_mode"] == "service_account_key"

    def test_host_identity_stores_no_secrets(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_identity", "service_account_json": self.KEY, "project_id": "stale"},
        )

        assert response.status_code == 201
        assert response.json()["config"] == {"auth_mode": "host_identity"}

    def test_host_identity_with_impersonation(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_identity", "impersonate_service_account": " octoprox@customer.iam.gserviceaccount.com "},
        )

        assert response.status_code == 201
        assert response.json()["config"] == {
            "auth_mode": "host_identity",
            "impersonate_service_account": "octoprox@customer.iam.gserviceaccount.com",
        }

    def test_key_mode_requires_the_key(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(authenticated_client, created_project["id"], {"auth_mode": "service_account_key", "project_id": "p"})

        assert response.status_code == 422
        assert "service_account_json" in response.json()["detail"]

    def test_rejects_malformed_impersonation_target(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_identity", "impersonate_service_account": "projects/x/serviceAccounts/y"},
        )

        assert response.status_code == 422
        assert "impersonate_service_account" in response.json()["detail"]


class TestAzureCredentialAuthModes:
    """Azure credentials: stored client secret, the host identity, and tenant federation."""

    GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @staticmethod
    def _create(client: TestClient, project_id: str, config: dict[str, Any]) -> Any:
        return client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": f"azure-{len(str(config))}", "type": "azure", "config": config},
        )

    def test_client_secret_is_the_default_mode(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"subscription_id": "s", "tenant_id": "t", "client_id": "c", "client_secret": "x"},
        )

        assert response.status_code == 201
        assert response.json()["config"]["auth_mode"] == "client_secret"

    def test_host_identity_stores_no_secrets(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_identity", "tenant_id": "t", "client_id": "c", "client_secret": "stale"},
        )

        assert response.status_code == 201
        assert response.json()["config"] == {"auth_mode": "host_identity"}

    def test_host_identity_with_federated_target(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {
                "auth_mode": "host_identity",
                "managed_identity_client_id": self.GUID,
                "target_tenant_id": "customer.onmicrosoft.com",
                "target_client_id": self.GUID,
            },
        )

        assert response.status_code == 201
        assert response.json()["config"] == {
            "auth_mode": "host_identity",
            "managed_identity_client_id": self.GUID,
            "target_tenant_id": "customer.onmicrosoft.com",
            "target_client_id": self.GUID,
        }

    def test_federation_needs_both_target_fields(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client, created_project["id"], {"auth_mode": "host_identity", "target_client_id": self.GUID}
        )

        assert response.status_code == 422
        assert "set together" in response.json()["detail"]

    def test_rejects_non_guid_target_client_id(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "host_identity", "target_tenant_id": "t", "target_client_id": "octoprox-app"},
        )

        assert response.status_code == 422
        assert "target_client_id" in response.json()["detail"]

    def test_host_only_fields_are_rejected_in_secret_mode(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {
                "auth_mode": "client_secret",
                "subscription_id": "s", "tenant_id": "t", "client_id": "c", "client_secret": "x",
                "target_tenant_id": "t2", "target_client_id": self.GUID,
            },
        )

        assert response.status_code == 422
        assert "host_identity mode" in response.json()["detail"]

    def test_secret_mode_requires_the_secret(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        response = self._create(
            authenticated_client,
            created_project["id"],
            {"auth_mode": "client_secret", "subscription_id": "s", "tenant_id": "t", "client_id": "c"},
        )

        assert response.status_code == 422
        assert "client_secret" in response.json()["detail"]


async def _pin_created_at(session_factory: SessionFactory, credential_ids: list[str], at: datetime) -> None:
    """Give the credentials one creation time, as a bulk import can."""
    async with session_factory() as session:
        await session.execute(
            update(CredentialModel).where(CredentialModel.id.in_(credential_ids)).values(created_at=at)
        )
        await session.commit()
