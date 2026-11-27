# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Needs attention on an api process: a project's lines read from the alerts the workers store, the mutes editors set
and History records, every project's problem count, and the install-wide lines with those the api answers itself.

Rows are written straight to the app's Postgres as the services and workers write them: connectors and proxies through
the repositories, failure and request counts as a worker's metrics flush writes them, heartbeats as live processes
write them; then the worker's alerts job runs on the app's stores, as a worker would, and the api reads what it stored.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from api.attention import requests as requests_module
from api.attention.alert import CHECKS_STALE, REOPEN_WINDOW, FoundAlert
from api.attention.checks import AttentionKind, Finding
from api.attention.data import ProjectDataLoader
from api.attention.install import InstallChecks, InstallKind
from api.core import utc_now
from api.core.demand_tracker import DemandTracker
from api.core.roles import JobKind
from api.core.system_stats import ClusterReader
from api.db.alert_repository import AlertRepository
from api.db.models import AlertModel, ConnectorMetricsModel, ProjectMetricsModel
from api.db.redis import ALERT_CHECK_KEY, CHANGES_STREAM_KEY, INSTALL_SCOPE, JOB_STREAM_KEY
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.session import SessionFactory
from api.feed.compiler import FeedCompiler
from api.feed.preview import RoutingPreview
from api.feed.state import RuntimeStateReader
from api.geo.models import GeoSettings
from api.jobs import alerts as alerts_module
from api.jobs.alerts import AlertsJob
from api.jobs.job import JobBatch
from api.models.attention import AttentionWhen, Severity
from api.models.connector import BYTES_PER_GB, Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy, ProxyStatus
from api.services.read_model import RuntimeView
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


def _requests(
    client: TestClient,
    *,
    project: tuple[str, int] | None = None,
    connector: tuple[str, int] | None = None,
    succeeded: int | None = None,
) -> None:
    """A flushed minute of requests, a minute ago; every project request succeeded unless ``succeeded`` says."""

    async def write(sessions: SessionFactory) -> None:
        at = utc_now() - timedelta(minutes=1)
        async with sessions() as session:
            if project is not None:
                session.add(ProjectMetricsModel(
                    project_id=project[0], timestamp=at, request_count=project[1],
                    success_count=project[1] if succeeded is None else succeeded, granularity=60,
                ))
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


def _check(client: TestClient, *keys: str, at: datetime | None = None) -> None:
    """One run of the worker's alerts job on the app's stores: every project and the install, or the projects named,
    with its data read at ``at`` (now when None)."""
    runtime = api_runtime(client)
    sessions, redis, registry = runtime.session_factory, runtime.redis_client, runtime.provider_registry
    view = RuntimeView(sessions, RuntimeStateReader(redis), registry, DemandTracker(redis), strict=True)
    compiler = FeedCompiler(sessions, registry, geo_defaults=GeoSettings(), default_strategy="round_robin")
    preview = RoutingPreview(compiler, view, registry, default_strategy="round_robin", off_loop=True)
    job = AlertsJob(
        sessions,
        ProjectDataLoader(sessions, view, registry, preview),
        InstallChecks(redis, sessions, ClusterReader(redis), geo_updater_interval=3600),
        runtime.alert_checks,
        redis,
    )
    batch = JobBatch(ids=("1-0",), keys=keys or ("all",))
    with patch.object(alerts_module, "utc_now", lambda: at or utc_now()):
        on_app_loop(client, job.run, batch)


