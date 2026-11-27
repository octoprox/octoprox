# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The alerts endpoints on an api process: a project's alert history and its alert cut-offs, the install's alert history
and its retention, each read from what the workers store and recorded in History as an editor or admin changes it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from starlette.testclient import TestClient

from api.attention.alert import REOPEN_WINDOW, FoundAlert
from api.attention.checks import AttentionKind, Finding
from api.attention.install import InstallKind
from api.core import utc_now
from api.core.roles import JobKind
from api.db.alert_repository import AlertRepository
from api.db.models import AlertModel, ProjectModel
from api.db.redis import ALERT_CHECK_KEY
from api.db.session import SessionFactory
from api.models.attention import Severity
from tests.conftest import api_runtime, on_app_loop, queued_jobs


def _url(project_id: str, rest: str = "") -> str:
    return f"/api/v1/projects/{project_id}{rest}"


def _store(client: TestClient, now: datetime, found: list[FoundAlert], checked: list[str]) -> None:
    """Alerts as a run at ``now`` stores them, written straight to the app's Postgres."""

    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            await AlertRepository(session).reconcile(found, checked, [], now, reopen=REOPEN_WINDOW)
            await session.commit()

    on_app_loop(client, write, api_runtime(client).session_factory)


def _finding(key: str, severity: Severity = Severity.WARNING, subject: str = "") -> Finding:
    kind = key.partition(":")[0]
    install = kind in InstallKind._value2member_map_
    return Finding(
        key=key,
        kind=InstallKind(kind) if install else AttentionKind(kind),
        severity=severity,
        lead="",
        subject=subject,
        text=" needs attention.",
        link="system" if install else "connectors",
        action="" if install else "Connectors",
    )


@pytest.fixture
def history(authenticated_client: TestClient, created_project: dict[str, Any]) -> tuple[str, datetime]:
    """Three alerts of the project a minute apart: one resolved after reaching a problem, two open, the newest muted."""
    project_id = created_project["id"]
    start = utc_now() - timedelta(hours=1)
    _store(authenticated_client, start, [FoundAlert(project_id, _finding("vendor_login:a", Severity.PROBLEM, "Plain_UK"))], [])
    _store(authenticated_client, start + timedelta(minutes=1), [FoundAlert(project_id, _finding("vendor_login:b", subject="Webshare"))], [])
    _store(
        authenticated_client,
        start + timedelta(minutes=2),
        [
            FoundAlert(project_id, _finding("vendor_login:b", subject="Webshare")),
            FoundAlert(project_id, _finding("connectors_off", Severity.PROBLEM)),
        ],
        [project_id],
    )
    muted = authenticated_client.put(_url(project_id, "/attention/mutes"), json={"key": "connectors_off", "duration": "day"})
    assert muted.status_code == 204, muted.text
    return project_id, start


