# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What's New (api.routes.releases): the notes up to the running version, what a user has not seen, and for admins a newer release."""

from __future__ import annotations

from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from api.core import utc_now
from api.core.config import Settings
from api.core.releases import LatestRelease, ReleaseNotes
from api.routes import releases
from tests.conftest import api_runtime, on_app_loop
from tests.core.test_releases import NOTES, note

ENDPOINT = "/api/v1/releases"
SEEN = "/api/v1/releases/seen"


@pytest.fixture(autouse=True)
def running_6_2_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app runs 6.2.0 and ships notes for 6.1.4, 6.2.0 and the 6.2.1 it does not run yet."""
    monkeypatch.setattr(releases, "__version__", "6.2.0")
    shipped = ReleaseNotes.parse(NOTES)
    monkeypatch.setattr(ReleaseNotes, "bundled", classmethod(lambda cls: shipped))


def store_finding(client: TestClient, *versions: str) -> None:
    """What a worker's update check would have stored an hour ago: octoprox.com listing ``versions``."""
    found = LatestRelease(checked_at=utc_now() - timedelta(hours=1), notes=[note(v) for v in versions])
    on_app_loop(client, found.save, api_runtime(client).redis_client)


def sign_in(client: TestClient, username: str) -> dict[str, str]:
    """A viewer the admin created, signed in: their bearer header."""
    created = client.post("/api/v1/users", json={"username": username, "password": "viewer-pass-1", "role": "viewer"})
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={"username": username, "password": "viewer-pass-1"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


class TestReleases:
    def test_signing_in_is_required(self, async_client: TestClient) -> None:
        assert async_client.get(ENDPOINT).status_code == 401
        assert async_client.post(SEEN).status_code == 401

    def test_the_notes_go_up_to_the_running_version_and_the_newest_is_unseen_at_first(
        self, viewer_client: TestClient
    ) -> None:
        body = viewer_client.get(ENDPOINT).json()

        assert body["current_version"] == "6.2.0"
        assert [n["version"] for n in body["notes"]] == ["6.2.0", "6.1.4"]
        assert body["notes"][1]["from"] == "6.1.0"
        assert (body["last_seen_version"], body["unseen"], body["update"]) == (None, 1, None)

    def test_opening_what_s_new_marks_the_running_version_seen_until_the_next_release(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headers = sign_in(authenticated_client, "reader")

        seen = authenticated_client.post(SEEN, headers=headers).json()
        assert (seen["last_seen_version"], seen["unseen"]) == ("6.2.0", 0)
        assert authenticated_client.get(ENDPOINT, headers=headers).json()["unseen"] == 0

        monkeypatch.setattr(releases, "__version__", "6.2.1")
        upgraded = authenticated_client.get(ENDPOINT, headers=headers).json()
        assert (upgraded["last_seen_version"], upgraded["unseen"]) == ("6.2.0", 1)
        assert upgraded["notes"][0]["version"] == "6.2.1"

    def test_an_admin_sees_the_releases_newer_than_the_running_one(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        test_settings.update_check = True
        store_finding(authenticated_client, "6.1.4", "6.2.0", "6.2.1", "6.3.0")

        update = authenticated_client.get(ENDPOINT).json()["update"]

        assert update["version"] == "6.3.0"
        assert [n["version"] for n in update["notes"]] == ["6.3.0", "6.2.1"]

    def test_nothing_newer_listed_is_no_update(self, authenticated_client: TestClient, test_settings: Settings) -> None:
        test_settings.update_check = True
        store_finding(authenticated_client, "6.1.4", "6.2.0")
        assert authenticated_client.get(ENDPOINT).json()["update"] is None

    def test_editors_and_viewers_are_not_told(
        self, viewer_client: TestClient, editor_auth_headers: dict[str, str], test_settings: Settings
    ) -> None:
        test_settings.update_check = True
        store_finding(viewer_client, "6.3.0")
        assert viewer_client.get(ENDPOINT, headers=editor_auth_headers).json()["update"] is None
        assert viewer_client.get(ENDPOINT).json()["update"] is None

    def test_with_the_check_off_an_earlier_finding_is_not_shown(self, authenticated_client: TestClient) -> None:
        store_finding(authenticated_client, "6.3.0")
        assert authenticated_client.get(ENDPOINT).json()["update"] is None
