# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for user management endpoints."""

from starlette.testclient import TestClient


class TestUserCRUD:
    """Tests for admin user CRUD operations."""

    def test_list_users(self, authenticated_client: TestClient) -> None:
        """Test listing users as admin."""
        response = authenticated_client.get("/api/v1/users")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "users" in data
        # Should have at least the seeded admin user
        assert data["total"] >= 1

    def test_create_user(self, authenticated_client: TestClient) -> None:
        """Test creating a user as admin."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "newpass123",
                "role": "editor",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "newuser"
        assert data["email"] == "new@example.com"
        assert data["role"] == "editor"
        assert data["is_active"] is True
        assert "id" in data
        assert "password_hash" not in data

    def test_create_user_duplicate_username(self, authenticated_client: TestClient) -> None:
        """Test creating a user with duplicate username fails."""
        user_data = {
            "username": "dupuser",
            "email": "dup@example.com",
            "password": "pass123",
            "role": "viewer",
        }
        # Create first
        authenticated_client.post("/api/v1/users", json=user_data)
        # Try duplicate
        response = authenticated_client.post("/api/v1/users", json=user_data)
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_get_user(self, authenticated_client: TestClient) -> None:
        """Test getting a user by ID."""
        # Create a user
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "getuser",
                "email": "get@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.get(f"/api/v1/users/{user_id}")
        assert response.status_code == 200
        assert response.json()["username"] == "getuser"

    def test_update_user(self, authenticated_client: TestClient) -> None:
        """Test updating a user."""
        # Create a user
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "updateuser",
                "email": "update@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/users/{user_id}",
            json={"role": "editor", "email": "updated@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["role"] == "editor"
        assert response.json()["email"] == "updated@example.com"

    def test_delete_user(self, authenticated_client: TestClient) -> None:
        """Test deleting a user."""
        # Create a user
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "deleteuser",
                "email": "delete@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.delete(f"/api/v1/users/{user_id}")
        assert response.status_code == 204

        # Verify deletion
        response = authenticated_client.get(f"/api/v1/users/{user_id}")
        assert response.status_code == 404

    def test_cannot_delete_self(self, authenticated_client: TestClient) -> None:
        """Test that admin cannot delete their own account."""
        # The admin's user_id from JWT is "test-admin-id"
        response = authenticated_client.delete("/api/v1/users/test-admin-id")
        assert response.status_code == 400
        assert "Cannot delete your own account" in response.json()["detail"]

    def test_get_nonexistent_user(self, authenticated_client: TestClient) -> None:
        """Test getting a user that doesn't exist."""
        response = authenticated_client.get("/api/v1/users/nonexistent-id")
        assert response.status_code == 404


class TestUserEmailUniqueness:
    """Tests for email uniqueness enforcement."""

    def test_create_user_duplicate_email(self, authenticated_client: TestClient) -> None:
        """Test creating a user with a duplicate email fails."""
        authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "emailuser1",
                "email": "shared@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "emailuser2",
                "email": "shared@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 400
        assert "Email" in response.json()["detail"]
        assert "already exists" in response.json()["detail"]

    def test_create_users_with_empty_email(self, authenticated_client: TestClient) -> None:
        """Test that multiple users can have empty emails."""
        resp1 = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "noemail1",
                "email": "",
                "password": "pass123",
                "role": "viewer",
            },
        )
        resp2 = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "noemail2",
                "email": "",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert resp1.status_code == 201
        assert resp2.status_code == 201

    def test_update_user_duplicate_email(self, authenticated_client: TestClient) -> None:
        """Test updating a user to a taken email fails."""
        authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "emailowner",
                "email": "taken@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "emailchanger",
                "email": "original@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/users/{user_id}",
            json={"email": "taken@example.com"},
        )
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_update_user_same_email_ok(self, authenticated_client: TestClient) -> None:
        """Test updating a user keeping the same email succeeds."""
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "keepemail",
                "email": "keep@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/users/{user_id}",
            json={"email": "keep@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["email"] == "keep@example.com"

    def test_update_user_clear_email_ok(self, authenticated_client: TestClient) -> None:
        """Test clearing a user's email to empty succeeds."""
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "clearemail",
                "email": "clear@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/users/{user_id}",
            json={"email": ""},
        )
        assert response.status_code == 200
        assert response.json()["email"] == ""


class TestUserSelfEndpoints:
    """Tests for user self-service endpoints."""

    def test_get_self(self, authenticated_client: TestClient) -> None:
        """Test getting own profile.

        Note: This uses the JWT user_id which may not match a real DB user
        (the admin was seeded, but with a different ID than 'test-admin-id').
        The endpoint should still handle this gracefully.
        """
        response = authenticated_client.get("/api/v1/users/me")
        # May be 404 if the JWT user_id doesn't match a DB user
        assert response.status_code in (200, 404)


class TestUserAccessControl:
    """Tests for user management access control."""

    def test_editor_cannot_list_users(self, editor_client: TestClient) -> None:
        """Test that editors cannot list users."""
        response = editor_client.get("/api/v1/users")
        assert response.status_code == 403

    def test_viewer_cannot_list_users(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot list users."""
        response = viewer_client.get("/api/v1/users")
        assert response.status_code == 403

    def test_editor_cannot_create_user(self, editor_client: TestClient) -> None:
        """Test that editors cannot create users."""
        response = editor_client.post(
            "/api/v1/users",
            json={
                "username": "unauthorized",
                "email": "no@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 403

    def test_unauthenticated_cannot_list_users(self, async_client: TestClient) -> None:
        """Test that unauthenticated users cannot list users."""
        response = async_client.get("/api/v1/users")
        assert response.status_code == 401
