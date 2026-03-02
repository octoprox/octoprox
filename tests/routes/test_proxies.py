# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for proxy endpoints."""

from typing import Any

from starlette.testclient import TestClient


class TestProxyEndpoints:
    """Tests for proxy CRUD endpoints."""

    def test_list_proxies_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test listing proxies when none exist (only connector, no proxy)."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["healthy"] == 0
        assert data["proxies"] == []

    def test_list_proxies_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test listing proxies for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/proxies")

        assert response.status_code == 404

    def test_create_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test creating a new proxy."""
        project_id = created_project["id"]
        proxy_data = sample_proxy_data.copy()
        proxy_data["connector_id"] = created_connector["id"]

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json=proxy_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["host"] == proxy_data["host"]
        assert data["port"] == proxy_data["port"]
        assert data["connector_id"] == created_connector["id"]
        assert "id" in data

    def test_create_proxy_connector_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test creating proxy with non-existent connector."""
        project_id = created_project["id"]
        proxy_data = sample_proxy_data.copy()
        proxy_data["connector_id"] = "non-existent"

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json=proxy_data,
        )

        assert response.status_code == 404
        assert "Connector" in response.json()["detail"]

    def test_get_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test getting a specific proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == proxy_id
        assert data["connector_name"] is not None

    def test_get_proxy_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a non-existent proxy."""
        project_id = created_project["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/non-existent"
        )

        assert response.status_code == 404

    def test_update_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test updating a proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}",
            json={"host": "updated.example.com", "port": 9090},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "updated.example.com"
        assert data["port"] == 9090

    def test_delete_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test deleting a proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )
        assert get_response.status_code == 404

    def test_list_proxies_with_data(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test listing proxies when some exist."""
        project_id = created_project["id"]

        # Create multiple proxies
        for i in range(3):
            proxy_data = sample_proxy_data.copy()
            proxy_data["connector_id"] = created_connector["id"]
            proxy_data["host"] = f"proxy{i}.example.com"
            authenticated_client.post(
                f"/api/v1/projects/{project_id}/proxies",
                json=proxy_data,
            )

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["proxies"]) == 3


class TestProxyRoleAccess:
    """Tests for role-based access control on proxy endpoints."""

    def test_viewer_cannot_create_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot create proxies."""
        response = viewer_client.post(
            "/api/v1/projects/some-project-id/proxies",
            json={
                "host": "proxy.example.com",
                "port": 8080,
                "protocol": "http",
                "connector_id": "some-connector-id",
            },
        )
        assert response.status_code == 403

    def test_viewer_cannot_update_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot update proxies."""
        response = viewer_client.patch(
            "/api/v1/projects/some-project-id/proxies/some-proxy-id",
            json={"host": "updated.example.com"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot delete proxies."""
        response = viewer_client.delete(
            "/api/v1/projects/some-project-id/proxies/some-proxy-id"
        )
        assert response.status_code == 403

