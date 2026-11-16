# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""``POST /projects/{id}/exit-reports``: each report names a request by the proxy username it used, and its exit by the
exit headers of its response when it carries ``connector_id``, as sent, with the tag's note to fall back on, else by the
username's ``-tag-``, from the note a gateway made of that request. Each is named on its own, and the ones named are
appended to the reports stream in one pipeline; the workers check the exit headers against the project's rows.

The api reads no Postgres for it: its project comes from the process's project map. A row with IPs counts the report
for its exit IP and connector; a dynamic connector alone, at the place the username names. Any signed-in role may
report, a viewer and a viewer service account's token included; the project's mode must record.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
import redis.asyncio as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import event

from api.db.redis import EXIT_REPORT_STREAM_KEY
from api.jobs.messages import ExitReportMessage, NotedExit
from api.models.exit_reports import ExitVerdict
from api.models.project import ExitReputationMode, Project
from api.reputation.reports import IP_NEEDS_CONNECTOR, NO_EXIT, NO_NOTE, NOT_A_CONNECTOR
from api.reputation.tag_note import TagNote
from tests.conftest import api_runtime, on_app_loop
from tests.waits import until


class Install:
    """A project whose exit reputation records, its connectors, and the notes gateways made of its tagged requests."""

    def __init__(self, client: TestClient, mode: str = "record") -> None:
        self.client = client
        suffix = uuid.uuid4().hex[:8]
        self.username = f"rep_{suffix}"
        project = client.post(
            "/api/v1/projects",
            json={"name": f"Reports {suffix}", "username": self.username, "password": "pw", "exit_reputation": mode},
        )
        assert project.status_code == 201, project.text
        self.project_id: str = project.json()["id"]
        self._credential_id: str | None = None
        # The api takes the project from its project map, which reads it once the change entry of its create arrives.
        self.mapped(lambda project: project is not None and project.exit_reputation == mode)

    def mapped(self, holds: Callable[[Project | None], bool]) -> None:
        """Wait until what the api's project map holds for the project ``holds``."""
        projects = api_runtime(self.client).project_map
        on_app_loop(self.client, until, lambda: holds(projects.get(self.project_id)))

    def connector(self, name: str) -> str:
        """A connector of the project, by id."""
        if self._credential_id is None:
            credential = self.client.post(
                f"/api/v1/projects/{self.project_id}/credentials",
                json={"name": "Reports credential", "type": "static_proxy_provider", "config": {}},
            )
            assert credential.status_code == 201, credential.text
            self._credential_id = credential.json()["id"]
        connector = self.client.post(
            f"/api/v1/projects/{self.project_id}/connectors",
            json={"name": name, "credential_id": self._credential_id, "config": {}, "enabled": True},
        )
        assert connector.status_code == 201, connector.text
        connector_id: str = connector.json()["id"]
        return connector_id

    def note(self, tag: str, *, connector_id: str = "pool", proxy_id: str = "row-1", ip: str | None = None) -> None:
        """Note what the request tagged ``tag`` went through, as a gateway forwarding it does."""
        self.write(TagNote.key(self.project_id, tag), TagNote(connector_id=connector_id, proxy_id=proxy_id, ip=ip).model_dump_json())

    def write(self, key: str, value: str) -> None:
        redis = api_runtime(self.client).redis_client
        on_app_loop(self.client, redis.client.set, key, value)

    def report(self, *reports: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return self.client.post(
            f"/api/v1/projects/{self.project_id}/exit-reports", json={"reports": list(reports)}, headers=headers
        )

    def results(self, *reports: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.report(*reports)
        assert response.status_code == 202, response.text
        results: list[dict[str, Any]] = response.json()["results"]
        return results

    def error(self, report: dict[str, Any]) -> str:
        (result,) = self.results(report)
        assert result["status"] == "rejected", result
        error: str = result["error"]
        return error

    def appended(self) -> list[ExitReportMessage]:
        """The reports on the stream, oldest first."""
        redis = api_runtime(self.client).redis_client
        entries = on_app_loop(self.client, redis.client.xrange, EXIT_REPORT_STREAM_KEY)
        return [ExitReportMessage.model_validate_json(fields["item"]) for _id, fields in entries]


@pytest.fixture
def install(authenticated_client: TestClient) -> Install:
    return Install(authenticated_client)


def _bad(username: str, **fields: Any) -> dict[str, Any]:
    return {"username": username, "site": "www.google.com", "verdict": "bad", **fields}


class TestByTag:
    def test_a_row_with_ips_counts_for_its_exit_ip_and_its_connector(self, install: Install) -> None:
        install.note("job-1", connector_id="pool", proxy_id="row-1", ip="::ffff:203.0.113.7")

        results = install.results(_bad(f"{install.username}-cc-us-tag-job-1", reason="captcha"))

        assert results == [
            {
                "index": 0,
                "status": "accepted",
                "ip": "203.0.113.7",
                "site": "www.google.com",
                "connector_id": "pool",
                # A connector with IPs keeps its evidence at no place: its rows already depend on the place asked for.
                "place": "",
                "error": None,
            }
        ]
        (message,) = install.appended()
        assert (message.project_id, message.ip, message.site, message.verdict, message.place) == (
            install.project_id,
            "203.0.113.7",
            "www.google.com",
            ExitVerdict.BAD,
            "",
        )
        assert (message.proxy_id, message.connector_id, message.reason) == ("row-1", "pool", "captcha")
        # The admin fixture's session JWT: no token.
        assert (message.reporter_id, message.token_id) == ("test-admin-id", None)

    def test_a_dynamic_gateway_counts_for_its_connector_at_the_place_asked_for(self, install: Install) -> None:
        install.note("t1", connector_id="dynamic", proxy_id="gw")

        (located, untargeted) = install.results(
            _bad(f"{install.username}-cc-us-st-ny-city-new-york-tag-t1-sessid-s1"),
            _bad(f"{install.username}-tag-t1"),
        )

        assert (located["ip"], located["connector_id"], located["place"]) == (None, "dynamic", "US/NY/new_york")
        assert (untargeted["ip"], untargeted["connector_id"], untargeted["place"]) == (None, "dynamic", "")
        first, second = install.appended()
        assert first.connector_key is not None and second.connector_key is not None
        assert [key.place for key in first.connector_key.levels()] == ["US/NY/new_york", "US/NY", "US"]
        assert first.ip_key is None and [key.place for key in second.connector_key.levels()] == [""]

    def test_a_tag_no_gateway_noted_lately(self, install: Install) -> None:
        assert install.error(_bad(f"{install.username}-tag-nobody")) == NO_NOTE
        # A tag a session reuses is written again once half the note's 2 minutes have passed.
        assert NO_NOTE == "no recent request with this tag (a note is kept 1 to 2 minutes after the tag's last request)"
        # A note another release wrote, which this one cannot read, is none either.
        install.write(TagNote.key(install.project_id, "garbled"), "{not json")
        assert install.error(_bad(f"{install.username}-tag-garbled")) == NO_NOTE

    def test_another_projects_note_is_never_read(self, install: Install, authenticated_client: TestClient) -> None:
        other = Install(authenticated_client)
        other.note("shared")
        assert install.error(_bad(f"{install.username}-tag-shared")) == NO_NOTE

    def test_a_username_without_a_tag_names_no_exit(self, install: Install) -> None:
        """A session id never resolves a report, nor a pinned proxy."""
        for username in (f"{install.username}-sessid-s1", f"{install.username}-prxy-row-1", install.username):
            assert install.error(_bad(username)) == NO_EXIT
        assert NO_EXIT == "name the exit with a -tag- in the username, or with the exit headers"

    def test_another_projects_username(self, install: Install) -> None:
        install.note("t1")
        assert install.error(_bad("someone_else-tag-t1")) == "username is not this project's"

    def test_a_place_the_gateway_would_refuse(self, install: Install) -> None:
        install.note("t1")
        assert install.error(_bad(f"{install.username}-st-ny-tag-t1")).startswith("username: ")


class TestByExitHeaders:
    """A report that carries connector_id is named by its exit headers, as sent, and its tag's note goes with it: the
    workers check the exit against the project's rows, keeping a dynamic connector at the place asked for and a
    connector with IPs at none, and fall back on the note when the headers name no exit of them."""

    def test_the_exit_headers_are_taken_as_sent_with_the_tags_note_to_fall_back_on(self, install: Install) -> None:
        # The tag's note names another exit: the exit headers win, unchecked here.
        install.note("t1", connector_id="elsewhere", ip="198.51.100.1")

        results = install.results(
            _bad(f"{install.username}-cc-us-st-ny-tag-t1", connector_id="dynamic", ip=" ::ffff:203.0.113.50 "),
            _bad(f"{install.username}-cc-us", connector_id=" pool "),
        )

        assert [(r["status"], r["ip"], r["connector_id"], r["place"]) for r in results] == [
            ("accepted", "203.0.113.50", "dynamic", "US/NY"),
            ("accepted", None, "pool", "US"),
        ]
        first, second = install.appended()
        # No proxy row: what tells the workers to check the exit.
        assert (first.ip, first.connector_id, first.place, first.proxy_id) == ("203.0.113.50", "dynamic", "US/NY", None)
        assert (second.ip, second.connector_id, second.place, second.proxy_id) == (None, "pool", "US", None)
        assert first.by_headers and second.by_headers
        # The note of the first one's tag, for the workers to fall back on; the second carries no tag.
        assert first.noted == NotedExit(connector_id="elsewhere", proxy_id="row-1", ip="198.51.100.1")
        assert second.noted is None

    def test_what_only_the_projects_rows_can_tell_is_left_to_the_workers(
        self, install: Install, authenticated_client: TestClient
    ) -> None:
        """Another project's connector, one that does not exist, a connector with IPs without its exit IP or with one
        none of its rows was seen at: the workers drop those, and count them."""
        theirs = Install(authenticated_client).connector("theirs")
        pool = install.connector("pool")

        results = install.results(
            _bad(install.username, connector_id=theirs, ip="203.0.113.7"),
            _bad(install.username, connector_id="no-such-connector"),
            _bad(install.username, connector_id=pool),
            _bad(install.username, connector_id=pool, ip="198.51.100.99"),
        )

        assert [r["status"] for r in results] == ["accepted"] * 4
        assert [(m.connector_id, m.ip) for m in install.appended()] == [
            (theirs, "203.0.113.7"),
            ("no-such-connector", None),
            (pool, None),
            (pool, "198.51.100.99"),
        ]

    # Exit headers that can name no exit, whatever the project's rows say, and why.
    _UNNAMING = [
        ({"ip": "203.0.113.7"}, IP_NEEDS_CONNECTOR),
        ({"connector_id": "pool", "ip": "nope"}, "not an IP: nope"),
        # Only printable ASCII reaches the stream: Postgres refuses a NUL in text, which would fail the workers' batch.
        ({"connector_id": "a\u0000b"}, NOT_A_CONNECTOR),
        ({"connector_id": "a\tb", "ip": "203.0.113.7"}, NOT_A_CONNECTOR),
        ({"connector_id": "café"}, NOT_A_CONNECTOR),
    ]

    def test_exit_headers_that_name_no_exit_fall_back_on_the_tags_note(self, install: Install) -> None:
        install.note("t1", connector_id="pool", proxy_id="row-1", ip="198.51.100.23")

        results = install.results(*(_bad(f"{install.username}-tag-t1", **headers) for headers, _why in self._UNNAMING))

        assert [(r["status"], r["connector_id"], r["ip"], r["place"]) for r in results] == [
            ("accepted", "pool", "198.51.100.23", "")
        ] * len(self._UNNAMING)
        assert all(message.proxy_id == "row-1" and message.noted is None for message in install.appended())

    def test_exit_headers_that_name_no_exit_and_no_note_are_refused_with_both_reasons(self, install: Install) -> None:
        for username, reason in ((f"{install.username}-tag-nobody", NO_NOTE), (install.username, NO_EXIT)):
            results = install.results(*(_bad(username, **headers) for headers, _why in self._UNNAMING))
            assert [r["error"] for r in results] == [f"{reason}; exit headers: {why}" for _headers, why in self._UNNAMING]
        assert install.appended() == []

    def test_the_api_reads_no_postgres(self, install: Install) -> None:
        """Neither the project, which its project map holds, nor the connector and rows the exit headers name."""
        install.note("t1", connector_id="pool", ip="198.51.100.23")
        engine = install.client.app.state.node.engine  # type: ignore[attr-defined]
        reads: list[str] = []

        def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
            if any(table in statement for table in ("projects", "connectors", "proxies")):
                reads.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            results = install.results(
                _bad(f"{install.username}-tag-t1"),
                _bad(install.username, connector_id="pool", ip="203.0.113.7"),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

        assert [r["status"] for r in results] == ["accepted", "accepted"]
        assert reads == []

    def test_notes_that_cannot_be_read_fail_only_a_report_with_nothing_else_to_name_its_exit(
        self, install: Install, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With notes unreadable, reports their exit headers named still count, without the note to fall back on, and a
        tag alone cannot."""
        redis = api_runtime(install.client).redis_client

        async def unreadable(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("notes cannot be read")

        monkeypatch.setattr(redis.client, "mget", unreadable)
        results = install.results(
            _bad(f"{install.username}-cc-gb-tag-t1", connector_id="dynamic"),
            _bad(f"{install.username}-tag-t2", connector_id="pool", ip="203.0.113.7"),
        )

        assert [(r["status"], r["connector_id"], r["ip"], r["place"]) for r in results] == [
            ("accepted", "dynamic", None, "GB"),
            ("accepted", "pool", "203.0.113.7", ""),
        ]
        assert [message.noted for message in install.appended()] == [None, None]
        assert install.report(_bad(f"{install.username}-tag-t1")).status_code == 503


class TestSites:
    @pytest.mark.parametrize(
        ("site", "error"),
        [
            ("co.uk", "not a site: co.uk is a public suffix"),
            ("*.google.com", "not a site: *.google.com is not a host name"),
            ("", "site is empty"),
        ],
    )
    def test_a_site_that_names_none_is_refused(self, install: Install, site: str, error: str) -> None:
        install.note("t1")
        assert install.error(_bad(f"{install.username}-tag-t1", site=site)) == error

    def test_a_url_counts_for_its_host(self, install: Install) -> None:
        install.note("t1", ip="198.51.100.23")
        (result,) = install.results(_bad(f"{install.username}-tag-t1", site="https://Maps.Google.com:443/x?q=1"))
        assert result["site"] == "maps.google.com"


class TestTheBatch:
    def test_each_report_is_judged_on_its_own(self, install: Install) -> None:
        install.note("t1", ip="198.51.100.23")
        install.note("t2", ip="198.51.100.24")
        response = install.report(
            _bad(f"{install.username}-tag-t1"),
            _bad(f"{install.username}-tag-missing"),
            {"username": f"{install.username}-tag-t2", "site": "bing.com", "verdict": "good"},
        )

        assert response.status_code == 202
        body = response.json()
        assert (body["accepted"], body["rejected"]) == (2, 1)
        assert [(r["index"], r["status"]) for r in body["results"]] == [(0, "accepted"), (1, "rejected"), (2, "accepted")]
        assert [(m.ip, m.site, m.verdict) for m in install.appended()] == [
            ("198.51.100.23", "www.google.com", ExitVerdict.BAD),
            ("198.51.100.24", "bing.com", ExitVerdict.GOOD),
        ]

    def test_between_one_and_a_thousand_reports(self, install: Install) -> None:
        install.note("t1", ip="198.51.100.23")
        report = _bad(f"{install.username}-tag-t1")
        assert install.report().status_code == 422
        assert install.report(*[report] * 1001).status_code == 422
        response = install.report(*[report] * 1000)
        assert response.status_code == 202 and response.json()["accepted"] == 1000
        assert len(install.appended()) == 1000

    @pytest.mark.parametrize(
        "report",
        [
            {"site": "google.com", "verdict": "meh", "username": "x-tag-t1"},
            {"verdict": "bad", "username": "x-tag-t1"},
            # Every report carries the username the request used, exit headers or not.
            {"site": "google.com", "verdict": "bad", "connector_id": "pool", "ip": "198.51.100.23"},
            {"site": "google.com", "verdict": "bad", "username": "x-tag-t1", "reason": "x" * 65},
            # A NUL would fail the workers' whole batch of reports, every project's in it.
            {"site": "google.com", "verdict": "bad", "username": "x-tag-t1", "reason": "cap\u0000tcha"},
            {"site": "google.com", "verdict": "bad", "username": "x-tag-t1", "reason": "cap\ttcha"},
            # Longer than a connector id or an IP's text: neither can reach the stream.
            {"site": "google.com", "verdict": "bad", "username": "x-tag-t1", "connector_id": "c" * 37},
            {"site": "google.com", "verdict": "bad", "username": "x-tag-t1", "connector_id": "c", "ip": "1" * 46},
        ],
    )
    def test_a_report_that_breaks_the_format_is_a_422(self, install: Install, report: dict[str, Any]) -> None:
        assert install.report(report).status_code == 422
        assert install.appended() == []

    @pytest.mark.parametrize("field", ["connector_id", "ip"])
    def test_exit_headers_that_are_no_unicode_are_a_422(self, install: Install, field: str) -> None:
        """A lone surrogate, which JSON can carry and no stream entry can: it would fail the whole call's append."""
        report = (
            f'"username":"{install.username}-tag-t1","site":"google.com","verdict":"bad","connector_id":"c",'
            f'"{field}":"a\\ud800b"'
        )
        body = '{"reports":[{' + report + "}]}"
        response = install.client.post(
            f"/api/v1/projects/{install.project_id}/exit-reports",
            content=body.encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert install.appended() == []

    def test_a_project_whose_exit_reputation_is_off(self, authenticated_client: TestClient) -> None:
        install = Install(authenticated_client, mode="off")
        response = install.report(_bad(f"{install.username}-tag-t1"))
        assert response.status_code == 409
        assert response.json()["detail"] == "Exit reputation is off for this project"
        assert install.appended() == []

    def test_an_unknown_project(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/v1/projects/no-such-project/exit-reports", json={"reports": [_bad("x-tag-t1")]}
        )
        assert response.status_code == 404

    def test_the_project_map_follows_the_projects_writes(self, install: Install) -> None:
        """The api answers from its project map, which reads a project again once its change entry arrives."""
        install.note("t1", ip="198.51.100.23")
        report = _bad(f"{install.username}-tag-t1")

        switched = install.client.patch(f"/api/v1/projects/{install.project_id}", json={"exit_reputation": "off"})
        assert switched.status_code == 200, switched.text
        install.mapped(lambda project: project is not None and project.exit_reputation is ExitReputationMode.OFF)
        assert install.report(report).status_code == 409

        install.client.patch(f"/api/v1/projects/{install.project_id}", json={"exit_reputation": "avoid"})
        install.mapped(lambda project: project is not None and project.exit_reputation is ExitReputationMode.AVOID)
        assert install.report(report).status_code == 202

        deleted = install.client.request(
            "DELETE", f"/api/v1/projects/{install.project_id}", json={"confirmation": "permanently delete"}
        )
        assert deleted.status_code == 204, deleted.text
        install.mapped(lambda project: project is None)
        assert install.report(report).status_code == 404
        assert len(install.appended()) == 1

    def test_a_redis_that_cannot_be_reached_is_a_503(self, install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = api_runtime(install.client)
        down = redis_lib.from_url("redis://127.0.0.1:1", decode_responses=True)
        with monkeypatch.context() as patch:
            patch.setattr(runtime.redis_client, "_client", down)
            response = install.report(_bad(f"{install.username}-tag-t1"))
            assert response.status_code == 503, response.text
            assert response.headers["retry-after"] == "5"
        on_app_loop(install.client, down.aclose)
        assert install.appended() == []

    def test_a_redis_that_does_not_answer_in_time_is_a_503(
        self, install: Install, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis may have taken what it was sent before the deadline: the client sends the reports again, and a report
        taken twice counts twice, which "Remember for" absorbs."""
        install.note("t1", ip="198.51.100.23")
        redis = api_runtime(install.client).redis_client
        with monkeypatch.context() as patch:
            # Every call under the request deadline runs out of time at once.
            patch.setattr(redis, "_request_timeout", 0)
            response = install.report(_bad(f"{install.username}-tag-t1"))
            assert response.status_code == 503, response.text
        assert install.appended() == []


class TestWhoMayReport:
    def test_a_viewer(self, install: Install, viewer_auth_headers: dict[str, str]) -> None:
        install.note("t1", ip="198.51.100.23")
        response = install.report(_bad(f"{install.username}-tag-t1"), headers=viewer_auth_headers)
        assert response.status_code == 202, response.text
        (message,) = install.appended()
        assert (message.reporter_id, message.token_id) == ("test-viewer-id", None)

    def test_a_viewer_service_accounts_token(self, install: Install) -> None:
        install.note("t1", ip="198.51.100.23")
        client = install.client
        account = client.post(
            "/api/v1/users/service-accounts", json={"username": f"scraper-{uuid.uuid4().hex[:8]}", "role": "viewer"}
        ).json()
        token = client.post(f"/api/v1/users/{account['id']}/tokens", json={"name": "reports"}).json()

        response = install.report(
            _bad(f"{install.username}-tag-t1"), headers={"Authorization": f"Bearer {token['token']}"}
        )

        assert response.status_code == 202, response.text
        (message,) = install.appended()
        assert (message.reporter_id, message.token_id) == (account["id"], token["api_token"]["id"])
        client.request("DELETE", f"/api/v1/users/{account['id']}")

    def test_a_caller_who_is_not_signed_in(self, install: Install) -> None:
        response = install.report(_bad(f"{install.username}-tag-t1"), headers={"Authorization": "Bearer not-a-token"})
        assert response.status_code == 401
        assert install.appended() == []