class TestProjectAlerts:
    def test_newest_first_with_the_counts_and_each_open_alert_s_mute(
        self, authenticated_client: TestClient, viewer_auth_headers: dict[str, str], history: tuple[str, datetime]
    ) -> None:
        project_id, start = history

        response = authenticated_client.get(_url(project_id, "/alerts"), headers=viewer_auth_headers)

        assert response.status_code == 200, response.text
        page = response.json()
        assert [alert["key"] for alert in page["alerts"]] == ["connectors_off", "vendor_login:b", "vendor_login:a"]
        assert (page["open"], page["resolved"], page["newer"], page["more"]) == (2, 1, 0, False)
        off, webshare, plain = page["alerts"]
        assert off["mute"]["duration"] == "day" and off["hidden_by"] is None
        assert (webshare["mute"], webshare["subject"], webshare["opened_at"]) == (
            None, "Webshare", (start + timedelta(minutes=1)).isoformat()
        )
        assert (plain["severity"], plain["worst_severity"], plain["resolved_at"], plain["mute"]) == (
            "problem", "problem", (start + timedelta(minutes=2)).isoformat(), None
        )
        assert set(plain) == {
            "id", "key", "kind", "severity", "worst_severity", "lead", "subject", "text", "detail", "when", "link",
            "action", "hidden_by", "opened_at", "reopened_at", "last_seen_at", "resolved_at", "reopened", "mute",
        }

    def test_a_covered_alert_names_its_cover_only_while_open(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        start = utc_now() - timedelta(hours=1)
        cover = FoundAlert(project_id, _finding("connectors_off", Severity.PROBLEM))
        covered = FoundAlert(
            project_id, _finding("refusals:no_proxy_available", Severity.PROBLEM), hidden_by="connectors_off"
        )
        _store(authenticated_client, start, [cover, covered], [project_id])

        def hidden_by() -> Any:
            response = authenticated_client.get(_url(project_id, "/alerts"), params={"kind": "refusals"})
            assert response.status_code == 200, response.text
            return response.json()["alerts"][0]["hidden_by"]

        assert hidden_by() == "connectors_off"
        _store(authenticated_client, start + timedelta(minutes=1), [], [project_id])
        assert hidden_by() is None

    def test_the_filters_paging_and_the_newer_count(
        self, authenticated_client: TestClient, history: tuple[str, datetime]
    ) -> None:
        project_id, start = history

        def keys(**params: Any) -> list[str]:
            response = authenticated_client.get(_url(project_id, "/alerts"), params=params)
            assert response.status_code == 200, response.text
            return [alert["key"] for alert in response.json()["alerts"]]

        assert keys(status="resolved") == ["vendor_login:a"]
        assert keys(status="open", severity="problem") == ["connectors_off"]
        assert keys(severity="problem") == ["connectors_off", "vendor_login:a"]
        assert keys(kind="vendor_login") == ["vendor_login:b", "vendor_login:a"]
        assert keys(q="n_U") == ["vendor_login:a"]
        assert keys(since=(start + timedelta(minutes=3)).isoformat()) == ["connectors_off", "vendor_login:b"]
        # A time with its zone reads as the stored times do, in UTC.
        assert keys(since=(start + timedelta(minutes=3)).isoformat() + "+00:00") == ["connectors_off", "vendor_login:b"]

        until = (start + timedelta(minutes=1)).isoformat()
        held = authenticated_client.get(_url(project_id, "/alerts"), params={"until": until}).json()
        assert ([alert["key"] for alert in held["alerts"]], held["until"], held["newer"]) == (
            ["vendor_login:b", "vendor_login:a"], until, 1
        )

        first = authenticated_client.get(_url(project_id, "/alerts"), params={"limit": 2}).json()
        assert first["more"] is True
        rest = authenticated_client.get(
            _url(project_id, "/alerts"), params={"limit": 2, "before": first["alerts"][-1]["id"]}
        ).json()
        assert ([alert["key"] for alert in rest["alerts"]], rest["more"]) == (["vendor_login:a"], False)

        for bad in ({"status": "closed"}, {"severity": "fatal"}, {"limit": 201}, {"q": ""}, {"kind": "k" * 49}):
            assert authenticated_client.get(_url(project_id, "/alerts"), params=bad).status_code == 422, bad

    def test_a_zoned_until_reads_as_utc_and_comes_back_without_its_zone(
        self, authenticated_client: TestClient, history: tuple[str, datetime]
    ) -> None:
        project_id, start = history
        until = start + timedelta(minutes=1)

        held = authenticated_client.get(
            _url(project_id, "/alerts"), params={"until": (until + timedelta(hours=2)).isoformat() + "+02:00"}
        ).json()

        assert ([alert["key"] for alert in held["alerts"]], held["until"], held["newer"]) == (
            ["vendor_login:b", "vendor_login:a"], until.isoformat(), 1
        )

    def test_one_alert_and_the_404s(self, authenticated_client: TestClient, history: tuple[str, datetime]) -> None:
        project_id, _start = history
        listed = authenticated_client.get(_url(project_id, "/alerts")).json()["alerts"][0]

        one = authenticated_client.get(_url(project_id, f"/alerts/{listed['id']}"))
        assert (one.status_code, one.json()) == (200, listed)

        assert authenticated_client.get(_url(project_id, "/alerts/nope")).status_code == 404
        assert authenticated_client.get(_url("nope", "/alerts")).status_code == 404
        assert authenticated_client.get(_url("nope", f"/alerts/{listed['id']}")).status_code == 404


class TestAlertSettings:
    def test_a_viewer_reads_them_with_the_defaults(
        self, authenticated_client: TestClient, viewer_auth_headers: dict[str, str], created_project: dict[str, Any]
    ) -> None:
        response = authenticated_client.get(_url(created_project["id"], "/alert-settings"), headers=viewer_auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["settings"] == body["defaults"]
        assert (body["defaults"]["success_warning_below"], body["defaults"]["keep_days"]) == (80, None)
        assert authenticated_client.get(_url("nope", "/alert-settings")).status_code == 404

    def test_an_editor_replaces_them_recorded_checked_at_once_and_compiling_nothing(
        self,
        authenticated_client: TestClient,
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
        created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        before = authenticated_client.get(_url(project_id)).json()["updated_at"]
        body = {"success_warning_below": 70, "vendor_min": 20, "keep_days": 0}

        assert authenticated_client.put(_url(project_id, "/alert-settings"), json=body, headers=viewer_auth_headers).status_code == 403
        response = authenticated_client.put(_url(project_id, "/alert-settings"), json=body, headers=editor_auth_headers)

        assert response.status_code == 200, response.text
        settings = response.json()["settings"]
        assert (settings["success_warning_below"], settings["vendor_min"], settings["keep_days"]) == (70, 20, 0)
        assert authenticated_client.get(_url(project_id, "/alert-settings")).json()["settings"] == settings
        # Not an edit of the project: nothing for the routing feed.
        assert authenticated_client.get(_url(project_id)).json()["updated_at"] == before
        assert project_id in queued_jobs(authenticated_client, JobKind.ALERTS)
        redis = api_runtime(authenticated_client).redis_client
        marks = on_app_loop(authenticated_client, redis.client.hgetall, ALERT_CHECK_KEY.format(scope=project_id))
        assert "requested" in marks

        # Back to the defaults: a field left out is its default again.
        authenticated_client.put(_url(project_id, "/alert-settings"), json={"vendor_min": 20}, headers=editor_auth_headers)
        # Unchanged: nothing recorded.
        authenticated_client.put(_url(project_id, "/alert-settings"), json={"vendor_min": 20}, headers=editor_auth_headers)

        entries = [
            entry
            for entry in authenticated_client.get(f"/api/v1/audit/project/{project_id}").json()["entries"]
            if entry["action"] == "updated"
        ]
        assert [entry["changes"] for entry in entries] == [
            {"alerts.success_warning_below": [70, 80], "alerts.keep_days": [0, None]},
            {"alerts.success_warning_below": [80, 70], "alerts.vendor_min": [10, 20], "alerts.keep_days": [None, 0]},
        ]
        assert entries[0]["actor"] == "testeditor"

        # Only the values that differ from the defaults are stored.
        async def stored(sessions: SessionFactory) -> dict[str, Any]:
            async with sessions() as session:
                row = await session.execute(select(ProjectModel.alert_settings).where(ProjectModel.id == project_id))
                return dict(row.scalar_one())

        assert on_app_loop(authenticated_client, stored, api_runtime(authenticated_client).session_factory) == {
            "vendor_min": 20
        }

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                {"refusals_warning_percent": 30, "refusals_problem_percent": 20},
                "Value error, refusals_warning_percent cannot be above refusals_problem_percent",
            ),
            (
                {"success_warning_below": 50, "success_problem_below": 50},
                "Value error, success_problem_below must be below success_warning_below",
            ),
            ({"stuck_deleting_minutes": 1}, "Input should be greater than or equal to 5"),
            ({"not_a_setting": 100}, "Extra inputs are not permitted"),
        ],
    )
    def test_a_refused_value_says_why(
        self, authenticated_client: TestClient, created_project: dict[str, Any], body: dict[str, Any], message: str
    ) -> None:
        response = authenticated_client.put(_url(created_project["id"], "/alert-settings"), json=body)

        assert response.status_code == 422
        assert [error["msg"] for error in response.json()["detail"]] == [message]


