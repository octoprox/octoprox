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


class TestUserEmailValidation:
    """Tests for email format validation."""

    def test_create_user_invalid_email_no_domain(self, authenticated_client: TestClient) -> None:
        """Test that creating a user with an invalid email (no domain) fails."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "bademail1",
                "email": "notanemail",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 422

    def test_create_user_invalid_email_no_tld(self, authenticated_client: TestClient) -> None:
        """Test that creating a user with an email missing TLD fails."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "bademail2",
                "email": "user@domain",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 422

    def test_create_user_invalid_email_spaces(self, authenticated_client: TestClient) -> None:
        """Test that creating a user with spaces in email fails."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "bademail3",
                "email": "user @example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 422

    def test_create_user_valid_email(self, authenticated_client: TestClient) -> None:
        """Test that valid emails are accepted."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "goodemail",
                "email": "valid.user+tag@sub.example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 201
        assert response.json()["email"] == "valid.user+tag@sub.example.com"

    def test_create_user_empty_email_still_allowed(self, authenticated_client: TestClient) -> None:
        """Test that empty email (opting out) is still allowed."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "noemailval",
                "email": "",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 201

    def test_create_user_email_whitespace_trimmed(self, authenticated_client: TestClient) -> None:
        """Test that whitespace around email is trimmed."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "trimmed",
                "email": "  trimmed@example.com  ",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 201
        assert response.json()["email"] == "trimmed@example.com"

    def test_update_user_invalid_email(self, authenticated_client: TestClient) -> None:
        """Test that updating to an invalid email fails."""
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "updatebademail",
                "email": "good@example.com",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.patch(
            f"/api/v1/users/{user_id}",
            json={"email": "not-valid"},
        )
        assert response.status_code == 422


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

    def test_editor_cannot_invite_user(self, editor_client: TestClient) -> None:
        """Test that editors cannot invite users."""
        response = editor_client.post(
            "/api/v1/users/invite",
            json={
                "username": "noaccess",
                "email": "no@example.com",
                "role": "viewer",
            },
        )
        assert response.status_code == 403