def _report(client: TestClient, project_id: str, *, check: bool = True, **headers: str) -> dict[str, Any]:
    """The project's card, the alerts job having run first unless ``check`` is False."""
    if check:
        _check(client)
    response = client.get(_url(project_id), headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _store(client: TestClient, now: datetime, found: list[FoundAlert], checked: list[str]) -> None:
    """Alerts as a run at ``now`` stores them, written straight to the app's Postgres."""

    async def write(sessions: SessionFactory) -> None:
        async with sessions() as session:
            await AlertRepository(session).reconcile(found, checked, [], now, reopen=REOPEN_WINDOW)
            await session.commit()

    _seed(client, write)


def _finding(key: str, severity: Severity = Severity.WARNING, **fields: Any) -> Finding:
    kind = key.partition(":")[0]
    return Finding(
        key=key,
        kind=InstallKind(kind) if kind in InstallKind._value2member_map_ else AttentionKind(kind),
        severity=severity,
        lead="",
        subject=fields.pop("subject", ""),
        text=fields.pop("text", " needs attention."),
        link=fields.pop("link", "connectors"),
        action=fields.pop("action", "Connectors"),
        **fields,
    )


@pytest.fixture
def failing_pool(authenticated_client: TestClient, created_project: dict[str, Any]) -> tuple[str, list[Proxy]]:
    """A project whose one connector's proxies all fail their checks: one problem, ``pool_no_routable``, checked."""
    connector = _connector(authenticated_client, created_project["id"], "Static")
    proxies = _proxies(authenticated_client, connector, 3, ProxyStatus.UNHEALTHY)
    _check(authenticated_client)
    return created_project["id"], proxies


class TestReport:
    def test_an_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(_url("nope")).status_code == 404
        assert authenticated_client.put(_url("nope", "/mutes"), json={"key": "connectors_off", "duration": "day"}).status_code == 404
        assert authenticated_client.delete(_url("nope", "/mutes/connectors_off")).status_code == 404

    def test_an_unknown_project_s_lines_answer_its_detail(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get(_url("nope"))

        assert (response.status_code, response.json()) == (404, {"detail": "Project not found"})

    def test_a_project_without_a_connector(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        report = _report(authenticated_client, created_project["id"])

        assert set(report) == {"generated_at", "lines", "problems", "warnings", "muted", "checked_at", "pending", "stale"}
        assert (report["problems"], report["warnings"], report["muted"]) == (1, 0, 0)
        assert (report["pending"], report["stale"]) == (False, False)
        (line,) = report["lines"]
        assert line == {
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
            "alert_id": line["alert_id"],
            "since": line["opened_at"],
            "opened_at": line["opened_at"],
            "reopened": 0,
        }
        assert line["opened_at"] == report["checked_at"]

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

        _check(authenticated_client)
        _seed(authenticated_client, delete)

        # Its alerts resolve at the next check, and the card shows nothing meanwhile.
        report = _report(authenticated_client, project_id, check=False)
        assert (report["lines"], report["problems"], report["warnings"], report["muted"]) == ([], 0, 0, 0)
        _check(authenticated_client)
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/alerts?status=open").json()["open"] == 0


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

        report = _report(authenticated_client, project_id, **viewer_auth_headers)

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

    def test_only_an_open_alert_with_a_known_duration(
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

    def test_an_until_clear_mute_ends_once_its_line_stayed_clear_and_no_one_is_credited(
        self, authenticated_client: TestClient, failing_pool: tuple[str, list[Proxy]]
    ) -> None:
        project_id, proxies = failing_pool
        authenticated_client.put(_url(project_id, "/mutes"), json={"key": "pool_no_routable", "duration": "until_clear"})
        assert _report(authenticated_client, project_id)["muted"] == 1

        _statuses(authenticated_client, proxies, ProxyStatus.HEALTHY)

        assert _report(authenticated_client, project_id)["lines"] == []
        # Cleared within the reopen window: a flapping line would come back still muted.
        assert set(_stored_mutes(authenticated_client, project_id)) == {"pool_no_routable"}
        _check(authenticated_client, at=utc_now() + REOPEN_WINDOW + timedelta(seconds=5))
        assert _stored_mutes(authenticated_client, project_id) == {}
        # The pool fails again: the line is back, unmuted.
        _statuses(authenticated_client, proxies, ProxyStatus.UNHEALTHY)
        _check(authenticated_client, at=utc_now() + REOPEN_WINDOW + timedelta(minutes=1))
        report = _report(authenticated_client, project_id, check=False)
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
        _check(authenticated_client)
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
            rows = await runtime._compiler.read_project(project_id)
            sections = runtime._compiler.preview(rows, quarantined={}, blocked_until={})
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
        _check(authenticated_client)
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
        _check(authenticated_client)

        response = authenticated_client.get("/api/v1/attention/projects")

        assert response.status_code == 200
        counts = {row["project_id"]: row["problems"] for row in response.json()["projects"]}
        assert leaving not in counts
        assert [counts[project_id] for project_id in made] == [
            _report(authenticated_client, project_id, check=False)["problems"] for project_id in made
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
            # Two checks a minute apart: an install line shows once both have seen it.
            now = utc_now()
            _check(authenticated_client, at=now)
            _check(authenticated_client, at=now + timedelta(minutes=1))
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


class TestStoredLines:
    """The card read from the stored alerts alone: what a check stored, and when."""

    def test_a_project_never_checked_says_so_rather_than_all_clear(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        report = _report(authenticated_client, created_project["id"], check=False)

        assert (report["lines"], report["checked_at"], report["stale"]) == ([], None, False)

    def test_a_covered_line_is_off_the_card_and_its_counts_but_can_still_be_muted(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        _store(authenticated_client, utc_now(), [
            FoundAlert(project_id, _finding("connectors_off", Severity.PROBLEM)),
            FoundAlert(project_id, _finding("refusals:no_proxy_available", Severity.PROBLEM), hidden_by="connectors_off"),
        ], [project_id])

        report = _report(authenticated_client, project_id, check=False)
        assert ([line["key"] for line in report["lines"]], report["problems"]) == (["connectors_off"], 1)
        counts = authenticated_client.get("/api/v1/attention/projects").json()["projects"]
        assert {row["project_id"]: row["problems"] for row in counts}[project_id] == 1

        body = {"key": "refusals:no_proxy_available", "duration": "until_clear"}
        assert authenticated_client.put(_url(project_id, "/mutes"), json=body).status_code == 204

    def test_since_is_the_current_spell_and_the_opening_is_kept(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        opened = utc_now() - timedelta(minutes=30)
        found = [FoundAlert(project_id, _finding("connectors_off", Severity.PROBLEM))]
        _store(authenticated_client, opened, found, [project_id])
        _store(authenticated_client, opened + timedelta(minutes=5), [], [project_id])
        back = opened + timedelta(minutes=10)
        _store(authenticated_client, back, found, [project_id])

        (line,) = _report(authenticated_client, project_id, check=False)["lines"]

        assert (line["since"], line["opened_at"], line["reopened"]) == (back.isoformat(), opened.isoformat(), 1)

    def test_pending_from_a_change_until_a_check_runs_or_a_minute_passes(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        _check(authenticated_client)
        assert _report(authenticated_client, project_id, check=False)["pending"] is False

        # A project edit asks for a check now; no worker runs in this process.
        edit = authenticated_client.patch(f"/api/v1/projects/{project_id}", json={"description": "edited"})
        assert edit.status_code == 200, edit.text
        assert _report(authenticated_client, project_id, check=False)["pending"] is True

        _check(authenticated_client)
        assert _report(authenticated_client, project_id, check=False)["pending"] is False

        redis = api_runtime(authenticated_client).redis_client
        asked = (utc_now() - requests_module.PENDING_FOR - timedelta(seconds=1)).isoformat(timespec="microseconds")
        on_app_loop(authenticated_client, redis.client.hset, ALERT_CHECK_KEY.format(scope=project_id), "requested", asked)
        on_app_loop(authenticated_client, redis.client.hdel, ALERT_CHECK_KEY.format(scope=project_id), "checked")
        assert _report(authenticated_client, project_id, check=False)["pending"] is False

    def test_stale_once_the_last_check_is_three_minutes_old(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        project_id = created_project["id"]
        checks = api_runtime(authenticated_client).alert_checks
        on_app_loop(authenticated_client, checks.mark_checked, [project_id], utc_now() - CHECKS_STALE - timedelta(seconds=5))

        report = _report(authenticated_client, project_id, check=False)
        assert (report["stale"], report["checked_at"] is not None) == (True, True)
        assert _report(authenticated_client, project_id)["stale"] is False


class TestInstallLines:
    @pytest.fixture(autouse=True)
    def _no_install_alerts(self, authenticated_client: TestClient) -> None:
        """The app's Postgres outlives a test: start from no install alert."""

        async def clear(sessions: SessionFactory) -> None:
            async with sessions() as session:
                await session.execute(delete(AlertModel).where(AlertModel.project_id.is_(None)))
                await session.commit()

        _seed(authenticated_client, clear)

    def _install(self, client: TestClient) -> dict[str, Any]:
        response = client.get("/api/v1/attention/install")
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    def test_the_stored_lines_with_their_opening_problems_first(self, authenticated_client: TestClient) -> None:
        now = utc_now()
        at = now - timedelta(minutes=4)
        _store(authenticated_client, now, [
            FoundAlert("", _finding(
                "ip_databases", text="An IP database could not be updated.", link="geo", action="", sort_name="4"
            )),
            FoundAlert("", _finding(
                "changes_not_applied", text="Changes since {time} have not taken effect yet.", link="system", action="",
                when=_at(at), sort_name="2",
            )),
            FoundAlert("", _finding("no_gateway_serving", Severity.PROBLEM, link="system", action="", sort_name="0")),
        ], [""])
        on_app_loop(authenticated_client, api_runtime(authenticated_client).alert_checks.mark_checked, [INSTALL_SCOPE], now)

        body = self._install(authenticated_client)

        assert [line["key"] for line in body["lines"]] == ["no_gateway_serving", "changes_not_applied", "ip_databases"]
        changes = body["lines"][1]
        assert (changes["at"], changes["admin_page"], changes["since"]) == (at.isoformat(), "system", now.isoformat())
        assert body["lines"][2]["admin_page"] == "geo"
        assert body["checked_at"] == now.isoformat()

    def test_a_line_back_within_the_window_reads_since_it_came_back(self, authenticated_client: TestClient) -> None:
        opened = utc_now() - timedelta(minutes=30)
        found = [FoundAlert("", _finding("ip_databases", link="geo", action=""))]
        _store(authenticated_client, opened, found, [""])
        _store(authenticated_client, opened + timedelta(minutes=5), [], [""])
        back = opened + timedelta(minutes=12)
        _store(authenticated_client, back, found, [""])
        on_app_loop(authenticated_client, api_runtime(authenticated_client).alert_checks.mark_checked, [INSTALL_SCOPE], utc_now())

        (line,) = self._install(authenticated_client)["lines"]

        assert (line["key"], line["since"]) == ("ip_databases", back.isoformat())

    def test_postgres_out_of_reach_is_that_line_alone(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refused(self: AlertRepository) -> list[Any]:
            raise OperationalError("SELECT", {}, ConnectionRefusedError("Postgres is away"))

        monkeypatch.setattr(AlertRepository, "open_install", refused)

        body = self._install(authenticated_client)

        assert [(line["key"], line["since"]) for line in body["lines"]] == [("store_unreachable:database", None)]
        assert body["lines"][0]["text"].startswith("Octoprox cannot reach its database.")

    def test_redis_out_of_reach_is_a_line_beside_the_stored_ones(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _store(authenticated_client, utc_now(), [FoundAlert("", _finding("ip_databases", link="geo", action=""))], [""])

        async def refused(*_: Any) -> Any:
            raise RedisConnectionError("Redis is away")

        monkeypatch.setattr(requests_module.AlertChecks, "read", refused)

        body = self._install(authenticated_client)

        assert [line["key"] for line in body["lines"]] == ["ip_databases", "store_unreachable:live_data"]
        assert body["lines"][1]["since"] is None

    def test_checks_behind_say_when_they_last_ran(self, authenticated_client: TestClient) -> None:
        checked = utc_now() - CHECKS_STALE - timedelta(seconds=30)
        on_app_loop(authenticated_client, api_runtime(authenticated_client).alert_checks.mark_checked, [INSTALL_SCOPE], checked)

        (line,) = self._install(authenticated_client)["lines"]

        assert (line["key"], line["at"], line["since"]) == ("checks_behind", checked.isoformat(), None)
        assert line["text"] == "Octoprox last checked for problems at {time}, so this list may be out of date."

    def test_checks_never_run_with_work_waiting_say_the_job_is_not_running(self, authenticated_client: TestClient) -> None:
        redis = api_runtime(authenticated_client).redis_client
        stream = JOB_STREAM_KEY.format(kind=JobKind.ALERTS)

        async def entry(seconds_ago: float) -> None:
            seconds, microseconds = await redis.client.time()
            at = int(seconds) * 1000 + int(microseconds) // 1000 - int(seconds_ago * 1000)
            await redis.client.xadd(stream, {"key": "all"}, id=f"{at}-0")

        on_app_loop(authenticated_client, entry, 60)
        assert self._install(authenticated_client)["lines"] == []

        on_app_loop(authenticated_client, redis.client.delete, stream)
        on_app_loop(authenticated_client, entry, CHECKS_STALE.total_seconds() + 5)
        (line,) = self._install(authenticated_client)["lines"]
        assert line["key"] == "checks_not_running"
        assert line["text"].endswith("Check that a worker runs the alerts job.")


def _at(at: datetime) -> AttentionWhen:
    return AttentionWhen(kind="at", at=at)
