# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log's endpoints on an api process: an entity's history, the retention, the edition and the extension seam.

Entries a test reads are stored for an entity id of its own, since the route tests' apps leave Postgres as they wrote
it; the system settings row is global, so each test starts and ends without one.
"""

import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
import structlog
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from api.core.config import Settings
from api.core.extension import Edition, Extension
from api.core.roles import Role
from api.db.audit_repository import AuditRepository, SystemSettingsRepository
from api.models.audit import AuditEntityType, AuditEntry
from api.models.system import SystemSettings
from tests.conftest import api_runtime, on_app_loop


@pytest.fixture(autouse=True)
def no_system_settings(migrated_database: str) -> Iterator[None]:
    """No ``system_settings`` row, nor its entries, before and after each test: the row is the install's one."""
    engine = create_engine(migrated_database)

    def clear() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM system_settings"))
            conn.execute(text("DELETE FROM audit_log WHERE entity_type = 'system_settings'"))

    clear()
    yield
    clear()
    engine.dispose()


def _store(client: TestClient, entries: Sequence[AuditEntry]) -> None:
    """``entries`` written straight to the app's Postgres, as a write would have recorded them."""

    async def add() -> None:
        async with api_runtime(client).session_factory() as session:
            await AuditRepository(session).add(entries)
            await session.commit()

    on_app_loop(client, add)


def _history(client: TestClient, entity_type: str, entity_id: str, **params: Any) -> Any:
    response = client.get(f"/api/v1/audit/{entity_type}/{entity_id}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestHistory:
    def test_is_newest_first_page_by_page(self, authenticated_client: TestClient) -> None:
        connector_id = str(uuid.uuid4())
        start = datetime(2026, 11, 1)
        _store(
            authenticated_client,
            [
                AuditEntry(
                    id=f"{connector_id[:30]}-{n}",
                    created_at=start + timedelta(minutes=n),
                    action="updated",
                    entity_type="connector",
                    entity_id=connector_id,
                    changes={"routing_config.weight": [n, n + 1]},
                )
                for n in range(5)
            ],
        )

        first = _history(authenticated_client, "connector", connector_id, limit=2)
        second = _history(authenticated_client, "connector", connector_id, limit=2, before=first["entries"][-1]["id"])
        last = _history(authenticated_client, "connector", connector_id, limit=2, before=second["entries"][-1]["id"])

        assert [[e["changes"]["routing_config.weight"][0] for e in page["entries"]] for page in (first, second, last)] == [
            [4, 3],
            [2, 1],
            [0],
        ]
        assert [page["more"] for page in (first, second, last)] == [True, True, False]
        assert set(first["entries"][0]) == {
            "id", "created_at", "action", "entity_type", "entity_id", "entity_name", "project_id", "actor_id",
            "actor", "request_id", "changes", "details",
        }

    def test_of_an_entity_never_recorded_is_empty(self, viewer_client: TestClient) -> None:
        assert _history(viewer_client, "proxy", str(uuid.uuid4())) == {"entries": [], "more": False}

    def test_of_a_projects_entity_is_open_to_a_viewer_and_of_the_installs_to_admins_only(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str], viewer_auth_headers: dict[str, str]
    ) -> None:
        statuses = {
            entity_type.value: tuple(
                authenticated_client.get(f"/api/v1/audit/{entity_type.value}/1", headers=headers).status_code
                for headers in ({}, editor_auth_headers, viewer_auth_headers)
            )
            for entity_type in AuditEntityType
        }

        project_scoped = {"project", "connector", "credential", "proxy", "wireguard_device", "openvpn_device"}
        assert statuses == {
            entity_type: (200, 200, 200) if entity_type in project_scoped else (200, 403, 403)
            for entity_type in statuses
        }
        assert len(statuses) == 15

    def test_refuses_an_unknown_type_a_page_size_out_of_bounds_and_a_caller_unknown(
        self, authenticated_client: TestClient
    ) -> None:
        assert authenticated_client.get("/api/v1/audit/spaceship/1").status_code == 422
        assert authenticated_client.get("/api/v1/audit/proxy/1", params={"limit": 0}).status_code == 422
        assert authenticated_client.get("/api/v1/audit/proxy/1", params={"limit": 201}).status_code == 422
        assert authenticated_client.get("/api/v1/audit/proxy/1", params={"limit": 200}).status_code == 200
        assert authenticated_client.get("/api/v1/audit/proxy/1", headers={"Authorization": ""}).status_code == 401


