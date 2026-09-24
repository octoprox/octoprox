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


class TestLastLogin:
    """``last_login_at`` is stamped whenever a token is issued.

    Login and set-password are public endpoints that ignore any bearer
    header, so the admin client is used for them too: opening a second
    TestClient on the same app would run the lifespan twice.
    """

    def _create(self, admin: TestClient, username: str, password: str) -> dict:
        resp = admin.post(
            "/api/v1/users",
            json={"username": username, "email": "", "password": password, "role": "viewer"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_new_user_has_never_logged_in(self, authenticated_client: TestClient) -> None:
        user = self._create(authenticated_client, "lastlogin-fresh", "pass12345")
        assert user["last_login_at"] is None

    def test_login_stamps_last_login(self, authenticated_client: TestClient) -> None:
        user = self._create(authenticated_client, "lastlogin-user", "pass12345")

        resp = authenticated_client.post(
            "/api/v1/auth/login", json={"username": "lastlogin-user", "password": "pass12345"}
        )
        assert resp.status_code == 200

        after = authenticated_client.get(f"/api/v1/users/{user['id']}").json()
        assert after["last_login_at"] is not None
        # A login is not a profile edit.
        assert after["updated_at"] == user["updated_at"]

    def test_failed_login_does_not_stamp(self, authenticated_client: TestClient) -> None:
        user = self._create(authenticated_client, "lastlogin-fail", "pass12345")

        resp = authenticated_client.post(
            "/api/v1/auth/login", json={"username": "lastlogin-fail", "password": "wrong"}
        )
        assert resp.status_code == 401

        after = authenticated_client.get(f"/api/v1/users/{user['id']}").json()
        assert after["last_login_at"] is None

    def test_invite_acceptance_stamps_last_login(self, authenticated_client: TestClient) -> None:
        resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "lastlogin-invited", "email": "", "role": "viewer"},
        )
        assert resp.status_code == 201, resp.text
        invited = resp.json()
        assert invited["user"]["last_login_at"] is None
        # Invite URLs look like <base>/set-password/<token>
        token = invited["invite_url"].rstrip("/").rsplit("/", 1)[-1]

        resp = authenticated_client.post(
            "/api/v1/auth/set-password", json={"token": token, "password": "pass12345"}
        )
        assert resp.status_code == 200, resp.text

        after = authenticated_client.get(f"/api/v1/users/{invited['user']['id']}").json()
        assert after["last_login_at"] is not None
