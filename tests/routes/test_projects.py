# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for project endpoints."""

from typing import Any

from starlette.testclient import TestClient


class TestProjectEndpoints:
    """Tests for project CRUD endpoints."""

    def test_list_projects_default(self, authenticated_client: TestClient) -> None:
        """Test listing projects returns successfully."""
        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 200
        data = response.json()
        # Verify response structure (may be empty if other tests truncated tables)
        assert "total" in data
        assert "projects" in data
        assert isinstance(data["projects"], list)
        assert data["total"] >= 0

    def test_create_project(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
    ) -> None:
        """Test creating a new project."""
        response = authenticated_client.post("/api/v1/projects", json=sample_project_data)

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == sample_project_data["name"]
        assert data["username"] == sample_project_data["username"]
        assert "id" in data
        assert "created_at" in data

    def test_create_project_duplicate_username(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
    ) -> None:
        """Test creating a project with duplicate username fails."""
        # Create first project
        authenticated_client.post("/api/v1/projects", json=sample_project_data)

        # Try to create another with same username
        response = authenticated_client.post("/api/v1/projects", json=sample_project_data)

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_get_project(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a specific project."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == project_id
        assert data["name"] == created_project["name"]

    def test_get_project_not_found(self, authenticated_client: TestClient) -> None:
        """Test getting a non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent-id")

        assert response.status_code == 404

    def test_update_project(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test updating a project."""
        project_id = created_project["id"]
        update_data = {"name": "Updated Name", "description": "Updated description"}

        response = authenticated_client.patch(f"/api/v1/projects/{project_id}", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["description"] == "Updated description"

    def test_update_project_not_found(self, authenticated_client: TestClient) -> None:
        """Test updating a non-existent project."""
        response = authenticated_client.patch(
            "/api/v1/projects/non-existent-id",
            json={"name": "New Name"},
        )

        assert response.status_code == 404

    def test_delete_project(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test deleting a project."""
        project_id = created_project["id"]

        response = authenticated_client.request(
            "DELETE",
            f"/api/v1/projects/{project_id}",
            json={"confirmation": "permanently delete"},
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(f"/api/v1/projects/{project_id}")
        assert get_response.status_code == 404

    def test_delete_project_without_confirmation(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test deleting a project without proper confirmation fails."""
        project_id = created_project["id"]

        response = authenticated_client.request(
            "DELETE",
            f"/api/v1/projects/{project_id}",
            json={"confirmation": "wrong"},
        )

        assert response.status_code == 400
        assert "Confirmation required" in response.json()["detail"]

    def test_delete_project_not_found(self, authenticated_client: TestClient) -> None:
        """Test deleting a non-existent project."""
        response = authenticated_client.request(
            "DELETE",
            "/api/v1/projects/non-existent-id",
            json={"confirmation": "permanently delete"},
        )

        assert response.status_code == 404

    def test_create_project_with_mitm_mode(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
    ) -> None:
        """Test creating a project with MITM mode fields."""
        project_data = sample_project_data.copy()
        project_data["tls_mitm_mode"] = "match_ua"
        project_data["tls_mitm_engine"] = "curl_cffi"

        response = authenticated_client.post("/api/v1/projects", json=project_data)

        assert response.status_code == 201
        data = response.json()
        assert data["tls_mitm_mode"] == "match_ua"
        assert data["tls_mitm_engine"] == "curl_cffi"
        assert data["tls_mitm_browser"] is None

    def test_create_project_default_mitm_mode(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
    ) -> None:
        """Test that project defaults to MITM off."""
        response = authenticated_client.post("/api/v1/projects", json=sample_project_data)

        assert response.status_code == 201
        data = response.json()
        assert data["tls_mitm_mode"] == "off"
        assert data["tls_mitm_engine"] is None
        assert data["tls_mitm_browser"] is None

    def test_update_project_mitm_mode(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test updating MITM mode from off to override_ua."""
        project_id = created_project["id"]
        update_data = {
            "tls_mitm_mode": "override_ua",
            "tls_mitm_engine": "rnet",
            "tls_mitm_browser": "firefox",
        }

        response = authenticated_client.patch(f"/api/v1/projects/{project_id}", json=update_data)

        assert response.status_code == 200
        data = response.json()
        assert data["tls_mitm_mode"] == "override_ua"
        assert data["tls_mitm_engine"] == "rnet"
        assert data["tls_mitm_browser"] == "firefox"

    def test_list_projects_with_data(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
    ) -> None:
        """Test listing projects when some exist."""
        import uuid

        # Get initial count (includes default project from migrations)
        initial_response = authenticated_client.get("/api/v1/projects")
        initial_count = initial_response.json()["total"]

        # Create multiple projects with unique usernames
        for i in range(3):
            unique_id = uuid.uuid4().hex[:8]
            project_data = sample_project_data.copy()
            project_data["name"] = f"Project {i}"
            project_data["username"] = f"user{i}_{unique_id}"
            authenticated_client.post("/api/v1/projects", json=project_data)

        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 200
        data = response.json()
        # Should have 3 more projects than initial count
        assert data["total"] == initial_count + 3
        assert len(data["projects"]) == initial_count + 3