class TestInstallAlerts:
    @pytest.fixture(autouse=True)
    def _no_install_alerts(self, authenticated_client: TestClient) -> None:
        """The app's Postgres outlives a test: start from no install alert."""

        async def clear(sessions: SessionFactory) -> None:
            async with sessions() as session:
                await session.execute(delete(AlertModel).where(AlertModel.project_id.is_(None)))
                await session.commit()

        on_app_loop(authenticated_client, clear, api_runtime(authenticated_client).session_factory)

    def test_admins_list_them_and_editors_cannot(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str]
    ) -> None:
        now = utc_now()
        _store(authenticated_client, now - timedelta(minutes=5), [FoundAlert("", _finding("ip_databases"))], [""])
        _store(authenticated_client, now, [FoundAlert("", _finding("no_gateway_serving", Severity.PROBLEM))], [""])

        page = authenticated_client.get("/api/v1/alerts/install").json()

        assert [(alert["key"], alert["resolved_at"] is None) for alert in page["alerts"]] == [
            ("no_gateway_serving", True), ("ip_databases", False)
        ]
        assert (page["open"], page["resolved"]) == (1, 1)
        assert page["alerts"][0]["mute"] is None and page["alerts"][0]["action"] == ""
        assert [a["key"] for a in authenticated_client.get("/api/v1/alerts/install", params={"status": "resolved"}).json()["alerts"]] == [
            "ip_databases"
        ]
        assert authenticated_client.get("/api/v1/alerts/install", headers=editor_auth_headers).status_code == 403

    def test_one_by_its_id_for_admins(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str], history: tuple[str, datetime]
    ) -> None:
        _store(authenticated_client, utc_now(), [FoundAlert("", _finding("ip_databases"))], [""])
        listed = authenticated_client.get("/api/v1/alerts/install").json()["alerts"][0]

        one = authenticated_client.get(f"/api/v1/alerts/install/{listed['id']}")

        assert (one.status_code, one.json()) == (200, listed)
        assert authenticated_client.get(f"/api/v1/alerts/install/{listed['id']}", headers=editor_auth_headers).status_code == 403
        assert authenticated_client.get("/api/v1/alerts/install/nope").status_code == 404
        # A project's alert is not the install's.
        project_id, _start = history
        project_alert = authenticated_client.get(_url(project_id, "/alerts")).json()["alerts"][0]
        assert authenticated_client.get(f"/api/v1/alerts/install/{project_alert['id']}").status_code == 404

    def test_the_retention_is_admins_to_set_and_recorded(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str]
    ) -> None:
        assert authenticated_client.get("/api/v1/alerts/install/retention").json() == {"days": 90}
        assert authenticated_client.get("/api/v1/alerts/install/retention", headers=editor_auth_headers).status_code == 403
        assert authenticated_client.put(
            "/api/v1/alerts/install/retention", json={"days": 30}, headers=editor_auth_headers
        ).status_code == 403
        assert authenticated_client.put("/api/v1/alerts/install/retention", json={"days": -1}).status_code == 422

        response = authenticated_client.put("/api/v1/alerts/install/retention", json={"days": 30})

        assert (response.status_code, response.json()) == (200, {"days": 30})
        assert authenticated_client.get("/api/v1/alerts/install/retention").json() == {"days": 30}
        entries = authenticated_client.get("/api/v1/audit/system_settings/1").json()["entries"]
        assert entries[0]["changes"] == {"install_alert_retention_days": [90, 30]}
        # The audit log's retention is left as it was.
        assert authenticated_client.get("/api/v1/audit/retention").json() == {"days": 365}

    def test_a_retention_save_is_recorded_as_its_admin_and_logged(self, authenticated_client: TestClient) -> None:
        kept = authenticated_client.get("/api/v1/alerts/install/retention").json()["days"]
        days = kept % 1000 + 1

        with patch("api.services.system_settings.logger") as log:
            response = authenticated_client.put("/api/v1/alerts/install/retention", json={"days": days})

        assert (response.status_code, response.json()) == (200, {"days": days})
        entry = authenticated_client.get("/api/v1/audit/system_settings/1").json()["entries"][0]
        assert {key: entry[key] for key in ("action", "entity_name", "actor_id", "actor", "changes", "details")} == {
            "action": "updated",
            "entity_name": "System settings",
            "actor_id": "test-admin-id",
            "actor": "testadmin",
            "changes": {"install_alert_retention_days": [kept, days]},
            "details": {},
        }
        assert entry["request_id"] == response.headers["x-request-id"]
        log.info.assert_called_once_with("Install alert retention changed", days=days, admin="testadmin")
        assert authenticated_client.put("/api/v1/alerts/install/retention", json={"days": kept}).status_code == 200
