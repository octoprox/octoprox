# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for authentication endpoints, and what the audit log hears of sign-ins.

The audit helpers at the top serve the user, sign-in provider and token tests too.
"""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Any, Self
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

import api.routes.auth
from api.core import utc_now
from api.core.audit import Actor, AuditLog
from api.core.config import Settings
from api.db.auth_repository import ApiTokenRepository, AuthIdentityRepository
from api.db.session import get_db
from api.models.auth import ApiToken, AuthIdentity
from api.models.user import User
from tests.conftest import api_runtime, on_app_loop

# --- Audit helpers -------------------------------------------------------------------------------


class HookRecorder(AuditLog):
    """The free audit log, keeping each sign-in and token hook call as ``(hook, what it was told)``.

    The token and linked sign-in hooks also say whether the row is in their session, which is the request's.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def install(cls, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Self:
        """A recorder in place of ``app``'s audit log for the rest of the test."""
        recorder = cls()
        monkeypatch.setattr(app.state, "audit_log", recorder)
        return recorder

    def told(self, *hooks: str) -> list[tuple[str, dict[str, Any]]]:
        """The calls of ``hooks``, in order."""
        return [call for call in self.calls if call[0] in hooks]

    async def signed_in(
        self, request: Request, user: User, method: str, *, provider: str | None = None, linked: bool = False
    ) -> None:
        self.calls.append(("signed_in", {"user": user.username, "method": method, "provider": provider, "linked": linked}))

    async def sign_in_failed(
        self,
        request: Request,
        method: str,
        reason: str,
        *,
        user: User | None = None,
        username: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.calls.append(
            (
                "sign_in_failed",
                {
                    "method": method,
                    "reason": reason,
                    "user": user.username if user else None,
                    "username": username,
                    "provider": provider,
                },
            )
        )

    async def token_issued(self, session: AsyncSession, actor: Actor, token: ApiToken) -> None:
        stored = await self._has_token(session, token)
        self.calls.append(("token_issued", {"by": actor.name, "token": token.name, "stored": stored}))

    async def token_first_used(self, request: Request, token: ApiToken, user: User) -> None:
        self.calls.append(("token_first_used", {"token": token.name, "user": user.username}))

    async def token_revoked(self, session: AsyncSession, actor: Actor, token: ApiToken) -> None:
        stored = await self._has_token(session, token)
        self.calls.append(("token_revoked", {"by": actor.name, "token": token.name, "stored": stored}))

    async def identity_unlinked(self, session: AsyncSession, actor: Actor, identity: AuthIdentity) -> None:
        linked = await AuthIdentityRepository(session).list_for_user(identity.user_id)
        self.calls.append(
            (
                "identity_unlinked",
                {
                    "by": actor.name,
                    "provider": identity.provider.value,
                    "email": identity.email,
                    "stored": identity.id in {i.id for i in linked},
                },
            )
        )

    @staticmethod
    async def _has_token(session: AsyncSession, token: ApiToken) -> bool:
        return token.id in {t.id for t in await ApiTokenRepository(session).list_for_user(token.user_id)}


class SessionWatch(HookRecorder):
    """A hook recorder that also notes, at each sign-in hook, whether the request's session still holds a transaction:
    ``(reason, or "signed_in", held)``."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list[tuple[str, bool]] = []

    @classmethod
    def install(cls, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Self:
        """A watch in place of ``app``'s audit log, and each request's ``get_db`` session kept on the request."""
        watch = super().install(app, monkeypatch)

        async def kept(request: Request) -> AsyncIterator[AsyncSession]:
            async with asynccontextmanager(get_db)() as session:
                request.state.db_session = session
                yield session

        monkeypatch.setitem(app.dependency_overrides, get_db, kept)
        return watch

    async def signed_in(
        self, request: Request, user: User, method: str, *, provider: str | None = None, linked: bool = False
    ) -> None:
        self.held.append(("signed_in", request.state.db_session.in_transaction()))
        await super().signed_in(request, user, method, provider=provider, linked=linked)

    async def sign_in_failed(
        self,
        request: Request,
        method: str,
        reason: str,
        *,
        user: User | None = None,
        username: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.held.append((reason, request.state.db_session.in_transaction()))
        await super().sign_in_failed(request, method, reason, user=user, username=username, provider=provider)


def _audit_entries(client: TestClient) -> dict[str, tuple[str, str]]:
    """Every entry in the app's audit log, oldest first, as ``(action, entity_type)`` by id."""

    async def read() -> dict[str, tuple[str, str]]:
        async with api_runtime(client).session_factory() as session:
            rows = await session.execute(text("SELECT id, action, entity_type FROM audit_log ORDER BY created_at, id"))
            return {row.id: (row.action, row.entity_type) for row in rows}

    return on_app_loop(client, read)


@contextmanager
def recorded(client: TestClient) -> Iterator[list[tuple[str, str]]]:
    """The entries the app's audit log gains while the block runs, as ``(action, entity_type)``, filled in at its end."""
    before = _audit_entries(client)
    added: list[tuple[str, str]] = []
    yield added
    added.extend(entry for entry_id, entry in _audit_entries(client).items() if entry_id not in before)


def stored_audit_text(client: TestClient, entity_type: str, entity_id: str) -> str:
    """What an entity's audit entries keep in their JSON columns, as text: where a leaked secret would show."""

    async def read() -> str:
        async with api_runtime(client).session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT coalesce(string_agg(CAST(changes AS text) || CAST(details AS text), ''), '') "
                    "FROM audit_log WHERE entity_type = :type AND entity_id = :id"
                ),
                {"type": entity_type, "id": entity_id},
            )
            return str(result.scalar_one())

    return on_app_loop(client, read)


