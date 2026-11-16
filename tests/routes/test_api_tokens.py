# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Personal access tokens: minted once, hashed at rest, honoured until revoked, and told to the audit log."""

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session
from starlette.testclient import TestClient

import api.core.auth
import api.models.auth
from api.core import utc_now
from api.core.api_tokens import TOKEN_PREFIX
from api.db.auth_repository import ApiTokenRepository
from api.db.repository import UserRepository
from api.models.auth import ApiToken, TokenUsage
from api.models.user import User
from tests.conftest import on_app_loop
from tests.routes.test_auth import HookRecorder, history, recorded

# An ORM statement as its verb and the tables of its entities, such as ("SELECT", ("api_tokens", "users")).
Statement = tuple[str, tuple[str, ...]]


def _login(client: TestClient, settings: Any) -> dict[str, str]:
    """A session JWT for the seeded admin: the user the fixtures' JWTs name does not exist in the DB."""
    response = client.post(
        "/api/v1/auth/login", json={"username": settings.auth_username, "password": settings.auth_password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@contextmanager
def statements_by_session() -> Iterator[defaultdict[Session, list[Statement]]]:
    """Each ORM statement any session runs while the block runs, by the session that ran it."""
    seen: defaultdict[Session, list[Statement]] = defaultdict(list)

    def record(state: ORMExecuteState) -> None:
        tables = tuple(sorted(mapper.class_.__tablename__ for mapper in state.all_mappers))
        seen[state.session].append((str(state.statement).split(None, 1)[0], tables))

    event.listen(Session, "do_orm_execute", record)
    try:
        yield seen
    finally:
        event.remove(Session, "do_orm_execute", record)


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
        # No password, no email: it exists to hold tokens.
        user = authenticated_client.post(
            "/api/v1/users/service-accounts", json={"username": "scraper-bot", "role": "viewer"}
        ).json()
        created = authenticated_client.post(f"/api/v1/users/{user['id']}/tokens", json={"name": "bot"})
        assert created.status_code == 201, created.text
        secret = created.json()["token"]
        assert created.json()["api_token"]["created_by"] == "testadmin"

        token_headers = {"Authorization": f"Bearer {secret}"}
        me = authenticated_client.get("/api/v1/users/me", headers=token_headers).json()
        assert (me["username"], me["kind"]) == ("scraper-bot", "service")
        assert authenticated_client.get("/api/v1/projects", headers=token_headers).status_code == 200
        # The role is the user's, read on every call: a viewer cannot write.
        assert authenticated_client.post("/api/v1/projects", json={"name": "x"}, headers=token_headers).status_code == 403

        # Disabling the user ends the token at once.
        assert authenticated_client.patch(f"/api/v1/users/{user['id']}", json={"is_active": False}).status_code == 200
        disabled = authenticated_client.get("/api/v1/users/me", headers=token_headers)
        assert (disabled.status_code, disabled.json()["detail"]) == (401, "Account is disabled")
        assert authenticated_client.patch(f"/api/v1/users/{user['id']}", json={"is_active": True}).status_code == 200
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 200

        listed = authenticated_client.get(f"/api/v1/users/{user['id']}/tokens").json()["tokens"]
        assert len(listed) == 1
        assert authenticated_client.delete(f"/api/v1/users/{user['id']}/tokens/{listed[0]['id']}").status_code == 204
        assert authenticated_client.get("/api/v1/users/me", headers=token_headers).status_code == 401
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")

    def test_a_service_accounts_own_token_gives_it_no_email_or_password(self, authenticated_client: TestClient) -> None:
        """An email would let whoever holds that address at a sign-in provider sign in as the account."""
        account = authenticated_client.post(
            "/api/v1/users/service-accounts", json={"username": f"bot-{uuid4().hex[:8]}"}
        ).json()
        secret = authenticated_client.post(f"/api/v1/users/{account['id']}/tokens", json={"name": "bot"}).json()["token"]
        token_headers = {"Authorization": f"Bearer {secret}"}

        email = authenticated_client.patch("/api/v1/users/me", json={"email": "bot@example.com"}, headers=token_headers)
        password = authenticated_client.patch("/api/v1/users/me", json={"password": "sneaky-pass-1"}, headers=token_headers)

        assert [(r.status_code, r.json()["detail"]) for r in (email, password)] == [
            (400, "A service account has no email: nobody signs in as it"),
            (400, "A service account has no password: it uses API tokens"),
        ]
        stored = authenticated_client.get(f"/api/v1/users/{account['id']}").json()
        assert (stored["email"], stored["has_password"]) == ("", False)
        # Its theme is no way in.
        themed = authenticated_client.patch("/api/v1/users/me", json={"theme_preference": "dark"}, headers=token_headers)
        assert themed.status_code == 200
        authenticated_client.request("DELETE", f"/api/v1/users/{account['id']}")

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


@pytest.fixture
def hooks(app_with_lifespan: FastAPI, monkeypatch: pytest.MonkeyPatch) -> HookRecorder:
    """The app's audit log, swapped for one that keeps each hook call."""
    return HookRecorder.install(app_with_lifespan, monkeypatch)


class TestHooks:
    """Issuing, first using and revoking a token are told to the audit log; the issue and revoke in their request's
    transaction."""

    def test_issue_first_use_and_revoke_are_told(
        self, async_client: TestClient, test_settings: Any, hooks: HookRecorder
    ) -> None:
        headers = _login(async_client, test_settings)
        created = async_client.post("/api/v1/users/me/tokens", json={"name": "hooked"}, headers=headers).json()
        token_headers = {"Authorization": f"Bearer {created['token']}"}

        # The status check is a first use like any other call.
        assert async_client.get("/api/v1/auth/status", headers=token_headers).json()["authenticated"] is True
        assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
        revoked = async_client.delete(f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=headers)
        assert revoked.status_code == 204

        user = test_settings.auth_username
        assert hooks.told("token_issued", "token_first_used", "token_revoked") == [
            ("token_issued", {"by": user, "token": "hooked", "stored": True}),
            ("token_first_used", {"token": "hooked", "user": user}),
            ("token_revoked", {"by": user, "token": "hooked", "stored": False}),
        ]

    def test_a_first_use_two_calls_make_together_is_stamped_and_told_once(
        self, async_client: TestClient, test_settings: Any, hooks: HookRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = _login(async_client, test_settings)
        secret = async_client.post("/api/v1/users/me/tokens", json={"name": "racing"}, headers=headers).json()["token"]
        read = ApiTokenRepository.get_with_user
        both_read = asyncio.Barrier(2)

        async def read_together(self: ApiTokenRepository, token_hash: str) -> tuple[ApiToken, User] | None:
            """Each call reads the token before either stamps it."""
            found = await read(self, token_hash)
            await asyncio.wait_for(both_read.wait(), timeout=10)
            return found

        async def two_calls() -> list[int]:
            transport = httpx.ASGITransport(app=async_client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                calls = [client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {secret}"}) for _ in range(2)]
                return [response.status_code for response in await asyncio.gather(*calls)]

        with monkeypatch.context() as patched:
            patched.setattr(ApiTokenRepository, "get_with_user", read_together)
            statuses = on_app_loop(async_client, two_calls)

        assert statuses == [200, 200]
        assert hooks.told("token_first_used") == [
            ("token_first_used", {"token": "racing", "user": test_settings.auth_username})
        ]
        [token] = async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"]
        assert token["last_used_at"] is not None
        async_client.delete(f"/api/v1/users/me/tokens/{token['id']}", headers=headers)

    def test_a_first_use_whose_call_fails_stays_stamped_and_is_told_once(
        self, async_client: TestClient, test_settings: Any, hooks: HookRecorder
    ) -> None:
        headers = _login(async_client, test_settings)
        secret = async_client.post("/api/v1/users/me/tokens", json={"name": "unlucky"}, headers=headers).json()["token"]
        token_headers = {"Authorization": f"Bearer {secret}"}

        # Authenticated, then refused: the request rolls back, its first use does not.
        assert async_client.get(f"/api/v1/projects/{uuid4()}", headers=token_headers).status_code == 404
        [token] = async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"]
        assert token["last_used_at"] is not None
        assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200

        assert hooks.told("token_first_used") == [
            ("token_first_used", {"token": "unlucky", "user": test_settings.auth_username})
        ]
        async_client.delete(f"/api/v1/users/me/tokens/{token['id']}", headers=headers)

    def test_revoking_an_unknown_token_is_404(self, authenticated_client: TestClient) -> None:
        user = authenticated_client.post("/api/v1/users/invite", json={"username": f"no-tokens-{uuid4().hex[:8]}"}).json()["user"]
        assert authenticated_client.delete(f"/api/v1/users/{user['id']}/tokens/nope").status_code == 404
        assert authenticated_client.delete("/api/v1/users/me/tokens/nope").status_code == 404
        authenticated_client.request("DELETE", f"/api/v1/users/{user['id']}")


class TestTheRequestsSession:
    """Token auth reads the token with its user in one statement and commits before the endpoint runs, so the endpoint
    finds the request's session holding no connection and a stamp never waits for another call's endpoint."""

    def test_auth_reads_once_and_leaves_no_transaction_to_the_endpoint(
        self, async_client: TestClient, test_settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = _login(async_client, test_settings)
        created = async_client.post("/api/v1/users/me/tokens", json={"name": "lean"}, headers=headers).json()
        token_headers = {"Authorization": f"Bearer {created['token']}"}
        read = UserRepository.get_by_id
        at_endpoint: list[tuple[list[Statement], bool]] = []

        with statements_by_session() as statements, monkeypatch.context() as patched:

            async def profile_read(self: UserRepository, user_id: str) -> User | None:
                """The first statement of /users/me: notes what auth ran on the session and whether it holds a
                transaction."""
                session = self._session
                at_endpoint.append((list(statements[session.sync_session]), session.in_transaction()))
                return await read(self, user_id)

            patched.setattr(UserRepository, "get_by_id", profile_read)
            # The first use, a call within the minute, and a call once the stamp is due.
            assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
            assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
            patched.setattr(api.core.auth, "utc_now", lambda: utc_now() + timedelta(minutes=2))
            assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200

        token_with_user, stamp = ("SELECT", ("api_tokens", "users")), ("UPDATE", ("api_tokens",))
        assert at_endpoint == [
            ([token_with_user, stamp], False),
            ([token_with_user], False),
            ([token_with_user, stamp], False),
        ]
        async_client.delete(f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=headers)

    def test_a_due_stamp_waits_for_no_other_call_of_the_token(
        self, async_client: TestClient, test_settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = _login(async_client, test_settings)
        created = async_client.post("/api/v1/users/me/tokens", json={"name": "busy"}, headers=headers).json()
        token_headers = {"Authorization": f"Bearer {created['token']}"}
        assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
        usage = ApiTokenRepository.usage_by_user
        endpoint_entered, second_answered = asyncio.Event(), asyncio.Event()

        async def first_held(self: ApiTokenRepository, user_ids: Iterable[str] | None = None) -> dict[str, TokenUsage]:
            """The first call waits in its endpoint, past its auth, until the second call has its answer."""
            if not endpoint_entered.is_set():
                endpoint_entered.set()
                await asyncio.wait_for(second_answered.wait(), timeout=10)
            return await usage(self, user_ids)

        async def two_calls() -> list[int]:
            transport = httpx.ASGITransport(app=async_client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = asyncio.create_task(client.get("/api/v1/users/me", headers=token_headers))
                await asyncio.wait_for(endpoint_entered.wait(), timeout=10)
                try:
                    second = await asyncio.wait_for(client.get("/api/v1/users/me", headers=token_headers), timeout=5)
                finally:
                    second_answered.set()
                return [(await first).status_code, second.status_code]

        with monkeypatch.context() as patched:
            # Two minutes on, the first call stamps; the second finds that stamp, never a row the first still holds.
            patched.setattr(api.core.auth, "utc_now", lambda: utc_now() + timedelta(minutes=2))
            patched.setattr(ApiTokenRepository, "usage_by_user", first_held)
            assert on_app_loop(async_client, two_calls) == [200, 200]
        async_client.delete(f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=headers)

    def test_a_call_refused_after_its_auth_keeps_its_stamp(
        self, async_client: TestClient, test_settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = _login(async_client, test_settings)
        created = async_client.post("/api/v1/users/me/tokens", json={"name": "refused"}, headers=headers).json()
        token_headers = {"Authorization": f"Bearer {created['token']}"}
        assert async_client.get("/api/v1/users/me", headers=token_headers).status_code == 200
        later = utc_now() + timedelta(minutes=2)

        with monkeypatch.context() as patched:
            patched.setattr(api.core.auth, "utc_now", lambda: later)
            # Authenticated, then refused: the request rolls back, the stamp its auth committed does not.
            assert async_client.get(f"/api/v1/projects/{uuid4()}", headers=token_headers).status_code == 404

        tokens = async_client.get("/api/v1/users/me/tokens", headers=headers).json()["tokens"]
        [token] = [t for t in tokens if t["id"] == created["api_token"]["id"]]
        assert datetime.fromisoformat(token["last_used_at"]) == later
        async_client.delete(f"/api/v1/users/me/tokens/{token['id']}", headers=headers)


class TestAuditTrail:
    def test_a_change_made_with_a_token_names_the_token(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)
        name = f"via-token-{uuid4().hex[:8]}"
        user = async_client.post("/api/v1/users/invite", json={"username": name}, headers=headers).json()["user"]
        created = async_client.post(f"/api/v1/users/{user['id']}/tokens", json={"name": "ci-bot"}, headers=headers).json()

        edited = async_client.patch(
            "/api/v1/users/me", json={"email": f"{name}@example.com"}, headers={"Authorization": f"Bearer {created['token']}"}
        )

        assert edited.status_code == 200, edited.text
        entry = history(async_client, "user", user["id"], headers=headers)[-1]
        assert (entry["actor"], entry["details"]) == (
            name, {"auth_token": "ci-bot", "auth_token_id": created["api_token"]["id"]}
        )
        async_client.request("DELETE", f"/api/v1/users/{user['id']}", headers=headers)

    def test_tokens_add_no_entries_on_the_free_edition(self, async_client: TestClient, test_settings: Any) -> None:
        headers = _login(async_client, test_settings)

        with recorded(async_client) as added:
            created = async_client.post("/api/v1/users/me/tokens", json={"name": "free"}, headers=headers).json()
            token_headers = {"Authorization": f"Bearer {created['token']}"}
            assert async_client.get("/api/v1/projects", headers=token_headers).status_code == 200
            assert async_client.get("/api/v1/projects", headers=token_headers).status_code == 200
            assert async_client.delete(
                f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=token_headers
            ).status_code == 204

        assert added == []
