# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for connector endpoints."""

from typing import Any

from starlette.testclient import TestClient


class TestConnectorEndpoints:
    """Tests for connector CRUD endpoints."""

    def test_list_connectors_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
    ) -> None:
        """Test listing connectors when none exist (only credential, no connector)."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/connectors")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["connectors"] == []

    def test_list_connectors_project_not_found(
        self,
            authenticated_client: TestClient,
    ) -> None:
        """Test listing connectors for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/connectors")

        assert response.status_code == 404

    def test_create_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a new connector."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == connector_data["name"]
        assert data["credential_id"] == created_credential["id"]
        assert data["project_id"] == project_id
        assert "id" in data

    def test_create_connector_credential_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating connector with non-existent credential."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = "non-existent"

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 404
        assert "Credential" in response.json()["detail"]

    def test_get_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test getting a specific connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == connector_id
        assert data["credential_name"] is not None

    def test_get_connector_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a non-existent connector."""
        project_id = created_project["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/non-existent"
        )

        assert response.status_code == 404

    def test_update_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test updating a connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"name": "Updated Connector", "enabled": False},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Connector"
        assert data["enabled"] is False

    def test_delete_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test deleting a connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )
        assert get_response.status_code == 404

    def test_create_connector_with_whitelist(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with a domain whitelist."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_whitelist": ["example.com", "test.org"]}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {"domain_whitelist": ["example.com", "test.org"]}

    def test_create_connector_with_blacklist(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with a domain blacklist."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_blacklist": ["blocked.com"]}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {"domain_blacklist": ["blocked.com"]}

    def test_create_connector_both_lists_rejected(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that creating a connector with both whitelist and blacklist is rejected."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {
            "domain_whitelist": ["a.com"],
            "domain_blacklist": ["b.com"],
        }

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 422

    def test_create_connector_empty_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with empty routing config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {}

    def test_update_connector_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test updating a connector's routing config."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"routing_config": {"domain_whitelist": ["updated.com"]}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["routing_config"] == {"domain_whitelist": ["updated.com"]}

    def test_update_connector_routing_config_both_lists_rejected(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test that updating with both whitelist and blacklist is rejected."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"routing_config": {
                "domain_whitelist": ["a.com"],
                "domain_blacklist": ["b.com"],
            }},
        )

        assert response.status_code == 422

    def test_get_connector_includes_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that GET connector response includes routing_config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_blacklist": ["blocked.com"]}

        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )
        assert create_resp.status_code == 201
        connector_id = create_resp.json()["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["routing_config"] == {"domain_blacklist": ["blocked.com"]}

    def test_list_connectors_includes_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that list connectors includes routing_config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_whitelist": ["example.com"]}

        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )
        assert create_resp.status_code == 201

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/connectors")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["connectors"][0]["routing_config"] == {"domain_whitelist": ["example.com"]}

    def test_get_connector_options(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting connector options."""
        response = authenticated_client.get("/api/v1/connector-options")

        assert response.status_code == 200
        # Response should contain available options
        data = response.json()
        assert isinstance(data, dict)

