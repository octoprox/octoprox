# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the admin backup (export/import) endpoints."""

import json
import uuid
from typing import Any

from starlette.testclient import TestClient

from api.core.config import Settings
from api.routes.auth import create_jwt

PASSPHRASE = "migrate-me-please"


def _export(client: TestClient, include_metrics: bool = False) -> bytes:
    resp = client.post(
        "/api/v1/backup/export",
        json={"passphrase": PASSPHRASE, "include_metrics": include_metrics},
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
        created_proxy: dict[str, Any],
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
        created_proxy: dict[str, Any],
    ) -> None:
        file_bytes = _export(authenticated_client, include_metrics=True)
        envelope = json.loads(file_bytes)
        assert envelope["includes_metrics"] is True
        resp = _import(authenticated_client, file_bytes)
        assert resp.status_code == 200, resp.text


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
