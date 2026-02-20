# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for authentication endpoints."""

from starlette.testclient import TestClient


class TestAuthEndpointsDisabled:
    """Tests for auth endpoints when authentication is disabled."""

    def test_auth_status_disabled(self, async_client: TestClient) -> None:
        """Test auth status when auth is disabled."""
        response = async_client.get("/api/v1/auth/status")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False

    def test_login_fails_when_disabled(self, async_client: TestClient) -> None:
        """Test that login fails when auth is disabled."""
        response = async_client.post(
            "/api/v1/auth/login",
            json={"username": "test", "password": "test"},
        )

        assert response.status_code == 400
        assert "not enabled" in response.json()["detail"]


class TestAuthEndpointsEnabled:
    """Tests for auth endpoints when authentication is enabled."""

    def test_auth_status_enabled(
        self,
        auth_client: TestClient,
    ) -> None:
        """Test auth status when auth is enabled."""
        response = auth_client.get("/api/v1/auth/status")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["authenticated"] is False

    def test_login_success(
        self,
        auth_client: TestClient,
        test_settings_auth_enabled,
    ) -> None:
        """Test successful login."""
        response = auth_client.post(
            "/api/v1/auth/login",
            json={
                "username": test_settings_auth_enabled.auth_username,
                "password": test_settings_auth_enabled.auth_password,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "expires_in" in data

    def test_login_invalid_credentials(
        self,
        auth_client: TestClient,
    ) -> None:
        """Test login with invalid credentials."""
        response = auth_client.post(
            "/api/v1/auth/login",
            json={"username": "wrong", "password": "wrong"},
        )

        assert response.status_code == 401
        assert "Invalid" in response.json()["detail"]

    def test_auth_status_authenticated(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test auth status when authenticated."""
        response = authenticated_client.get("/api/v1/auth/status")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["authenticated"] is True
        assert data["username"] is not None

    def test_protected_endpoint_without_auth(
        self,
        auth_client: TestClient,
    ) -> None:
        """Test accessing protected endpoint without authentication."""
        response = auth_client.get("/api/v1/projects")

        assert response.status_code == 401

    def test_protected_endpoint_with_auth(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test accessing protected endpoint with authentication."""
        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 200

