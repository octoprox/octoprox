# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the admin backup (export/import) endpoints, on an api process.

An import replaces every row in one transaction, recomputes the metric totals from the history it restored and appends
one ``all`` entry, and the answering process reads the imported definitions back at once.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from api.core.config import Settings
from api.core.roles import Role
from api.core.stats import MetricDelta
from api.db.redis import (
    FAILURE_LOG_APPENDED_KEY,
    MITM_REQUESTS_KEY,
    PROJECT_WINDOWS,
    PROXY_WINDOWS,
    EntityWindows,
)
from api.db.repository import (
    MetricsRepository,
    ProviderDescriptorRepository,
)
from api.db.streams import ChangeKind
from api.models.provider import ProviderRecord
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.routes.auth import create_jwt
from tests.conftest import api_runtime, on_app_loop
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.routes.test_providers import Unheard, announced_since, stream_head

PASSPHRASE = "migrate-me-please"


def _export(
    client: TestClient, include_metrics: bool = False, include_database_files: bool = False
) -> bytes:
    resp = client.post(
        "/api/v1/backup/export",
        json={
            "passphrase": PASSPHRASE,
            "include_metrics": include_metrics,
            "include_database_files": include_database_files,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment")
    return resp.content


def _import(
    client: TestClient,
    file_bytes: bytes,
    passphrase: str = PASSPHRASE,
    keep_current_user: bool | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = {"passphrase": passphrase, "mode": "replace"}
    if keep_current_user is not None:
        data["keep_current_user"] = "true" if keep_current_user else "false"
    return client.post(
        "/api/v1/backup/import",
        files={"file": ("backup.opbak", file_bytes, "application/octet-stream")},
        data=data,
        headers=headers,
    )


def _jwt_headers(settings: Settings, user_id: str, username: str) -> dict[str, str]:
    token = create_jwt(
        payload={"sub": username, "user_id": user_id, "role": "admin"},
        secret=settings.jwt_secret,
        expiry_hours=settings.jwt_expiry_hours,
    )
    return {"Authorization": f"Bearer {token}"}


def _project_ids(client: TestClient) -> list[str]:
    return [p["id"] for p in client.get("/api/v1/projects").json()["projects"]]


def _in_session[T](client: TestClient, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """``work`` in one committed transaction of the app's Postgres, on the app's loop."""

    async def run() -> T:
        async with api_runtime(client).session_factory() as session:
            result = await work(session)
            await session.commit()
            return result

    return on_app_loop(client, run)


def _add_proxy(client: TestClient, connector_id: str) -> Proxy:
    """A healthy proxy stored as a job stores one (the proxies routes are another module's)."""
    proxy = Proxy(
        host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
        connector_id=connector_id, status=ProxyStatus.HEALTHY,
    )
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)
    return proxy


@pytest.fixture
def stored_proxy(authenticated_client: TestClient, created_connector: dict[str, Any]) -> Proxy:
    return _add_proxy(authenticated_client, created_connector["id"])


class TestBackupAccessControl:
    def test_editor_cannot_export(self, editor_client: TestClient) -> None:
        resp = editor_client.post(
            "/api/v1/backup/export", json={"passphrase": PASSPHRASE}
        )
        assert resp.status_code == 403

    def test_viewer_cannot_export(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            "/api/v1/backup/export", json={"passphrase": PASSPHRASE}
        )
        assert resp.status_code == 403

    def test_editor_cannot_import(self, editor_client: TestClient) -> None:
        resp = _import(editor_client, b"whatever")
        assert resp.status_code == 403

    def test_unauthenticated_cannot_export(self, async_client: TestClient) -> None:
        resp = async_client.post(
            "/api/v1/backup/export", json={"passphrase": PASSPHRASE}
        )
        assert resp.status_code == 401


class TestExportValidation:
    def test_short_passphrase_rejected(self, authenticated_client: TestClient) -> None:
        resp = authenticated_client.post(
            "/api/v1/backup/export", json={"passphrase": "short"}
        )
        assert resp.status_code == 422  # min_length on the pydantic model


class TestBackupRoundTrip:
    def test_export_import_replaces_all_data(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        stored_proxy: Proxy,
    ) -> None:
        original_project_id = created_project["id"]

        # Snapshot the current setup.
        file_bytes = _export(authenticated_client)

        # Create an extra project that must be wiped by the replace-import.
        unique = uuid.uuid4().hex[:8]
        extra = authenticated_client.post(
            "/api/v1/projects",
            json={
                "name": f"Throwaway {unique}",
                "description": "should be wiped",
                "username": f"throwaway_{unique}",
                "password": "throwaway123",
                "routing_strategy": "round_robin",
            },
        ).json()
        extra_id = extra["id"]
        assert extra_id in _project_ids(authenticated_client)

        # Import the snapshot back.
        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text
        summary = resp.json()
        assert summary["projects"] >= 1
        assert summary["proxies"] >= 1

        ids = _project_ids(authenticated_client)
        assert original_project_id in ids  # restored
        assert extra_id not in ids  # wiped

    def test_round_trip_with_metrics_flag(
        self,
        authenticated_client: TestClient,
        stored_proxy: Proxy,
    ) -> None:
        file_bytes = _export(authenticated_client, include_metrics=True)
        envelope = json.loads(file_bytes)
        assert envelope["includes_metrics"] is True
        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text

    def test_import_rebuilds_the_running_totals_from_the_imported_history(
        self, authenticated_client: TestClient, created_project: dict[str, Any], stored_proxy: Proxy
    ) -> None:
        """Counters read the totals: after an import they are the sums of the history the file brought."""
        project_id = created_project["id"]
        window = MetricDelta(
            request_count=4, success_count=3, failure_count=1, latency_sum_ms=400.0, bytes_sent=100, bytes_received=1000
        )

        async def flushed(session: AsyncSession) -> None:
            """One window as a worker's metrics flush writes it: history and totals in one transaction."""
            repository = MetricsRepository(session)
            await repository.save_project_metrics_snapshots({project_id: window})
            await repository.save_proxy_metrics_snapshots({stored_proxy.id: window}, {stored_proxy.id: "healthy"})
            await repository.upsert_totals(PROJECT_WINDOWS, {project_id: window})
            await repository.upsert_totals(PROXY_WINDOWS, {stored_proxy.id: window})

        def totals(kind: EntityWindows, entity_id: str) -> MetricDelta | None:
            found = _in_session(authenticated_client, lambda s: MetricsRepository(s).get_totals(kind, [entity_id]))
            return found.get(entity_id)

        _in_session(authenticated_client, flushed)
        _in_session(authenticated_client, flushed)
        file_bytes = _export(authenticated_client, include_metrics=True)
        # Traffic after the export: in the history and the totals, not in the file.
        _in_session(authenticated_client, flushed)
        project_totals = totals(PROJECT_WINDOWS, project_id)
        assert project_totals is not None and project_totals.request_count == 12

        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_metrics"] >= 2
        kinds: tuple[tuple[EntityWindows, str], ...] = ((PROJECT_WINDOWS, project_id), (PROXY_WINDOWS, stored_proxy.id))
        for kind, entity_id in kinds:
            restored = totals(kind, entity_id)
            assert restored is not None, kind
            assert (restored.request_count, restored.success_count, restored.failure_count) == (8, 6, 2), kind
            assert (restored.bytes_sent, restored.bytes_received) == (200, 2000), kind

        # A file without metrics brings no history, so nothing is counted.
        assert _import(authenticated_client, _export(authenticated_client)).status_code == 200
        assert totals(PROJECT_WINDOWS, project_id) is None and totals(PROXY_WINDOWS, stored_proxy.id) is None

    def test_an_import_appends_one_all_entry(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        """Anything may have changed: every process reads every definition again, from one entry."""
        file_bytes = _export(authenticated_client)
        after = stream_head(authenticated_client)

        assert _import(authenticated_client, file_bytes).status_code == 200
        # Refused before it wrote anything: nothing announced.
        assert _import(authenticated_client, file_bytes, passphrase="wrong-passphrase").status_code == 400

        assert announced_since(authenticated_client, after) == [("all", "")]

    def test_only_the_removed_projects_lose_their_redis_keys(
        self, authenticated_client: TestClient, created_project: dict[str, Any], sample_project_data: dict[str, Any]
    ) -> None:
        file_bytes = _export(authenticated_client)
        removed = authenticated_client.post(
            "/api/v1/projects",
            json={
                **sample_project_data,
                "name": f"{sample_project_data['name']} gone",
                "username": f"{sample_project_data['username']}gone",
            },
        ).json()
        redis = api_runtime(authenticated_client).redis_client.client
        kept_capture = MITM_REQUESTS_KEY.format(project_id=created_project["id"])
        removed_keys = [
            MITM_REQUESTS_KEY.format(project_id=removed["id"]),
            FAILURE_LOG_APPENDED_KEY.format(project_id=removed["id"]),
        ]
        on_app_loop(authenticated_client, redis.xadd, kept_capture, {"method": "GET"})
        on_app_loop(authenticated_client, redis.xadd, removed_keys[0], {"method": "GET"})
        on_app_loop(authenticated_client, redis.hset, removed_keys[1], "upstream_timeout", 1)

        assert _import(authenticated_client, file_bytes).status_code == 200
        # The project the file carries keeps its captures; the one it removed leaves nothing behind.
        assert on_app_loop(authenticated_client, redis.exists, kept_capture) == 1
        assert on_app_loop(authenticated_client, redis.exists, *removed_keys) == 0
        on_app_loop(authenticated_client, redis.delete, kept_capture)

    def test_attribution_settings_and_databases_round_trip(
        self, authenticated_client: TestClient, created_project: dict[str, Any], tmp_path: Path
    ) -> None:
        """Geo settings and database rows always travel, the file bytes only on request, as the envelope records.

        A restore leaves attribution configured as it was, the database record present and, with its bytes, readable
        again without a re-upload.
        """
        def put_retention(days: int) -> None:
            body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
            body["exit_ip_retention_days"] = days
            assert authenticated_client.put("/api/v1/geo/settings", json=body).status_code == 200

        put_retention(123)
        path = write_mmdb(tmp_path / "GeoLite2-Country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        with path.open("rb") as handle:
            resp = authenticated_client.post(
                "/api/v1/geo/databases",
                files={"file": (path.name, handle, "application/octet-stream")},
                data={"name": "backup-round-trip"},
            )
        assert resp.status_code == 201, resp.text
        db_id = resp.json()["id"]

        # Other tests may leave URL-sourced records with no file; only rows with
        # a file carry a blob.
        listed_before = authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        with_file_before = sum(1 for d in listed_before if d["source"] != "path" and d["has_file"])

        without = json.loads(_export(authenticated_client))
        assert without["includes_database_files"] is False
        with_files = json.loads(_export(authenticated_client, include_database_files=True))
        assert with_files["includes_database_files"] is True

        # Drift after the export, then restore and expect the exported state back.
        put_retention(7)
        assert authenticated_client.delete(f"/api/v1/geo/databases/{db_id}").status_code == 204
        resp = _import(authenticated_client, json.dumps(with_files).encode("utf-8"))
        assert resp.status_code == 200, resp.text
        summary = resp.json()
        assert summary["geo_settings"] == 1
        assert summary["geo_databases"] >= 1
        assert summary["geo_database_blobs"] == with_file_before

        settings = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
        assert settings["exit_ip_retention_days"] == 123
        listed = {d["id"]: d for d in authenticated_client.get("/api/v1/geo/databases").json()["databases"]}
        assert db_id in listed
        assert listed[db_id]["has_file"] is True
        # The restored file is open again: the sample range resolves.
        resp = authenticated_client.post("/api/v1/geo/lookup", json={"ip": "81.2.69.142"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["resolution"]["resolved_country"] == MAXMIND_RECORD["country"]["iso_code"]

        authenticated_client.delete(f"/api/v1/geo/databases/{db_id}")

    def test_import_without_database_files_keeps_rows_but_not_bytes(
        self, authenticated_client: TestClient, created_project: dict[str, Any], tmp_path: Path
    ) -> None:
        path = write_mmdb(tmp_path / "GeoLite2-Country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        with path.open("rb") as handle:
            resp = authenticated_client.post(
                "/api/v1/geo/databases",
                files={"file": (path.name, handle, "application/octet-stream")},
                data={"name": "rows-only"},
            )
        assert resp.status_code == 201, resp.text
        db_id = resp.json()["id"]
        file_bytes = _export(authenticated_client)
        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text
        assert resp.json()["geo_database_blobs"] == 0
        listed = {d["id"]: d for d in authenticated_client.get("/api/v1/geo/databases").json()["databases"]}
        assert db_id in listed
        # The record survived, so the admin sees what to re-upload or refresh.
        assert listed[db_id]["has_file"] is False
        authenticated_client.delete(f"/api/v1/geo/databases/{db_id}")


class TestImportedDefinitionsHere:
    def test_the_answering_process_reads_them_back_at_once(
        self, build_app: Callable[..., FastAPI], auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Other processes follow the import's entry; this one has the imported setup before it answers."""
        # Its writes are announced where no refresher hears them, and the
        # hourly reconcile is far off: only the import itself can bring them in.
        unheard = Unheard()
        for route in ("backup", "geo"):
            monkeypatch.setattr(f"api.routes.{route}.change_log_of", lambda request: unheard)
        app = build_app(Role.API, reconcile_interval=3600)
        with TestClient(app, headers=auth_headers) as client:
            runtime = api_runtime(client)
            spec = {**TestProviderDescriptorsInBackup.ACME, "id": "acme_here", "name": "Acme Here"}
            record = ProviderRecord(id="acme_here", name="Acme Here", spec=spec, created_by="testadmin")
            _in_session(client, lambda s: ProviderDescriptorRepository(s).create(record))
            settings = client.get("/api/v1/geo/settings").json()["settings"]
            file_bytes = _export(client)
            try:
                _in_session(client, lambda s: ProviderDescriptorRepository(s).delete("acme_here"))
                assert client.put(
                    "/api/v1/geo/settings", json={**settings, "observation_retention_days": 41}
                ).status_code == 200
                assert runtime.provider_registry.get("acme_here") is None

                assert _import(client, file_bytes).status_code == 200
                assert unheard.entries[-1] == (ChangeKind.ALL, "")
                assert runtime.provider_registry.get("acme_here") is not None
                # This process's copy, not a GET: the settings routes read the row themselves.
                restored = runtime.geo_settings_store.settings.observation_retention_days
                assert restored == settings["observation_retention_days"]
            finally:
                _in_session(client, lambda s: ProviderDescriptorRepository(s).delete("acme_here"))
                on_app_loop(client, runtime.provider_store.sync_all)

    def test_a_read_back_cut_by_a_deadline_is_logged_by_its_type(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The import stands, and a bare TimeoutError, which has no text, is named by its type."""
        file_bytes = _export(authenticated_client)
        runtime = api_runtime(authenticated_client)
        monkeypatch.setattr(runtime.provider_store, "sync_all", AsyncMock(side_effect=TimeoutError))

        with patch("api.routes.backup.logger") as log:
            assert _import(authenticated_client, file_bytes).status_code == 200

        log.warning.assert_called_once_with(
            "Imported definitions not read yet, left to the refresher", error="TimeoutError"
        )


class TestImportErrors:
    def test_wrong_passphrase_returns_400(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        file_bytes = _export(authenticated_client)
        resp = _import(authenticated_client, file_bytes, passphrase="wrong-passphrase")
        assert resp.status_code == 400
        assert "passphrase" in resp.json()["detail"].lower()

    def test_incompatible_schema_rejected_before_decryption(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        file_bytes = _export(authenticated_client)
        envelope = json.loads(file_bytes)
        envelope["schema_version"] = "000_bogus_revision"
        tampered = json.dumps(envelope).encode("utf-8")

        # Even with the CORRECT passphrase this must fail on the schema gate,
        # proving compatibility is checked before decryption.
        resp = _import(authenticated_client, tampered, passphrase=PASSPHRASE)
        assert resp.status_code == 400
        assert "schema" in resp.json()["detail"].lower()

    def test_not_a_backup_file_returns_400(
        self, authenticated_client: TestClient
    ) -> None:
        resp = _import(authenticated_client, b"definitely not json", passphrase=PASSPHRASE)
        assert resp.status_code == 400

    def test_short_passphrase_and_unknown_mode_return_400(self, authenticated_client: TestClient) -> None:
        assert _import(authenticated_client, b"{}", passphrase="short").status_code == 400
        resp = authenticated_client.post(
            "/api/v1/backup/import",
            files={"file": ("backup.opbak", b"{}", "application/octet-stream")},
            data={"passphrase": PASSPHRASE, "mode": "merge"},
        )
        assert resp.status_code == 400 and "mode" in resp.json()["detail"]


class TestKeepCurrentUser:
    def test_keep_current_user_preserves_account_and_renames_clash(
        self,
        authenticated_client: TestClient,
        test_settings: Settings,
        created_project: dict[str, Any],
    ) -> None:
        # Create a real admin in the DB and act as them (the default fixture's
        # JWT points at an id that has no DB row).
        unique = uuid.uuid4().hex[:8]
        username = f"keepme_{unique}"
        email = f"{username}@example.com"
        me = authenticated_client.post(
            "/api/v1/users",
            json={
                "username": username,
                "email": email,
                "password": "keepme-pass-123",
                "role": "admin",
            },
        ).json()
        headers = _jwt_headers(test_settings, me["id"], username)

        # The backup contains this same user (same id, username and email).
        file_bytes = _export(authenticated_client)

        resp = _import(
            authenticated_client, file_bytes, keep_current_user=True, headers=headers
        )
        assert resp.status_code == 200, resp.text
        summary = resp.json()
        assert summary["kept_current_user"] is True
        clash = [c for c in summary["user_conflicts"] if c["original_username"] == username]
        assert clash == [
            {
                "original_username": username,
                "new_username": f"{username}-imported",
                "new_id": True,
                "email_cleared": True,
            }
        ]

        # We are still who we were, with the same id, and still signed in.
        who = authenticated_client.get("/api/v1/users/me", headers=headers)
        assert who.status_code == 200, who.text
        assert who.json()["id"] == me["id"]
        assert who.json()["username"] == username
        assert who.json()["email"] == email

        # Both the kept user and the renamed imported copy exist.
        users = authenticated_client.get("/api/v1/users", headers=headers).json()
        usernames = {u["username"] for u in users["users"]}
        assert {username, f"{username}-imported"} <= usernames
        imported = next(u for u in users["users"] if u["username"] == f"{username}-imported")
        assert imported["id"] != me["id"]
        assert imported["email"] == ""

        # Everything else was still replaced from the backup.
        assert created_project["id"] in _project_ids(authenticated_client)

    def test_keep_current_user_without_db_row_returns_400(
        self,
        authenticated_client: TestClient,
        test_settings: Settings,
        created_project: dict[str, Any],
    ) -> None:
        file_bytes = _export(authenticated_client)
        headers = _jwt_headers(test_settings, str(uuid.uuid4()), "ghost-admin")
        resp = _import(
            authenticated_client, file_bytes, keep_current_user=True, headers=headers
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()
        # Nothing was wiped: the transaction rolled back.
        assert created_project["id"] in _project_ids(authenticated_client)

    def test_default_does_not_keep_current_user(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        file_bytes = _export(authenticated_client)
        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text
        assert resp.json()["kept_current_user"] is False
        assert resp.json()["user_conflicts"] == []


class TestProviderDescriptorsInBackup:
    """Admin-authored providers travel with the backup and are live right after import."""

    ACME = {
        "id": "acme_backup",
        "name": "Acme Backup",
        "credential_fields": [{"key": "username", "label": "User", "required": True}, {"key": "password", "label": "Pass", "type": "password", "secret": True, "required": True}],
        "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number", "default": 1, "min": 1}],
        "proxy_types": [{"key": "res", "label": "Res", "mode": "session", "host": "gw.acme.test", "port": 9000, "username": "{credential.username}-{session_id}", "password": "{credential.password}"}],
    }

    def test_custom_provider_round_trips(self, authenticated_client: TestClient) -> None:
        runtime = api_runtime(authenticated_client)
        record = ProviderRecord(id="acme_backup", name="Acme Backup", spec=self.ACME, created_by="testadmin")
        _in_session(authenticated_client, lambda s: ProviderDescriptorRepository(s).create(record))
        on_app_loop(authenticated_client, runtime.provider_store.sync_all)
        assert runtime.provider_registry.get("acme_backup") is not None
        try:
            file_bytes = _export(authenticated_client)

            _in_session(authenticated_client, lambda s: ProviderDescriptorRepository(s).delete("acme_backup"))
            on_app_loop(authenticated_client, runtime.provider_store.sync_all)
            assert runtime.provider_registry.get("acme_backup") is None

            resp = _import(authenticated_client, file_bytes)
            assert resp.status_code == 200, resp.text
            assert resp.json()["provider_descriptors"] >= 1

            # Registered without a restart: the import read the restored rows back.
            provider = runtime.provider_registry.get("acme_backup")
            assert provider is not None and provider.name == "Acme Backup"
        finally:
            _in_session(authenticated_client, lambda s: ProviderDescriptorRepository(s).delete("acme_backup"))
            on_app_loop(authenticated_client, runtime.provider_store.sync_all)