class TestRetention:
    def test_is_a_year_until_an_admin_sets_it(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/audit/retention").json() == {"days": 365}

    def test_a_change_is_stored_and_recorded_and_a_save_without_one_is_not(
        self, authenticated_client: TestClient
    ) -> None:
        response = authenticated_client.put("/api/v1/audit/retention", json={"days": 30})

        assert response.status_code == 200, response.text
        assert response.json() == {"days": 30}
        assert authenticated_client.get("/api/v1/audit/retention").json()["days"] == 30
        assert authenticated_client.put("/api/v1/audit/retention", json={"days": 30}).status_code == 200
        [entry] = _history(authenticated_client, "system_settings", "1")["entries"]
        assert {key: entry[key] for key in ("action", "entity_name", "actor_id", "actor", "changes", "details")} == {
            "action": "updated",
            "entity_name": "System settings",
            "actor_id": "test-admin-id",
            "actor": "testadmin",
            "changes": {"audit_retention_days": [365, 30]},
            "details": {},
        }
        assert entry["request_id"] == response.headers["x-request-id"]

    def test_a_save_records_only_the_retention(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A license key another request saves between this save's read and its write is not recorded as its change."""
        client = authenticated_client
        set_retention = SystemSettingsRepository.set_audit_retention

        async def licensed_meanwhile(repository: SystemSettingsRepository, days: int) -> SystemSettings:
            async with api_runtime(client).session_factory() as session:
                await SystemSettingsRepository(session).set_license_key("oxl1.another-request.sig")
                await session.commit()
            return await set_retention(repository, days)

        monkeypatch.setattr(SystemSettingsRepository, "set_audit_retention", licensed_meanwhile)

        assert client.put("/api/v1/audit/retention", json={"days": 30}).status_code == 200
        [entry] = _history(client, "system_settings", "1")["entries"]
        assert entry["changes"] == {"audit_retention_days": [365, 30]}

    def test_zero_keeps_entries_forever(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.put("/api/v1/audit/retention", json={"days": 0}).json()["days"] == 0

    def test_is_for_admins_and_a_whole_number_of_days(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str]
    ) -> None:
        assert authenticated_client.get("/api/v1/audit/retention", headers=editor_auth_headers).status_code == 403
        assert authenticated_client.put(
            "/api/v1/audit/retention", json={"days": 30}, headers=editor_auth_headers
        ).status_code == 403
        assert authenticated_client.put("/api/v1/audit/retention", json={"days": -1}).status_code == 422
        assert authenticated_client.put("/api/v1/audit/retention", json={"days": 36501}).status_code == 422

    def test_a_change_made_with_a_personal_access_token_says_so(
        self, async_client: TestClient, test_settings: Settings
    ) -> None:
        login = async_client.post(
            "/api/v1/auth/login", json={"username": test_settings.auth_username, "password": test_settings.auth_password}
        )
        session = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = async_client.post("/api/v1/users/me/tokens", json={"name": "ci"}, headers=session).json()
        token = {"Authorization": f"Bearer {created['token']}"}

        assert async_client.put("/api/v1/audit/retention", json={"days": 90}, headers=token).status_code == 200

        history = async_client.get("/api/v1/audit/system_settings/1", headers=session)
        [entry] = history.json()["entries"]
        assert entry["details"] == {"auth_token": "ci", "auth_token_id": created["api_token"]["id"]}
        assert entry["actor"] == test_settings.auth_username
        async_client.delete(f"/api/v1/users/me/tokens/{created['api_token']['id']}", headers=session)


class PingExtension(Extension):
    """An extension as an edition package ships one: a router of its own, and its edition."""

    name: ClassVar[str] = "ping"

    def routers(self) -> Sequence[APIRouter]:
        router = APIRouter(prefix="/ping")

        @router.get("")
        async def ping() -> dict[str, bool]:
            return {"pong": True}

        return (router,)

    async def edition(self, session: AsyncSession) -> Edition:
        return Edition(name=self.name, organisation="Acme Ltd")


class TestEditionAndExtension:
    def test_the_free_edition_answers_anyone(self, async_client: TestClient) -> None:
        assert async_client.get("/api/v1/edition").json() == {"name": "free", "organisation": None}

    def test_an_installed_extension_serves_its_routes_and_its_edition(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        with patch.object(Extension, "installed", return_value=PingExtension()), structlog.testing.capture_logs() as logs:
            app = build_app(Role.API)
        paths = [context.path for context in iter_route_contexts(app.routes)]

        with TestClient(app) as client:
            assert client.get("/api/v1/edition").json() == {"name": "ping", "organisation": "Acme Ltd"}
            assert client.get("/api/v1/ping").json() == {"pong": True}
        assert [log["edition"] for log in logs if log["event"] == "Edition loaded"] == ["ping"]
        if "/{full_path:path}" in paths:
            assert paths.index("/api/v1/ping") < paths.index("/{full_path:path}")

    @pytest.mark.parametrize("role", [Role.GATEWAY, Role.CONTROLLER, Role.WORKER])
    def test_a_process_without_the_api_never_loads_one(self, build_app: Callable[..., FastAPI], role: Role) -> None:
        with patch.object(Extension, "installed") as installed:
            app = build_app(role)

        installed.assert_not_called()
        assert not hasattr(app.state, "extension") and not hasattr(app.state, "audit_log")
