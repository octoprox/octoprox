# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Personal access tokens: minted once, hashed at rest, honoured until revoked."""

from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

import api.models.auth
from api.core import utc_now
from api.core.api_tokens import TOKEN_PREFIX


def _login(client: TestClient, settings: Any) -> dict[str, str]:
    """A session JWT for the seeded admin: the user the fixtures' JWTs name does not exist in the DB."""
    response = client.post(
        "/api/v1/auth/login", json={"username": settings.auth_username, "password": settings.auth_password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestOwnTokens:
    def test_create_list_use_and_revoke(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)

        created = async_client.post("/api/v1/users/me/tokens", json={"name": "ci"}, headers=headers)
        assert created.status_code == 201, created.text
        body = created.json()
        secret = body["token"]
        assert secret.startswith(TOKEN_PREFIX)
        assert body["api_token"]["name"] == "ci"
        assert body["api_token"]["expires_at"] is None
        assert secret.startswith(body["api_token"]["token_prefix"])

        listed = async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"]
        assert [t["id"] for t in listed] == [body["api_token"]["id"]]
        assert "token" not in listed[0] and "token_hash" not in listed[0]

        # The token authenticates as the user, with the user's current role.
        token_headers = {"Authorization": f"Bearer {secret}"}
        me = async_client.get("/api/v1/users/me", headers=token_headers)
        assert me.status_code == 200
        assert me.json()["username"] == test_settings.auth_username
        assert async_client.get("/api/v1/projects", headers=token_headers).status_code == 200
        assert async_client.get("/api/v1/users", headers=token_headers).status_code == 200

        used = async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"][0]
        assert used["last_used_at"] is not None

        revoked = async_client.delete(f"/api/v1/users/me/tokens/{body['api_token']['id']}", headers=headers)
        assert revoked.status_code == 204
        assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 401
        assert async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"] == []

    def test_expiry_is_recorded(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)
        created = async_client.post(
            "/api/v1/users/me/tokens", json={"name": "short", "expires_in_days": 30}, headers=headers
        ).json()
        assert created["api_token"]["expires_at"] is not None
        assert created["api_token"]["expired"] is False
        async_client.delete(f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=headers)

    def test_a_token_cannot_mint_tokens(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)
        secret = async_client.post("/api/v1/users/me/tokens", json={"name": "t"}, headers=headers).json()["token"]
        token_headers = {"Authorization": f"Bearer {secret}"}
        assert async_client.post("/api/v1/users/me/tokens", json={"name": "t2"}, headers=token_headers).status_code == 403
        # Revoking is fine: a leaked token may at least be used to end itself.
        mine = async_client.get("/api/v1/users/me/tokens", headers=token_headers).json()["tokens"]
        for t in mine:
            assert async_client.delete(f"/api/v1/users/me/tokens/{t['id']}", headers=token_headers).status_code == 204

    def test_unknown_token_is_refused(self, async_client: TestClient) -> None:
        response = async_client.get("/api/v1/projects", headers={"Authorization": f"Bearer {TOKEN_PREFIX}nope"})
        assert response.status_code == 401

    def test_name_is_required(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)
        assert async_client.post("/api/v1/users/me/tokens", json={"name": "  "}, headers=headers).status_code == 422


class TestAdminTokens:
    def test_admin_issues_and_revokes_for_a_service_account(self, authenticated_client: TestClient) -> None:
        # A user with no password and no email: it exists to hold tokens.
        user = authenticated_client.post(
            "/api/v1/users/invite", json={"username": "scraper-bot", "role": "viewer"}
        ).json()["user"]
        created = authenticated_client.post(f"/api/v1/users/{user['id']}/tokens", json={"name": "bot"})
        assert created.status_code == 201, created.text
        secret = created.json()["token"]
        assert created.json()["api_token"]["created_by"] == "testadmin"

        token_headers = {"Authorization": f"Bearer {secret}"}
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).json()["username"] == "scraper-bot"
        # The role is the user's, read on every call: a viewer cannot write.
        assert authenticated_client.post("/api/v1/projects", json={"name": "x"}, headers=token_headers).status_code == 403

        # Disabling the user ends the token at once.
        assert authenticated_client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}).status_code == 200
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 401
        assert authenticated_client.patch(f"/api/v1/users/{user['id']}", json={"is_active": True}).status_code == 200
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 200

        listed = authenticated_client.get(f"/api/v1/users/{user['id']}/tokens").json()["tokens"]
        assert len(listed) == 1
        assert authenticated_client.delete(f"/api/v1/users/{user['id']}/tokens/{listed[0]['id']}").status_code == 204
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 401
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")

    def test_deleting_the_user_ends_the_token(self, authenticated_client: TestClient) -> None:
        user = authenticated_client.post("/api/v1/users/invite", json={"username": "temp-bot", "role": "viewer"}).json()["user"]
        secret = authenticated_client.post(f"/api/v1/users/{user['id']}/tokens", json={"name": "t"}).json()["token"]
        token_headers = {"Authorization": f"Bearer {secret}"}
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
        assert authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}").status_code in (200, 204)
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 401

    def test_unknown_user(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/users/nope/tokens").status_code == 404
        assert authenticated_client.post("/api/v1/users/nope/tokens", json={"name": "t"}).status_code == 404

    def test_editor_cannot_manage_others(self, editor_client: TestClient) -> None:
        assert editor_client.get("/api/v1/users/anyone/tokens").status_code == 403
        assert editor_client.post("/api/v1/users/anyone/tokens", json={"name": "t"}).status_code == 403


class TestExpiredToken:
    def test_expired_token_is_refused(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = authenticated_client.post("/api/v1/users/invite", json={"username": "expiring", "role": "viewer"}).json()["user"]
        created = authenticated_client.post(
            f"/api/v1/users/{user['id']}/tokens", json={"name": "t", "expires_in_days": 1}
        ).json()
        token_headers = {"Authorization": f"Bearer {created['token']}"}
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 200

        # Two days later, as the token model sees the clock.
        monkeypatch.setattr(api.models.auth, "utc_now", lambda: utc_now() + timedelta(days=2))
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 401
        listed = authenticated_client.get(f"/api/v1/users/{user['id']}/tokens").json()["tokens"]
        assert listed[0]["expired"] is True
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")
