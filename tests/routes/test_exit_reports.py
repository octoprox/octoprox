# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""``POST /projects/{id}/exit-reports``: each report names a request by the proxy username it used, and its exit by the
exit headers of its response when they are complete, else by the username's ``-tag-``, from the note a gateway made of
that request. Each is resolved on its own, and the ones resolved are appended to the reports stream in one pipeline.

A row with IPs counts the report for its exit IP and connector; a dynamic connector alone, at the place the username
names. Any signed-in role may report, a viewer and a viewer service account's token included; the project's mode must
record.
"""

import uuid
from typing import Any

import pytest
import redis.asyncio as redis_lib
from fastapi.testclient import TestClient

from api.db.redis import EXIT_REPORT_STREAM_KEY
from api.jobs.messages import ExitReportMessage
from api.models.exit_reports import ExitVerdict
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.reputation.reports import NO_EXIT, NO_NOTE
from api.reputation.tag_note import TagNote
from tests.conftest import api_runtime, on_app_loop


class Install:
    """A project whose exit reputation records, its connectors and rows, and the notes gateways made of its tagged
    requests."""

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

    def row(self, connector_id: str, **metadata: str) -> None:
        """A proxy row of the connector, with ``metadata`` such as where it was last seen exiting."""
        proxy = Proxy(
            host=f"{uuid.uuid4().hex[:8]}.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector_id,
            status=ProxyStatus.HEALTHY,
            metadata=metadata,
        )
        on_app_loop(self.client, api_runtime(self.client).proxy_service.add, proxy)

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
    """The exit headers name the exit when complete: a dynamic connector's id alone, or a connector with IPs and the
    exit IP one of its rows was last seen exiting from. They win over the tag, whose note is then not read."""

    def test_a_dynamic_connectors_id_alone_counts_for_it_at_the_place_asked_for(self, install: Install) -> None:
        dynamic = install.connector("dynamic")
        install.row(dynamic, dynamic_sessions="true", discovered_ip="203.0.113.50")
        # The tag's note names another exit: complete headers win.
        install.note("t1", connector_id="elsewhere", ip="198.51.100.1")

        (result,) = install.results(
            _bad(f"{install.username}-cc-us-st-ny-tag-t1", connector_id=dynamic, ip="203.0.113.50")
        )

        # A dynamic connector's exits are per request: the IP it went out through is no record of its own.
        assert (result["status"], result["ip"], result["connector_id"], result["place"]) == (
            "accepted",
            None,
            dynamic,
            "US/NY",
        )
        (message,) = install.appended()
        assert (message.ip, message.proxy_id) == (None, None)
        assert message.connector_key is not None
        assert [key.place for key in message.connector_key.levels()] == ["US/NY", "US"]

    def test_a_connector_with_ips_and_where_one_of_its_rows_was_last_seen_exiting(self, install: Install) -> None:
        pool = install.connector("pool")
        install.row(pool, discovered_ip="203.0.113.7")
        install.row(pool, discovered_ip="::ffff:203.0.113.8")
        install.row(pool)

        results = install.results(
            # No tag: the headers alone name the exit.
            _bad(f"{install.username}-cc-us", connector_id=pool, ip=" 203.0.113.7 "),
            # A row's exit as an echo answered it, in its IPv4-mapped form, named in either spelling.
            _bad(install.username, connector_id=pool, ip="203.0.113.8"),
            _bad(install.username, connector_id=pool, ip="::ffff:203.0.113.8"),
        )

        assert [(r["status"], r["ip"], r["connector_id"], r["place"]) for r in results] == [
            ("accepted", "203.0.113.7", pool, ""),
            ("accepted", "203.0.113.8", pool, ""),
            ("accepted", "203.0.113.8", pool, ""),
        ]
        assert [(m.ip, m.connector_id, m.place) for m in install.appended()] == [
            ("203.0.113.7", pool, ""),
            ("203.0.113.8", pool, ""),
            ("203.0.113.8", pool, ""),
        ]

    def test_complete_headers_read_no_note(self, install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
        """The note is read only for a report the headers did not name: with notes unreadable, complete headers still
        count, and a tag alone cannot."""
        dynamic = install.connector("dynamic")
        install.row(dynamic, dynamic_sessions="true")
        pool = install.connector("pool")
        install.row(pool, discovered_ip="203.0.113.7")
        redis = api_runtime(install.client).redis_client

        async def unreadable(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("notes cannot be read")

        monkeypatch.setattr(redis.client, "mget", unreadable)
        results = install.results(
            _bad(f"{install.username}-cc-gb-tag-t1", connector_id=dynamic),
            _bad(f"{install.username}-tag-t2", connector_id=pool, ip="203.0.113.7"),
        )

        assert [(r["status"], r["connector_id"], r["ip"], r["place"]) for r in results] == [
            ("accepted", dynamic, None, "GB"),
            ("accepted", pool, "203.0.113.7", ""),
        ]
        assert install.report(_bad(f"{install.username}-tag-t1")).status_code == 503

    def test_headers_that_name_no_exit_leave_it_to_the_tag(self, install: Install) -> None:
        pool = install.connector("pool")
        install.row(pool, discovered_ip="203.0.113.7")
        install.note("t1", connector_id=pool, proxy_id="row-9", ip="203.0.113.9")

        (result,) = install.results(_bad(f"{install.username}-tag-t1", connector_id=pool, ip="198.51.100.99"))

        assert (result["status"], result["ip"], result["connector_id"]) == ("accepted", "203.0.113.9", pool)
        (message,) = install.appended()
        assert message.proxy_id == "row-9"

    def test_without_a_tag_the_refusal_says_why_the_headers_name_no_exit(
        self, install: Install, authenticated_client: TestClient
    ) -> None:
        pool = install.connector("pool")
        install.row(pool, discovered_ip="203.0.113.7")
        theirs = Install(authenticated_client).connector("theirs")
        cases = [
            ({"connector_id": pool}, f"connector {pool} has IPs, so ip is needed too"),
            ({"connector_id": pool, "ip": "198.51.100.99"}, f"no proxy of connector {pool} was last seen exiting from 198.51.100.99"),
            ({"connector_id": pool, "ip": "nope"}, "not an IP: nope"),
            ({"ip": "203.0.113.7"}, "ip needs connector_id"),
            ({"connector_id": theirs, "ip": "203.0.113.7"}, f"connector {theirs} is not in this project"),
            # Never looked up: Postgres refuses a NUL in text.
            ({"connector_id": "a\u0000b"}, "connector a\u0000b is not in this project"),
        ]

        results = install.results(*(_bad(install.username, **headers) for headers, _why in cases))

        assert [r["error"] for r in results] == [f"{NO_EXIT}; exit headers: {why}" for _headers, why in cases]
        assert install.appended() == []
        # With a tag no gateway noted lately, the note is missed for the same reason.
        assert install.error(_bad(f"{install.username}-tag-gone", connector_id=pool)) == (
            f"{NO_NOTE}; exit headers: connector {pool} has IPs, so ip is needed too"
        )


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
        ],
    )
    def test_a_report_that_breaks_the_format_is_a_422(self, install: Install, report: dict[str, Any]) -> None:
        assert install.report(report).status_code == 422
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
