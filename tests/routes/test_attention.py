# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Needs attention on an api process: a project's lines read off what Postgres and Redis hold, the mutes editors set
and History records, every project's problem count, and the install-wide lines.

Rows are written straight to the app's Postgres as the services and workers write them: connectors and proxies through
the repositories, failure and request counts as a worker's metrics flush writes them, heartbeats as live processes
write them.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from api.core import utc_now
from api.db.models import ConnectorMetricsModel, ProjectMetricsModel
from api.db.redis import CHANGES_STREAM_KEY
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.session import SessionFactory
from api.models.connector import BYTES_PER_GB, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyStatus
from tests.attention.test_install import snapshot
from tests.conftest import api_runtime, on_app_loop

STATIC = CredentialType.STATIC_PROXY_PROVIDER.value
OXYLABS = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}


def _url(project_id: str, rest: str = "") -> str:
    return f"/api/v1/projects/{project_id}/attention{rest}"


def _seed(client: TestClient, write: Callable[[SessionFactory], Awaitable[Any]]) -> Any:
    """``write`` run on the app's loop with its sessions, as a service or a worker would."""
    return on_app_loop(client, write, api_runtime(client).session_factory)


def _connector(client: TestClient, project_id: str, name: str, type: str = STATIC, **fields: Any) -> Connector:
    """A connector of the project on a credential of its own, stored."""
    config = fields.pop("credential", {})
    credential = Credential(name=f"{name} key", type=type, project_id=project_id, config=config)
    connector = Connector(name=name, credential_id=credential.id, credential_type=type, project_id=project_id, **fields)

    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            await CredentialRepository(session).create(credential)
            await ConnectorRepository(session).create(connector)
            await session.commit()

    _seed(client, write)
    return connector


def _proxies(client: TestClient, connector: Connector, n: int, status: ProxyStatus = ProxyStatus.HEALTHY) -> list[Proxy]:
    proxies = [
        Proxy(host=f"{uuid.uuid4().hex[:8]}.test", port=8080, connector_id=connector.id, status=status) for _ in range(n)
    ]

    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            await ProxyRepository(session).bulk_create(proxies)
            await session.commit()

    _seed(client, write)
    return proxies


def _statuses(client: TestClient, proxies: list[Proxy], status: ProxyStatus) -> None:
    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            for proxy in proxies:
                await ProxyRepository(session).set_status(proxy.id, status)
            await session.commit()

    _seed(client, write)


def _failures(client: TestClient, counts: dict[tuple[str, str | None, str, int | None], int]) -> None:
    """Failure counts as the metrics flush writes them, stamped now."""

    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            assert await MetricsRepository(session).save_failure_metrics_snapshots(counts) == []
            await session.commit()

    _seed(client, write)


def _requests(client: TestClient, *, project: tuple[str, int] | None = None, connector: tuple[str, int] | None = None) -> None:
    """A flushed minute of requests, a minute ago."""

    async def write(sessions: SessionFactory) -> None:
        at = utc_now() - timedelta(minutes=1)
        async with sessions() as session:
            if project is not None:
                session.add(ProjectMetricsModel(project_id=project[0], timestamp=at, request_count=project[1], granularity=60))
            if connector is not None:
                session.add(ConnectorMetricsModel(connector_id=connector[0], timestamp=at, request_count=connector[1], granularity=60))
            await session.commit()

    _seed(client, write)


def _stored_mutes(client: TestClient, project_id: str) -> dict[str, Any]:
    async def read(sessions: SessionFactory) -> dict[str, Any]:
        async with sessions() as session:
            return (await ProjectRepository(session).attention_mutes([project_id]))[project_id]

    result: dict[str, Any] = _seed(client, read)
    return result


