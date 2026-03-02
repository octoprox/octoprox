# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for authentication endpoints."""

from starlette.testclient import TestClient


class TestAuthEndpoints:
    """Tests for auth endpoints."""

    def test_auth_status_unauthenticated(self, async_client: TestClient) -> None:
        """Test auth status when not authenticated."""
        response = async_client.get("/api/v1/auth/status")

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False
        assert data["username"] is None
        assert data["role"] is None

    def test_login_success(
        self,
        async_client: TestClient,
        test_settings,
    ) -> None:
        """Test successful login with seeded admin user."""
        response = async_client.post(
            "/api/v1/auth/login",
            json={
                "username": test_settings.auth_username,
                "password": test_settings.auth_password,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "expires_in" in data

    def test_login_invalid_credentials(
        self,
        async_client: TestClient,
    ) -> None:
        """Test login with invalid credentials."""
        response = async_client.post(
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
        assert data["authenticated"] is True
        assert data["username"] is not None
        assert data["role"] == "admin"

    def test_protected_endpoint_without_auth(
        self,
        async_client: TestClient,
    ) -> None:
        """Test accessing protected endpoint without authentication."""
        response = async_client.get("/api/v1/projects")

        assert response.status_code == 401

    def test_protected_endpoint_with_auth(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test accessing protected endpoint with authentication."""
        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 200