def history(
    client: TestClient, entity_type: str, entity_id: str, headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """An entity's audit entries as its History tab reads them, oldest first."""
    response = client.get(f"/api/v1/audit/{entity_type}/{entity_id}", params={"limit": 200}, headers=headers)
    assert response.status_code == 200, response.text
    return list(reversed(response.json()["entries"]))


@pytest.fixture
def hooks(app_with_lifespan: FastAPI, monkeypatch: pytest.MonkeyPatch) -> HookRecorder:
    """The app's audit log, swapped for one that keeps each hook call."""
    return HookRecorder.install(app_with_lifespan, monkeypatch)


# --- Endpoints -----------------------------------------------------------------------------------


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

    def test_login_refuses_a_json_body_sent_without_a_content_type(
        self,
        async_client: TestClient,
        test_settings,
    ) -> None:
        """FastAPI reads a body as JSON only under a JSON Content-Type; without one the body is not a login."""
        response = async_client.post(
            "/api/v1/auth/login",
            content=json.dumps({"username": test_settings.auth_username, "password": test_settings.auth_password}),
        )

        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body"]]

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


class TestSignInHooks:
    """Each password or invite-link sign-in, and each refusal with its reason, is told to the audit log; the answers
    stay as they were."""

    def test_each_login_refusal_is_told_with_its_reason(
        self, authenticated_client: TestClient, hooks: HookRecorder
    ) -> None:
        client = authenticated_client
        name = f"hooked-{uuid4().hex[:8]}"
        invited, disabled, stranger = f"{name}-invited", f"{name}-off", f"{name}-nobody"
        assert client.post("/api/v1/users", json={"username": name, "password": "right-pass-1"}).status_code == 201
        assert client.post("/api/v1/users/invite", json={"username": invited}).status_code == 201
        off = client.post("/api/v1/users", json={"username": disabled, "password": "right-pass-1"}).json()
        assert client.patch(f"/api/v1/users/{off['id']}", json={"is_active": False}).status_code == 200

        attempts = [
            (stranger, "right-pass-1"),
            (invited, "anything"),
            (name, "wrong-pass"),
            # The password is checked before the account, as before.
            (disabled, "wrong-pass"),
            (disabled, "right-pass-1"),
            (name, "right-pass-1"),
        ]
        responses = [client.post("/api/v1/auth/login", json={"username": u, "password": p}) for u, p in attempts]

        assert [(r.status_code, r.json().get("detail")) for r in responses] == [
            (401, "Invalid username or password"),
            (401, "Invalid username or password"),
            (401, "Invalid username or password"),
            (401, "Invalid username or password"),
            (401, "Account is disabled"),
            (200, None),
        ]
        assert [(call["method"], call["reason"], call["user"], call["username"]) for _hook, call in hooks.calls[:-1]] == [
            ("password", "unknown user", None, stranger),
            ("password", "no password set", invited, None),
            ("password", "wrong password", name, None),
            ("password", "wrong password", disabled, None),
            ("password", "account disabled", disabled, None),
        ]
        assert hooks.calls[-1] == ("signed_in", {"user": name, "method": "password", "provider": None, "linked": False})

    def test_an_invite_link_signs_in_or_is_refused_with_its_reason(
        self, authenticated_client: TestClient, hooks: HookRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = authenticated_client
        name = f"linked-{uuid4().hex[:8]}"

        def invite(username: str) -> tuple[str, str]:
            invited = client.post("/api/v1/users/invite", json={"username": username}).json()
            return invited["user"]["id"], invited["invite_url"].split("/set-password/")[1]

        _late, late_link = invite(f"{name}-late")
        off, off_link = invite(f"{name}-off")
        assert client.patch(f"/api/v1/users/{off}", json={"is_active": False}).status_code == 200
        _user, link = invite(name)

        unknown = client.post("/api/v1/auth/set-password", json={"token": "no-such-link", "password": "pass12345"})
        disabled = client.post("/api/v1/auth/set-password", json={"token": off_link, "password": "pass12345"})
        with monkeypatch.context() as later:
            # A day past the link's expiry, as the route reads the clock.
            later.setattr(api.routes.auth, "utc_now", lambda: utc_now() + timedelta(days=8))
            expired = client.post("/api/v1/auth/set-password", json={"token": late_link, "password": "pass12345"})
        accepted = client.post("/api/v1/auth/set-password", json={"token": link, "password": "pass12345"})

        assert [(r.status_code, r.json().get("detail")) for r in (unknown, disabled, expired, accepted)] == [
            (404, "Invalid invite token"),
            (400, "Account is disabled"),
            (400, "Invite token has expired"),
            (200, None),
        ]
        assert [(call["method"], call["reason"], call["user"]) for _hook, call in hooks.calls[:-1]] == [
            ("invite", "unknown link", None),
            ("invite", "account disabled", f"{name}-off"),
            ("invite", "expired link", f"{name}-late"),
        ]
        assert hooks.calls[-1] == ("signed_in", {"user": name, "method": "invite", "provider": None, "linked": False})


class TestTheRequestsSession:
    """A sign-in hook runs once the request's session holds no transaction, so a hook that opens a session of its own
    never waits for a second connection while the request keeps one."""

    def test_holds_no_transaction_when_a_login_or_invite_hook_runs(
        self,
        authenticated_client: TestClient,
        app_with_lifespan: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
        test_settings: Settings,
    ) -> None:
        client = authenticated_client
        invited = client.post("/api/v1/users/invite", json={"username": f"held-{uuid4().hex[:8]}"}).json()
        link = invited["invite_url"].split("/set-password/")[1]
        admin = {"username": test_settings.auth_username, "password": test_settings.auth_password}
        watch = SessionWatch.install(app_with_lifespan, monkeypatch)

        assert client.post("/api/v1/auth/login", json={"username": "nobody-here", "password": "x"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={**admin, "password": "wrong"}).status_code == 401
        assert client.post("/api/v1/auth/login", json=admin).status_code == 200
        assert client.post(
            "/api/v1/auth/set-password", json={"token": "no-such-link", "password": "pass12345"}
        ).status_code == 404
        assert client.post("/api/v1/auth/set-password", json={"token": link, "password": "pass12345"}).status_code == 200

        assert watch.held == [
            ("unknown user", False),
            ("wrong password", False),
            ("signed_in", False),
            ("unknown link", False),
            ("signed_in", False),
        ]
        client.request("DELETE", f"/api/v1/users/{invited['user']['id']}")


class TestFreeEdition:
    """A free install records no sign-ins: a login adds nothing to the audit log, an accepted invite only the change
    it makes to the user."""

    def test_sign_ins_add_no_entries(self, authenticated_client: TestClient, test_settings: Settings) -> None:
        client = authenticated_client
        invited = client.post("/api/v1/users/invite", json={"username": f"free-{uuid4().hex[:8]}"}).json()
        link = invited["invite_url"].split("/set-password/")[1]
        admin = {"username": test_settings.auth_username, "password": test_settings.auth_password}

        with recorded(client) as by_logins:
            assert client.post("/api/v1/auth/login", json=admin).status_code == 200
            assert client.post("/api/v1/auth/login", json={**admin, "password": "wrong"}).status_code == 401
            long_name = client.post("/api/v1/auth/login", json={"username": "x" * 10_000, "password": "x"})
            assert (long_name.status_code, long_name.json()["detail"]) == (401, "Invalid username or password")
            assert client.post(
                "/api/v1/auth/set-password", json={"token": "no-such-link", "password": "pass12345"}
            ).status_code == 404
        with recorded(client) as by_acceptance:
            assert client.post("/api/v1/auth/set-password", json={"token": link, "password": "pass12345"}).status_code == 200

        assert by_logins == []
        assert by_acceptance == [("updated", "user")]