def _history(client: TestClient, project_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/audit/project/{project_id}")
    assert response.status_code == 200, response.text
    return [entry for entry in response.json()["entries"] if entry["action"] in ("muted", "unmuted")]


def _report(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.get(_url(project_id))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.fixture
def failing_pool(authenticated_client: TestClient, created_project: dict[str, Any]) -> tuple[str, list[Proxy]]:
    """A project whose one connector's proxies all fail their checks: one problem, ``pool_no_routable``."""
    connector = _connector(authenticated_client, created_project["id"], "Static")
    return created_project["id"], _proxies(authenticated_client, connector, 3, ProxyStatus.UNHEALTHY)


class TestReport:
    def test_an_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(_url("nope")).status_code == 404
        assert authenticated_client.put(_url("nope", "/mutes"), json={"key": "connectors_off", "duration": "day"}).status_code == 404
        assert authenticated_client.delete(_url("nope", "/mutes/connectors_off")).status_code == 404

    def test_a_project_without_a_connector(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        report = _report(authenticated_client, created_project["id"])

        assert set(report) == {"generated_at", "lines", "problems", "warnings", "muted"}
        assert (report["problems"], report["warnings"], report["muted"]) == (1, 0, 0)
        assert report["lines"] == [
            {
                "key": "connectors_off",
                "kind": "connectors_off",
                "severity": "problem",
                "lead": "",
                "subject": "",
                "text": "Add a connector to start serving requests.",
                "detail": None,
                "when": None,
                "link": "connectors",
                "action": "Connectors",
                "mute": None,
            }
        ]

    def test_the_lines_the_stored_state_makes_in_display_order(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        static = _connector(authenticated_client, project_id, "Static")
        _proxies(authenticated_client, static, 4)
        dead = _connector(authenticated_client, project_id, "Dead")
        _proxies(authenticated_client, dead, 2, ProxyStatus.UNHEALTHY)
        failed_at = utc_now() - timedelta(minutes=7)
        synced = _connector(
            authenticated_client, project_id, "Oxylabs", "oxylabs", credential=OXYLABS,
            last_error="407 Proxy Authentication Required", last_error_at=failed_at, consecutive_errors=2,
        )
        _requests(authenticated_client, project=(project_id, 380), connector=(static.id, 100))
        _failures(authenticated_client, {
            (project_id, static.id, "upstream_rejected", 407): 30,
            (project_id, None, "no_proxy_available", None): 20,
        })

        report = _report(authenticated_client, project_id)

        assert [line["key"] for line in report["lines"]] == [
            f"vendor_login:{static.id}",
            "refusals:no_proxy_available",
            f"connector_sync_failing:{synced.id}",
            f"connector_no_healthy_proxy:{dead.id}",
        ]
        assert (report["problems"], report["warnings"]) == (1, 3)
        login, refused, sync, _dead = report["lines"]
        assert login["text"] == ": the vendor is rejecting its login (30% of requests in the last 15 minutes)."
        assert login["when"]["kind"] == "last_at"
        assert (login["link"], login["action"]) == (f"connectors?open={static.id}", "Open connector")
        assert (refused["severity"], refused["subject"]) == ("warning", "20 requests (5%)")
        assert sync["when"] == {"kind": "last_failed", "at": failed_at.isoformat()}

    def test_countries_traffic_and_a_connector_still_being_deleted(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        _proxies(authenticated_client, _connector(authenticated_client, project_id, "Healthy"), 2)
        _connector(authenticated_client, project_id, "Germany", config={"countries": ["DE"]})
        metered = _connector(
            authenticated_client, project_id, "Metered",
            traffic_config={"limit_bytes": 10 * BYTES_PER_GB, "action": "block", "warn_percent": 80},
        )
        _proxies(authenticated_client, metered, 1)
        leaving = _connector(
            authenticated_client, project_id, "Leaving", enabled=False, pending_deletion=True,
            updated_at=utc_now() - timedelta(minutes=45),
        )

        async def carried(sessions: SessionFactory) -> None:
            async with sessions() as session:
                session.add(ConnectorMetricsModel(
                    connector_id=metered.id, timestamp=utc_now(), bytes_received=9 * BYTES_PER_GB, granularity=60
                ))
                await session.commit()

        _seed(authenticated_client, carried)

        report = _report(authenticated_client, project_id)

        assert [(line["key"], line["severity"]) for line in report["lines"]] == [
            ("countries_unserved", "problem"),
            (f"connector_traffic_near:{metered.id}", "warning"),
            (f"connector_stuck_deleting:{leaving.id}", "warning"),
        ]
        countries, near, stuck = report["lines"]
        assert (countries["link"], countries["subject"]) == ("countries?country=DE", "Germany")
        assert near["text"] == " has used 90% of its 10 GB limit this month. New requests stop at the limit."
        assert near["when"]["kind"] == "resets"
        assert stuck["text"] == " is still being deleted after 45 minutes: its cloud instances have not all shut down."

    def test_a_project_pending_deletion_has_no_lines(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]

        async def delete(sessions: SessionFactory) -> None:
            async with sessions() as session:
                await ProjectRepository(session).mark_pending_deletion(project_id)
                await session.commit()

        _seed(authenticated_client, delete)

        report = _report(authenticated_client, project_id)
        assert (report["lines"], report["problems"], report["warnings"], report["muted"]) == ([], 0, 0, 0)


class TestMutes:
    def test_a_mute_hides_the_line_from_the_counts_for_everyone(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str], viewer_auth_headers: dict[str, str],
        failing_pool: tuple[str, list[Proxy]],
    ) -> None:
        project_id, _rows = failing_pool
        response = authenticated_client.put(
            _url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "week"}, headers=editor_auth_headers
        )
        assert response.status_code == 204, response.text

        report = authenticated_client.get(_url(project_id), headers=viewer_auth_headers).json()

        assert (report["problems"], report["warnings"], report["muted"]) == (0, 0, 1)
        (line,) = report["lines"]
        mute = line["mute"]
        assert (mute["severity"], mute["duration"], mute["muted_by"], mute["active"]) == ("problem", "week", "testeditor", True)
        assert mute["until"] is not None and mute["muted_at"] is not None

    def test_viewers_cannot_mute_or_unmute(
        self, authenticated_client: TestClient, viewer_auth_headers: dict[str, str], failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool
        body = {"key": "pool_no_routable", "duration": "day"}

        assert authenticated_client.put(_url(project_id, "/mutes"), json=body, headers=viewer_auth_headers).status_code == 403
        assert authenticated_client.delete(_url(project_id, "/mutes/pool_no_routable"), headers=viewer_auth_headers).status_code == 403
        assert _stored_mutes(authenticated_client, project_id) == {}

    def test_only_a_line_there_now_with_a_known_duration(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool

        response = authenticated_client.put(_url(project_id, "/mutes"), json={"key": "connectors_off", "duration": "day"})
        assert (response.status_code, response.json()) == (404, {"detail": "Nothing with that key needs attention now"})
        assert authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "year"}).status_code == 422
        assert authenticated_client.put(_url(project_id, "/mutes"), json={"key": "", "duration": "day"}).status_code == 422

    def test_an_unmute_shows_the_line_again_and_another_changes_nothing(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool
        authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "day"})

        assert authenticated_client.delete(_url(project_id, "/mutes/pool_no_routable")).status_code == 204
        report = _report(authenticated_client, project_id)
        assert (report["problems"], report["muted"], report["lines"][0]["mute"]) == (1, 0, None)
        assert authenticated_client.delete(_url(project_id, "/mutes/pool_no_routable")).status_code == 204
        assert authenticated_client.delete(_url(project_id, "/mutes/never%3Amuted")).status_code == 204

        assert [entry["action"] for entry in _history(authenticated_client, project_id)] == ["unmuted", "muted"]

    def test_history_says_who_muted_and_unmuted_which_line_until_when(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str], failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool
        authenticated_client.put(
            _url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "until_clear"}, headers=editor_auth_headers
        )
        authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "day"})
        authenticated_client.delete(_url(project_id, "/mutes/pool_no_routable"), headers=editor_auth_headers)

        unmuted, muted_day, muted_clear = _history(authenticated_client, project_id)

        sentence = "No proxy can take requests: 3 unhealthy. Requests fail until one recovers."
        assert (muted_clear["actor"], muted_clear["entity_type"], muted_clear["entity_id"], muted_clear["project_id"]) == (
            "testeditor", "project", project_id, project_id
        )
        assert muted_clear["details"] == {"line": sentence, "muted_until": "it clears"}
        assert (muted_day["actor"], muted_day["details"]["line"]) == ("testadmin", sentence)
        # A day from when it was muted.
        until = datetime.fromisoformat(muted_day["details"]["muted_until"])
        assert abs(until - datetime.fromisoformat(muted_day["created_at"]) - timedelta(days=1)) < timedelta(seconds=5)
        assert (unmuted["actor"], unmuted["details"]) == ("testeditor", {"line": sentence})

    def test_an_until_clear_mute_ends_with_its_line_and_no_one_is_credited(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, proxies = failing_pool
        authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "until_clear"})
        assert _report(authenticated_client, project_id)["muted"] == 1

        _statuses(authenticated_client, proxies, ProxyStatus.HEALTHY)

        assert _report(authenticated_client, project_id)["lines"] == []
        assert _stored_mutes(authenticated_client, project_id) == {}
        # The pool fails again: the line is back, unmuted.
        _statuses(authenticated_client, proxies, ProxyStatus.UNHEALTHY)
        report = _report(authenticated_client, project_id)
        assert (report["problems"], report["lines"][0]["mute"]) == (1, None)
        assert [entry["action"] for entry in _history(authenticated_client, project_id)] == ["muted"]

    def test_a_timed_mute_that_ended_counts_the_line_again(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool
        ended = (utc_now() - timedelta(minutes=1)).isoformat()

        async def write(sessions: SessionFactory) -> None:
            async with sessions() as session:
                await ProjectRepository(session).write_attention_mutes(project_id, {
                    "pool_no_routable": {
                        "severity": "problem", "duration": "day", "until": ended, "by": "ivan", "by_id": "u",
                        "at": (utc_now() - timedelta(days=1)).isoformat(), "text": "...",
                    }
                })
                await session.commit()

        _seed(authenticated_client, write)

        report = _report(authenticated_client, project_id)
        assert (report["problems"], report["muted"], report["lines"][0]["mute"]) == (1, 0, None)

    def test_a_muted_warning_that_turns_into_a_problem_shows_again(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        connector = _connector(authenticated_client, project_id, "Webshare")
        _proxies(authenticated_client, connector, 2)
        _requests(authenticated_client, connector=(connector.id, 1000))
        _failures(authenticated_client, {(project_id, connector.id, "upstream_rejected", 429): 100})
        key = f"vendor_rate_limited:{connector.id}"
        assert authenticated_client.put(_url(project_id, "/mutes"), json={"key": key, "duration": "week"}).status_code == 204
        muted = _report(authenticated_client, project_id)
        assert (muted["warnings"], muted["muted"]) == (0, 1)

        _failures(authenticated_client, {(project_id, connector.id, "upstream_rejected", 429): 500})

        report = _report(authenticated_client, project_id)
        (line,) = report["lines"]
        assert (line["severity"], line["mute"]["severity"], line["mute"]["active"]) == ("problem", "warning", False)
        assert (report["problems"], report["muted"]) == (1, 0)

    def test_a_mute_reaches_no_gateway_and_asks_for_no_compile(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, _rows = failing_pool
        runtime = api_runtime(authenticated_client)

        async def compiled() -> str:
            sections = await runtime._compiler.compile_project(project_id, quarantined={}, blocked_until={})
            return "".join(section.model_dump_json() for section in sections.values())

        async def announced() -> int:
            return int(await runtime.redis_client.client.xlen(CHANGES_STREAM_KEY))

        before, changes = on_app_loop(authenticated_client, compiled), on_app_loop(authenticated_client, announced)
        authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "week"})
        authenticated_client.delete(_url(project_id, "/mutes/pool_no_routable"))

        assert on_app_loop(authenticated_client, compiled) == before
        assert on_app_loop(authenticated_client, announced) == changes
        assert "attention_mutes" not in authenticated_client.get(f"/api/v1/projects/{project_id}").json()


class TestProjectList:
    def test_each_project_s_problems_are_its_card_s(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        made = []
        for n in range(4):
            data = {**sample_project_data, "name": f"{sample_project_data['name']} {n}", "username": f"{sample_project_data['username']}{n}"}
            made.append(authenticated_client.post("/api/v1/projects", json=data).json()["id"])
        empty, failing, muted, healthy = made
        for project_id in (failing, muted):
            connector = _connector(authenticated_client, project_id, "Static", config={"countries": ["DE"]})
            _proxies(authenticated_client, connector, 2, ProxyStatus.UNHEALTHY)
            _failures(authenticated_client, {(project_id, connector.id, "upstream_rejected", 407): 30})
        authenticated_client.put(_url(muted, "/mutes"), json={"key": "pool_no_routable", "duration": "day"})
        _proxies(authenticated_client, _connector(authenticated_client, healthy, "Fine"), 2)
        leaving = authenticated_client.post(
            "/api/v1/projects", json={**sample_project_data, "name": f"{sample_project_data['name']} leaving",
                                      "username": f"{sample_project_data['username']}-leaving"}
        ).json()["id"]

        async def delete(sessions: SessionFactory) -> None:
            async with sessions() as session:
                await ProjectRepository(session).mark_pending_deletion(leaving)
                await session.commit()

        _seed(authenticated_client, delete)

        response = authenticated_client.get("/api/v1/attention/projects")

        assert response.status_code == 200
        counts = {row["project_id"]: row["problems"] for row in response.json()["projects"]}
        assert leaving not in counts
        assert [counts[project_id] for project_id in made] == [
            _report(authenticated_client, project_id)["problems"] for project_id in made
        ] == [1, 2, 1, 0]
        listed = [row["project_id"] for row in response.json()["projects"]]
        assert [project_id for project_id in listed if project_id in made] == made


class TestInstall:
    def test_every_role_reads_the_same_lines_with_no_deployment_detail(
        self, authenticated_client: TestClient, viewer_auth_headers: dict[str, str]
    ) -> None:
        redis = api_runtime(authenticated_client).redis_client
        secret_id, secret_version = "gw-ip-10-0-3-17.internal", "6.9.42-rc7"
        beat = snapshot(
            secret_id, "gateway", version=secret_version, reasons=["Postgres unreachable"],
            gateway={"listening": False, "geo_database_errors": {"secret-db-id": "/etc/octoprox/secret.mmdb: no such file"}},
        )
        on_app_loop(authenticated_client, redis.advertise_instance, secret_id, "gateway", beat.model_dump_json())
        try:
            viewer = authenticated_client.get("/api/v1/attention/install", headers=viewer_auth_headers)
            admin = authenticated_client.get("/api/v1/attention/install")
        finally:
            on_app_loop(authenticated_client, redis.withdraw_instance, secret_id)

        assert viewer.status_code == admin.status_code == 200
        body = viewer.json()
        keys = [line["key"] for line in body["lines"]]
        assert keys[:2] == ["no_gateway_serving", "store_unreachable:database"]
        assert [line["key"] for line in admin.json()["lines"]] == keys
        for secret in (secret_id, secret_version, "secret-db-id", "secret.mmdb", "10-0-3-17"):
            assert secret not in viewer.text
