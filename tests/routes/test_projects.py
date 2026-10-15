# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for project endpoints."""

import uuid
from typing import Any

import pytest
from starlette.testclient import TestClient

from api.core.config import Settings
from api.core.username_params import LOCATION_INVALID_COUNTRY, LOCATION_NEEDS_COUNTRY


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


class TestProjectRoleAccess:
    """Tests for role-based access control on project endpoints."""

    def test_viewer_can_list_projects(self, viewer_client: TestClient) -> None:
        """Test that viewers can list projects."""
        response = viewer_client.get("/api/v1/projects")
        assert response.status_code == 200

    def test_viewer_cannot_create_project(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot create projects."""
        response = viewer_client.post(
            "/api/v1/projects",
            json={
                "name": "Viewer Project",
                "username": "viewerproject",
                "password": "pass123",
            },
        )
        assert response.status_code == 403

    def test_viewer_cannot_update_project(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot update projects."""
        response = viewer_client.patch(
            "/api/v1/projects/some-project-id",
            json={"name": "Updated"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_project(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot delete projects."""
        response = viewer_client.request(
            "DELETE",
            "/api/v1/projects/some-project-id",
            json={"confirmation": "permanently delete"},
        )
        assert response.status_code == 403

    def test_editor_can_create_project(self, editor_client: TestClient) -> None:
        """Test that editors can create projects."""
        unique_id = uuid.uuid4().hex[:8]
        response = editor_client.post(
            "/api/v1/projects",
            json={
                "name": f"Editor Project {unique_id}",
                "username": f"editorproject_{unique_id}",
                "password": "pass123",
            },
        )
        assert response.status_code == 201



class TestConnectInfo:
    """GET /projects/{id}/connect: the username for a place and session, read as the proxy reads it."""

    def _get(self, client: TestClient, project: dict[str, Any], **params: str) -> dict[str, Any]:
        response = client.get(f"/api/v1/projects/{project['id']}/connect", params=params)
        assert response.status_code == 200
        return response.json()

    def test_plain_username_and_default_endpoint(
        self, authenticated_client: TestClient, created_project: dict[str, Any], test_settings: Settings
    ) -> None:
        data = self._get(authenticated_client, created_project)
        assert data["username"] == created_project["username"]
        assert data["location"] is None and data["sessid"] is None and data["error"] is None
        assert data["endpoint"] == {"scheme": "http", "host": None, "port": test_settings.proxy_port}
        assert data["routes"] == [] and data["places"] == [] and data["countries"] == []
        assert data["any_country"] is False

    def test_configured_endpoint(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        test_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(test_settings, "proxy_public_host", "proxy.example.com")
        monkeypatch.setattr(test_settings, "proxy_public_port", 18080)
        data = self._get(authenticated_client, created_project)
        assert data["endpoint"] == {"scheme": "http", "host": "proxy.example.com", "port": 18080}
        # A TLS load balancer in front of the listener, on the default port.
        monkeypatch.setattr(test_settings, "proxy_public_url", "https://proxy.example.com")
        data = self._get(authenticated_client, created_project)
        assert data["endpoint"] == {"scheme": "https", "host": "proxy.example.com", "port": None}

    def test_place_and_session_are_normalised(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        data = self._get(authenticated_client, created_project, country="uk", state="ENG", city="City of London", sessid=" job-1 ")
        assert data["username"] == f"{created_project['username']}-cc-gb-st-eng-city-london-sessid-job-1"
        assert data["location"] == {"country": "GB", "state": "ENG", "city": "london"}
        assert data["sessid"] == "job-1" and data["error"] is None

    def test_refusals_are_the_proxy_messages(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        username = created_project["username"]
        data = self._get(authenticated_client, created_project, state="ny")
        assert data["error"] == LOCATION_NEEDS_COUNTRY and data["username"] == f"{username}-st-ny"
        assert self._get(authenticated_client, created_project, country="usa")["error"] == LOCATION_INVALID_COUNTRY
        assert "cannot contain -cc-" in self._get(authenticated_client, created_project, sessid="a-cc-de")["error"]
        assert "cannot contain -cc-" in self._get(authenticated_client, created_project, country="us", city="x-sessid-y")["error"]
        # A delimiter formed with the dash around a value: "-sessid-st-ny" would read as state NY with no session.
        assert "cannot contain -cc-" in self._get(authenticated_client, created_project, country="us", sessid="st-ny")["error"]
        assert "cannot contain -cc-" in self._get(authenticated_client, created_project, country="us", city="paris-st", sessid="1")["error"]
        # At the end of the username nothing follows, so a trailing "-cc" is only part of the session ID.
        data = self._get(authenticated_client, created_project, sessid="job-cc")
        assert data["error"] is None and data["username"] == f"{username}-sessid-job-cc"
        data = self._get(authenticated_client, created_project, sessid="a:b")
        assert data["error"] == "A session ID cannot contain a colon" and data["routes"] == []

    def test_viewer_can_read_it(self, viewer_client: TestClient) -> None:
        assert viewer_client.get("/api/v1/projects/non-existent-id/connect").status_code == 404
