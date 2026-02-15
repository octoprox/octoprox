"""Tests for credential endpoints."""

from typing import Any

from starlette.testclient import TestClient


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