class TestUserInviteFlow:
    """Tests for the invite link user creation flow."""

    def test_invite_user(self, authenticated_client: TestClient) -> None:
        """Test creating a user via invite link."""
        response = authenticated_client.post(
            "/api/v1/users/invite",
            json={
                "username": "invitee",
                "email": "invitee@example.com",
                "role": "editor",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["user"]["username"] == "invitee"
        assert data["user"]["email"] == "invitee@example.com"
        assert data["user"]["role"] == "editor"
        assert data["user"]["has_password"] is False
        assert "invite_url" in data
        assert "/set-password/" in data["invite_url"]

    def test_invite_user_has_password_false_in_list(self, authenticated_client: TestClient) -> None:
        """Test that invited users show has_password=False in list."""
        authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "invitelist", "role": "viewer"},
        )
        response = authenticated_client.get("/api/v1/users")
        users = response.json()["users"]
        invited = [u for u in users if u["username"] == "invitelist"]
        assert len(invited) == 1
        assert invited[0]["has_password"] is False

    def test_invite_user_duplicate_username(self, authenticated_client: TestClient) -> None:
        """Test that inviting a user with a duplicate username fails."""
        authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "dupinvite", "role": "viewer"},
        )
        response = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "dupinvite", "role": "viewer"},
        )
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_invite_user_duplicate_email(self, authenticated_client: TestClient) -> None:
        """Test that inviting a user with a duplicate email fails."""
        authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "invitemail1", "email": "shared-invite@example.com", "role": "viewer"},
        )
        response = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "invitemail2", "email": "shared-invite@example.com", "role": "viewer"},
        )
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_set_password_with_invite_token(self, authenticated_client: TestClient) -> None:
        """Test setting password via invite token returns a JWT."""
        invite_resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "setpwuser", "email": "setpw@example.com", "role": "viewer"},
        )
        invite_url = invite_resp.json()["invite_url"]
        token = invite_url.split("/set-password/")[1]

        # Set password (endpoint is public, works with any client)
        response = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": "mypassword123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_set_password_user_has_password_after(self, authenticated_client: TestClient) -> None:
        """Test that after setting password, has_password becomes True."""
        invite_resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "pwcheck", "role": "viewer"},
        )
        invite_url = invite_resp.json()["invite_url"]
        user_id = invite_resp.json()["user"]["id"]
        token = invite_url.split("/set-password/")[1]

        authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": "mypassword123"},
        )

        user_resp = authenticated_client.get(f"/api/v1/users/{user_id}")
        assert user_resp.json()["has_password"] is True

    def test_set_password_invalid_token(self, authenticated_client: TestClient) -> None:
        """Test that an invalid invite token returns 404."""
        response = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": "nonexistent-token", "password": "pass123"},
        )
        assert response.status_code == 404

    def test_set_password_token_consumed(self, authenticated_client: TestClient) -> None:
        """Test that an invite token can only be used once."""
        invite_resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "onceonly", "role": "viewer"},
        )
        token = invite_resp.json()["invite_url"].split("/set-password/")[1]

        # First use
        resp1 = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": "pass123"},
        )
        assert resp1.status_code == 200

        # Second use — token is consumed
        resp2 = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": "pass456"},
        )
        assert resp2.status_code == 404

    def test_login_blocked_without_password(self, authenticated_client: TestClient) -> None:
        """Test that users without a password cannot login."""
        authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "nologin", "role": "viewer"},
        )
        response = authenticated_client.post(
            "/api/v1/auth/login",
            json={"username": "nologin", "password": "anything"},
        )
        assert response.status_code == 401

    def test_login_works_after_set_password(self, authenticated_client: TestClient) -> None:
        """Test that invited user can login after setting password."""
        invite_resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "loginafter", "role": "viewer"},
        )
        token = invite_resp.json()["invite_url"].split("/set-password/")[1]

        authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": token, "password": "mypass123"},
        )

        login_resp = authenticated_client.post(
            "/api/v1/auth/login",
            json={"username": "loginafter", "password": "mypass123"},
        )
        assert login_resp.status_code == 200
        assert "access_token" in login_resp.json()

    def test_reinvite_user(self, authenticated_client: TestClient) -> None:
        """Test regenerating an invite link for a pending user."""
        invite_resp = authenticated_client.post(
            "/api/v1/users/invite",
            json={"username": "reinvitee", "role": "viewer"},
        )
        user_id = invite_resp.json()["user"]["id"]
        old_token = invite_resp.json()["invite_url"].split("/set-password/")[1]

        # Reinvite
        reinvite_resp = authenticated_client.post(f"/api/v1/users/{user_id}/reinvite")
        assert reinvite_resp.status_code == 200
        new_url = reinvite_resp.json()["invite_url"]
        new_token = new_url.split("/set-password/")[1]
        assert new_token != old_token

        # Old token should no longer work
        old_resp = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": old_token, "password": "pass123"},
        )
        assert old_resp.status_code == 404

        # New token should work
        new_resp = authenticated_client.post(
            "/api/v1/auth/set-password",
            json={"token": new_token, "password": "pass123"},
        )
        assert new_resp.status_code == 200

    def test_reinvite_user_already_has_password(
        self, authenticated_client: TestClient
    ) -> None:
        """Test that reinviting a user who already has a password fails."""
        create_resp = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "haspass",
                "password": "pass123",
                "role": "viewer",
            },
        )
        user_id = create_resp.json()["id"]

        response = authenticated_client.post(f"/api/v1/users/{user_id}/reinvite")
        assert response.status_code == 400
        assert "already has a password" in response.json()["detail"]

    def test_create_with_password_has_password_true(
        self, authenticated_client: TestClient
    ) -> None:
        """Test that users created with password have has_password=True."""
        response = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": "withpass",
                "password": "pass123",
                "role": "viewer",
            },
        )
        assert response.status_code == 201
        assert response.json()["has_password"] is True
