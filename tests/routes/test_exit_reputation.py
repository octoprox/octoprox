# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit reputation reads: a project's exit IP records, one IP's, per-IP totals, the traffic split for a site and
one site across every place, for any signed-in role.

Records are written as the workers write them, through the repository on the app's own database; times are relative to
now, since the status is what holds when the API answers.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from api.core import utc_now
from api.core.stats import RequestCount
from api.db import reputation_repository
from api.db.geo_repository import ExitSighting, ObservationRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models import exit_reputation
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from tests.conftest import api_runtime, on_app_loop


def _project(client: TestClient, data: dict[str, Any], mode: str = "record") -> dict[str, Any]:
    response = client.post("/api/v1/projects", json={**data, "exit_reputation": mode})
    assert response.status_code == 201, response.text
    return response.json()


def _apply(client: TestClient, when: datetime, batch: dict[ExitIpKey, ReportCounts]) -> None:
    """What a worker's reports ingest writes for ``batch``, applied at ``when``."""

    async def applied() -> None:
        async with api_runtime(client).session_factory() as session:
            await ExitReputationRepository(session).apply_reports(batch, when)
            await session.commit()

    on_app_loop(client, applied)


def _apply_connectors(client: TestClient, when: datetime, evidence: dict[ConnectorSiteKey, RequestCount]) -> None:
    """What the metrics flush writes for connector reports and requests, at every level of each key's place."""

    async def applied() -> None:
        async with api_runtime(client).session_factory() as session:
            await ExitReputationRepository(session).apply_evidence(
                {level: counts for key, counts in evidence.items() for level in key.levels()}, (), when, timedelta(minutes=1)
            )
            await session.commit()

    on_app_loop(client, applied)


def _sighted(client: TestClient, connector_id: str, ip: str, when: datetime) -> None:
    """What the observation ingest writes when a connector hands out ``ip``."""

    async def stored() -> None:
        async with api_runtime(client).session_factory() as session:
            await ObservationRepository(session).add_exit_ips(
                {(connector_id, ip): ExitSighting(first_seen=when, last_seen=when, source="health_check")}
            )
            await session.commit()

    on_app_loop(client, stored)


def _utc(value: datetime) -> str:
    return value.isoformat() + "Z"


def _records(client: TestClient, project_id: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{project_id}/exit-reputation", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def three(authenticated_client: TestClient, sample_project_data: dict[str, Any]) -> tuple[str, datetime]:
    """A recording project with an avoided record, one avoided before and one never, written half an hour ago."""
    project = _project(authenticated_client, sample_project_data)
    half_hour_ago = utc_now() - timedelta(minutes=30)

    def k(n: int, site: str = "google.com") -> ExitIpKey:
        return ExitIpKey(project["id"], f"203.0.113.{n}", site)

    _apply(authenticated_client, half_hour_ago - timedelta(hours=3), {k(3, "bing.com"): ReportCounts(bad=1)})
    _apply(
        authenticated_client,
        half_hour_ago,
        {k(1): ReportCounts(bad=1, reason="captcha"), k(2): ReportCounts(good=1), k(3, "bing.com"): ReportCounts(good=1)},
    )
    return project["id"], half_hour_ago


class TestList:
    def test_each_record_with_its_status_avoid_and_decayed_weights(
        self, authenticated_client: TestClient, three: tuple[str, datetime]
    ) -> None:
        project_id, half_hour_ago = three

        page = _records(authenticated_client, project_id)

        assert (page["total"], page["total_exact"], page["limit"], page["offset"]) == (3, True, 100, 0)
        by_ip = {record["ip"]: record for record in page["records"]}
        avoided = by_ip["203.0.113.1"]
        assert (avoided["site"], avoided["status"]) == ("google.com", "avoided")
        # One bad report avoids for "Avoid for", an hour at the default, and the answer's times are UTC with a zone.
        assert avoided["avoided_until"] == avoided["avoid_until"] == _utc(half_hour_ago + timedelta(hours=1))
        assert avoided["last_report_at"] == _utc(half_hour_ago) and avoided["last_reason"] == "captcha"
        assert avoided["bad_reports"] == 1 and 0.985 < avoided["weighted_bad"] < 0.986
        assert 0.985 < avoided["weighted_avoids"] < 0.986
        assert set(avoided) == {
            "ip", "site", "status", "avoided_until", "good_reports", "bad_reports", "weighted_good", "weighted_bad",
            "weighted_avoids", "avoid_until", "first_seen_at", "last_seen_at", "last_report_at", "last_reason",
        }
        good = by_ip["203.0.113.2"]
        assert (good["status"], good["avoided_until"], good["avoid_until"]) == ("none", None, None)
        before = by_ip["203.0.113.3"]
        # Avoided for an hour three hours before: the end stays, past.
        assert (before["status"], before["avoided_until"]) == ("none", None)
        assert before["avoid_until"] == _utc(half_hour_ago - timedelta(hours=2))

    def test_filters_and_sorts_apply_on_the_server(
        self, authenticated_client: TestClient, three: tuple[str, datetime]
    ) -> None:
        project_id, _ = three

        def ips(**params: Any) -> list[str]:
            return [record["ip"] for record in _records(authenticated_client, project_id, **params)["records"]]

        assert ips(status="avoided") == ["203.0.113.1"]
        assert sorted(ips(status="none")) == ["203.0.113.2", "203.0.113.3"]
        assert len(ips(status="any")) == 3
        assert ips(ip="203.0.113.2") == ["203.0.113.2"]
        assert len(ips(ip="203.0.")) == 3 and ips(ip="198.51.") == []
        assert ips(site="BING") == ["203.0.113.3"]
        assert ips(sort="reports")[0] == "203.0.113.3"
        for bad in ({"status": "failing"}, {"status": "good"}, {"sort": "failures"}):
            response = authenticated_client.get(f"/api/v1/projects/{project_id}/exit-reputation", params=bad)
            assert response.status_code == 422

    def test_paging_counts_only_so_far_past_the_page(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(reputation_repository, "COUNT_CAP", 3)
        project_id = _project(authenticated_client, sample_project_data)["id"]
        _apply(
            authenticated_client,
            utc_now(),
            {ExitIpKey(project_id, f"198.51.100.{n}", "google.com"): ReportCounts(bad=1) for n in range(6)},
        )

        first = _records(authenticated_client, project_id, limit=2)
        last = _records(authenticated_client, project_id, limit=2, offset=4)

        assert (len(first["records"]), first["total"], first["total_exact"]) == (2, 3, False)
        assert (len(last["records"]), last["total"], last["total_exact"]) == (2, 6, True)
        clamped = _records(authenticated_client, project_id, limit=0, offset=-5)
        assert (clamped["limit"], clamped["offset"], len(clamped["records"])) == (1, 0, 1)

    def test_a_project_with_nothing_recorded_has_an_empty_page(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data, mode="off")["id"]

        assert _records(authenticated_client, project_id) == {
            "records": [],
            "total": 0,
            "total_exact": True,
            "limit": 100,
            "offset": 0,
        }


class TestIp:
    def test_every_site_of_the_ip_and_the_connectors_that_served_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        sample_credential_data: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Another project's records and connectors for the same IP never show."""
        client = authenticated_client
        ip = "203.0.113.7"
        now = utc_now()
        mine, theirs = (
            _project(
                client,
                {
                    **sample_project_data,
                    "name": f"{sample_project_data['name']} {n}",
                    "username": f"{sample_project_data['username']}{n}",
                },
            )
            for n in (1, 2)
        )
        connectors = []
        for project in (mine, theirs):
            credential = client.post(
                f"/api/v1/projects/{project['id']}/credentials", json=sample_credential_data
            )
            assert credential.status_code == 201, credential.text
            connector = client.post(
                f"/api/v1/projects/{project['id']}/connectors",
                json={**sample_connector_data, "credential_id": credential.json()["id"]},
            )
            assert connector.status_code == 201, connector.text
            connectors.append(connector.json())
            _sighted(client, connector.json()["id"], ip, now - timedelta(hours=2))
        _apply(
            client,
            now - timedelta(minutes=30),
            {
                ExitIpKey(mine["id"], ip, "google.com"): ReportCounts(bad=1),
                ExitIpKey(theirs["id"], ip, "bing.com"): ReportCounts(bad=1),
            },
        )
        _apply(client, now, {ExitIpKey(mine["id"], ip, "yahoo.com"): ReportCounts(good=2)})

        response = client.get(f"/api/v1/projects/{mine['id']}/exit-reputation/ips/{ip}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["ip"], body["sites"], body["avoided_sites"]) == (ip, 2, 1)
        assert [(r["site"], r["status"]) for r in body["records"]] == [
            ("google.com", "avoided"),
            ("yahoo.com", "none"),
        ]
        assert body["connectors"] == [
            {
                "connector_id": connectors[0]["id"],
                "connector_name": connectors[0]["name"],
                "first_seen": _utc(now - timedelta(hours=2)),
                "last_seen": _utc(now - timedelta(hours=2)),
                "sightings": 1,
            }
        ]

    def test_an_ip_is_read_in_its_canonical_form(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        _apply(
            authenticated_client,
            utc_now(),
            {ExitIpKey(project_id, "2001:db8::1", "google.com"): ReportCounts(bad=1)},
        )

        written_out = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/ips/2001:0DB8:0:0::1"
        )
        nothing = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/ips/198.51.100.1"
        )
        not_an_ip = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/ips/example.com"
        )

        assert written_out.status_code == 200
        assert (written_out.json()["ip"], len(written_out.json()["records"])) == ("2001:db8::1", 1)
        assert nothing.json() == {
            "ip": "198.51.100.1",
            "sites": 0,
            "avoided_sites": 0,
            "records": [],
            "connectors": [],
        }
        assert (
            not_an_ip.status_code == 400 and not_an_ip.json()["detail"] == "Not a valid IP address"
        )

    def test_an_ip_of_a_wide_crawl_lists_the_sites_in_force_first(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only so many sites come back, the avoided before the newest; the totals still count them all."""
        monkeypatch.setattr(exit_reputation, "MAX_IP_SITES", 2)
        project_id = _project(authenticated_client, sample_project_data)["id"]
        ip = "203.0.113.5"
        now = utc_now()
        _apply(
            authenticated_client,
            now - timedelta(minutes=30),
            {ExitIpKey(project_id, ip, "google.com"): ReportCounts(bad=1)},
        )
        _apply(authenticated_client, now, {ExitIpKey(project_id, ip, site): ReportCounts(good=1) for site in ("bing.com", "ebay.com")})

        body = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/ips/{ip}"
        ).json()

        assert (body["sites"], body["avoided_sites"]) == (3, 1)
        assert [(r["site"], r["status"]) for r in body["records"]] == [
            ("google.com", "avoided"),
            ("bing.com", "none"),
        ]


class TestSummary:
    def test_per_ip_totals_for_the_ips_asked(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        client = authenticated_client
        mine = _project(client, sample_project_data)["id"]
        theirs = _project(
            client,
            {
                **sample_project_data,
                "name": f"{sample_project_data['name']} b",
                "username": f"{sample_project_data['username']}b",
            },
        )["id"]
        ip = "203.0.113.1"
        now = utc_now()
        _apply(
            client,
            now,
            {
                ExitIpKey(mine, ip, "google.com"): ReportCounts(bad=1),
                ExitIpKey(mine, ip, "bing.com"): ReportCounts(good=2),
                ExitIpKey(theirs, ip, "yahoo.com"): ReportCounts(bad=7),
            },
        )

        response = client.post(
            f"/api/v1/projects/{mine}/exit-reputation/summary",
            json={"ips": [ip, "::ffff:203.0.113.1", "198.51.100.1", "not-an-ip"]},
        )

        assert response.status_code == 200, response.text
        expected = {
            "good_reports": 2,
            "bad_reports": 1,
            "sites": 2,
            "avoided_sites": 1,
            "avoided_until": _utc(now + timedelta(hours=1)),
        }
        assert response.json() == {
            "ips": [{"ip": ip, **expected}, {"ip": "::ffff:203.0.113.1", **expected}]
        }

    def test_a_page_of_ips_at_most(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        url = f"/api/v1/projects/{project_id}/exit-reputation/summary"

        assert (
            authenticated_client.post(url, json={"ips": ["198.51.100.1"] * 1000}).status_code == 200
        )
        assert (
            authenticated_client.post(url, json={"ips": ["198.51.100.1"] * 1001}).status_code == 422
        )
        assert authenticated_client.post(url, json={"ips": []}).json() == {"ips": []}


class TestAccess:
    def test_every_signed_in_role_reads_them(
        self,
        authenticated_client: TestClient,
        three: tuple[str, datetime],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        project_id, _ = three
        base = f"/api/v1/projects/{project_id}/exit-reputation"
        for headers in (editor_auth_headers, viewer_auth_headers):
            assert authenticated_client.get(base, headers=headers).json()["total"] == 3
            assert (
                authenticated_client.get(f"{base}/ips/203.0.113.1", headers=headers).status_code
                == 200
            )
            summary = authenticated_client.post(
                f"{base}/summary", json={"ips": ["203.0.113.1"]}, headers=headers
            )
            assert summary.status_code == 200 and summary.json()["ips"][0]["bad_reports"] == 1
        assert (
            authenticated_client.get(base, headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )

    def test_an_unknown_project_is_404(self, authenticated_client: TestClient) -> None:
        base = "/api/v1/projects/missing/exit-reputation"

        assert authenticated_client.get(base).status_code == 404
        assert authenticated_client.get(f"{base}/ips/203.0.113.1").status_code == 404
        assert authenticated_client.post(f"{base}/summary", json={"ips": []}).status_code == 404

    def test_which_refusal_comes_first_and_the_404_detail(self, authenticated_client: TestClient) -> None:
        """A site or place that names none is refused before the project is looked up; an IP only after."""
        base = "/api/v1/projects/missing/exit-reputation"
        client = authenticated_client
        missing = (404, {"detail": "Project not found"})
        not_a_site = (400, {"detail": "not a site: co.uk is a public suffix"})

        def answer(response: Any) -> tuple[int, Any]:
            return response.status_code, response.json()

        assert answer(client.get(f"{base}/places", params={"site": "co.uk"})) == not_a_site
        assert answer(client.get(f"{base}/places/history", params={"site": "co.uk"})) == not_a_site
        assert answer(client.get(f"{base}/places/history", params={"site": "google.com", "place": "US/NY/a/b"})) == (
            400, {"detail": "not a place: US/NY/a/b"}
        )
        preview = {"strength": 2, "floor": 10}
        assert answer(client.get(f"{base}/strength-preview", params={**preview, "site": "co.uk"})) == not_a_site
        assert answer(client.get(f"{base}/ips/example.com")) == missing
        for response in (
            client.get(base),
            client.get(f"{base}/ips/203.0.113.1"),
            client.post(f"{base}/summary", json={"ips": []}),
            client.get(f"{base}/places"),
            client.get(f"{base}/places/history", params={"site": "google.com"}),
            client.get(f"{base}/strength-preview", params=preview),
        ):
            assert answer(response) == missing


class TestFilterEdges:
    def test_a_limit_past_a_thousand_is_a_thousand_and_blank_filters_do_not_narrow(
        self, authenticated_client: TestClient, three: tuple[str, datetime]
    ) -> None:
        project_id, _ = three

        page = _records(authenticated_client, project_id, limit=5000, ip="  ", site=" ")

        assert (page["limit"], page["total"], len(page["records"])) == (1000, 3, 3)

    def test_the_connectors_of_an_ip_the_one_that_saw_it_last_first(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        sample_credential_data: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        client = authenticated_client
        project_id = _project(client, sample_project_data)["id"]
        credential = client.post(f"/api/v1/projects/{project_id}/credentials", json=sample_credential_data)
        assert credential.status_code == 201, credential.text
        ip, now = "203.0.113.7", utc_now()
        ids = []
        for name, age in (("Earlier", timedelta(hours=2)), ("Later", timedelta(hours=1))):
            connector = client.post(
                f"/api/v1/projects/{project_id}/connectors",
                json={**sample_connector_data, "name": name, "credential_id": credential.json()["id"]},
            )
            assert connector.status_code == 201, connector.text
            ids.append(connector.json()["id"])
            _sighted(client, connector.json()["id"], ip, now - age)

        body = client.get(f"/api/v1/projects/{project_id}/exit-reputation/ips/{ip}").json()

        assert [(c["connector_id"], c["connector_name"]) for c in body["connectors"]] == [
            (ids[1], "Later"), (ids[0], "Earlier"),
        ]

    def test_an_ip_asked_twice_in_the_same_text_is_answered_once(
        self, authenticated_client: TestClient, three: tuple[str, datetime]
    ) -> None:
        project_id, _ = three

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/exit-reputation/summary", json={"ips": ["203.0.113.2", "203.0.113.2"]}
        )

        assert [row["ip"] for row in response.json()["ips"]] == ["203.0.113.2"]


class TestProxyExitIp:
    def test_a_row_names_the_ip_its_records_are_kept_under(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """A static row by its last seen exit; a dynamic gateway exits elsewhere on every request, so it names none."""
        rows = {
            "static": {"discovered_ip": "::ffff:203.0.113.9"},
            "gateway": {"discovered_ip": "203.0.113.8", "dynamic_sessions": "true"},
            "unseen": {},
        }
        ids = {}
        for name, metadata in rows.items():
            proxy = Proxy(
                host=f"{name}.example.com",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                connector_id=created_connector["id"],
                status=ProxyStatus.HEALTHY,
                metadata=metadata,
            )
            on_app_loop(
                authenticated_client, api_runtime(authenticated_client).proxy_service.add, proxy
            )
            ids[proxy.id] = name

        listed = authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/proxies"
        ).json()["proxies"]

        assert {ids[p["id"]]: p["exit_ip"] for p in listed if p["id"] in ids} == {
            "static": "203.0.113.9",
            "gateway": None,
            "unseen": None,
        }


class Split:
    """A project with a pool of four rows with IPs in New York (weight 1), two of them avoided for www.google.com, and an
    Oxylabs dynamic connector (weight 2) blocked on 5 of 20 requests there for US/NY and on 2 of 40 for requests naming
    no place. ``pool_routing`` adds to the pool's routing config."""

    SITE = "www.google.com"

    def __init__(
        self, client: TestClient, data: dict[str, Any], mode: str = "avoid", *, pool_routing: dict[str, Any] | None = None
    ) -> None:
        self.client = client
        self.project_id: str = _project(client, data, mode)["id"]
        static = self._credential("Split", "static_proxy_provider", {})
        oxylabs = self._credential("Oxylabs", "oxylabs", {"proxy_type": "residential", "username": "alice", "password": "s3cret"})
        self.pool = self._connector(static, "Pool", {}, {"weight": 1, **(pool_routing or {})})
        self.dynamic = self._connector(oxylabs, "Dynamic", {"session_mode": "dynamic", "session_duration_minutes": 10}, {"weight": 2})
        for n in range(1, 5):
            self._row(self.pool, discovered_ip=f"203.0.113.{n}", country="US", state_code="NY")
        self._row(self.dynamic, provider="oxylabs", proxy_type="residential", dynamic_sessions="true")
        now = self.reported_at = utc_now()
        _apply(
            client,
            now,
            {ExitIpKey(self.project_id, f"203.0.113.{n}", self.SITE): ReportCounts(bad=1) for n in (1, 2)},
        )
        _apply_connectors(
            client,
            now,
            {
                self.key(self.pool): RequestCount(blocked=2),
                self.key(self.dynamic, "US/NY"): RequestCount(requests=20, blocked=5),
                self.key(self.dynamic): RequestCount(requests=40, blocked=2),
                ConnectorSiteKey(self.project_id, self.pool, "bing.com", ""): RequestCount(good=1),
            },
        )

    def key(self, connector_id: str, place: str = "") -> ConnectorSiteKey:
        return ConnectorSiteKey(self.project_id, connector_id, self.SITE, place)

    def _credential(self, name: str, type: str, config: dict[str, Any]) -> str:
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/credentials", json={"name": name, "type": type, "config": config}
        )
        assert response.status_code == 201, response.text
        credential_id: str = response.json()["id"]
        return credential_id

    def _connector(self, credential_id: str, name: str, config: dict[str, Any], routing: dict[str, Any]) -> str:
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/connectors",
            json={"name": name, "credential_id": credential_id, "config": config, "routing_config": routing},
        )
        assert response.status_code == 201, response.text
        connector_id: str = response.json()["id"]
        return connector_id

    def _row(self, connector_id: str, **metadata: str) -> None:
        proxy = Proxy(
            host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
            connector_id=connector_id, status=ProxyStatus.HEALTHY, metadata=metadata,
        )
        on_app_loop(self.client, api_runtime(self.client).proxy_service.add, proxy)

    def split(self, **params: str) -> dict[str, Any]:
        response = self.client.get(f"/api/v1/projects/{self.project_id}/metrics/traffic-split", params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def by_name(self, **params: str) -> dict[str, dict[str, Any]]:
        return {connector["name"]: connector for connector in self.split(**params)["connectors"]}


REPUTATION_FIELDS = (
    "reputation", "reputation_kind", "reputation_place", "blocked", "requests", "usable_ips", "eligible_ips",
    "effective_weight", "effective_share",
)


def _reputation(connector: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(connector[field] for field in REPUTATION_FIELDS)


class TestTrafficSplit:
    """``GET /metrics/traffic-split?site=&place=``: each connector's reputation for a site, read from the project's
    records, and the share of the site's requests its effective weight takes."""

    def test_each_connector_weighed_for_the_site_at_the_place(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = split.split(site="https://WWW.Google.com/search", place="us/ny")

        assert (body["site"], body["place"], body["reputation_applied"]) == ("www.google.com", "US/NY", True)
        connectors = {connector["name"]: connector for connector in body["connectors"]}
        # Two of the pool's four rows avoided: x0.5. The dynamic connector's 5 blocked of 20 for US/NY: x0.75.
        assert _reputation(connectors["Pool"]) == (0.5, "usable_ips", None, 2, 0, 2, 4, 0.5, 25.0)
        assert _reputation(connectors["Dynamic"]) == (0.75, "block_rate", "US/NY", 5, 20, None, None, 1.5, 75.0)
        # The configured split is unchanged beside it.
        assert [connectors[name]["expected_share"] for name in ("Dynamic", "Pool")] == [66.67, 33.33]
        # Every report counts once, at its place's coarsest level: 2 + 2 + 5 on the site, 1 on bing.com.
        assert body["reputation_sites"] == [{"site": "www.google.com", "reports": 9}, {"site": "bing.com", "reports": 1}]
        assert body["reputation_places"] == [
            {"place": "US", "label": "United States", "reports": 5},
            {"place": "US/NY", "label": "New York, United States", "reports": 5},
        ]

    def test_a_dynamic_connector_takes_the_finest_level_of_the_place_that_has_a_factor(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        # A state with no record falls back to its country.
        assert _reputation(split.by_name(site=Split.SITE, place="US/CA")["Dynamic"])[:5] == (
            0.75, "block_rate", "US", 5, 20
        )
        # A request naming no place has its own record: 2 of 40.
        assert _reputation(split.by_name(site=Split.SITE)["Dynamic"])[:5] == (0.95, "block_rate", "", 2, 40)
        # A country with none keeps the weight: there is no figure for all countries to fall back on. The pool's rows are
        # all in New York, so the requests from Germany go to the dynamic connector alone.
        from_germany = split.by_name(site=Split.SITE, place="DE")
        assert _reputation(from_germany["Dynamic"]) == (1.0, "block_rate", None, None, None, None, None, 2.0, 100.0)
        assert _reputation(from_germany["Pool"]) == (1.0, "usable_ips", None, 2, 0, 0, 0, 0.0, 0.0)

    def test_without_a_site_the_effective_split_is_the_configured_one(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = split.split(place="US/NY")

        assert (body["site"], body["place"], body["reputation_applied"], body["reputation_places"]) == (
            None, None, False, []
        )
        for connector in body["connectors"]:
            assert (connector["reputation"], connector["reputation_kind"], connector["blocked"]) == (1.0, None, None)
            assert connector["effective_share"] == connector["expected_share"]
            assert connector["effective_weight"] == connector["weight"]

    def test_a_project_that_records_shows_the_reputation_and_routes_by_the_weights(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data, mode="record")

        body = split.split(site=Split.SITE, place="US/NY")

        assert body["reputation_applied"] is False
        connectors = {connector["name"]: connector for connector in body["connectors"]}
        assert _reputation(connectors["Dynamic"]) == (0.75, "block_rate", "US/NY", 5, 20, None, None, 2.0, 66.67)
        assert _reputation(connectors["Pool"]) == (0.5, "usable_ips", None, 2, 0, 2, 4, 1.0, 33.33)

    def test_a_connector_the_sites_domain_filter_refuses_takes_none_of_its_requests(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data, pool_routing={"domain_blacklist": ["google.com"]})

        connectors = split.by_name(site=Split.SITE, place="US/NY")

        # Routing keeps the pool off the site's requests: none of its rows is eligible, whatever is avoided.
        assert _reputation(connectors["Pool"]) == (1.0, "usable_ips", None, 2, 0, 0, 0, 0.0, 0.0)
        assert _reputation(connectors["Dynamic"])[-2:] == (1.5, 100.0)
        # Its share of the requests naming no site is as configured.
        assert connectors["Pool"]["expected_share"] == 33.33
        assert split.by_name(site="www.bing.com")["Pool"]["effective_share"] == 33.33

    def test_a_reserving_connector_takes_every_request_for_its_country_and_none_without_one(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        """The reason stays the untargeted one, while the shares are those of the request to the site from the place."""
        split = Split(authenticated_client, sample_project_data, pool_routing={"reserved_countries": ["US"]})

        from_new_york = split.by_name(site=Split.SITE, place="US/NY")
        no_place = split.by_name(site=Split.SITE)

        assert from_new_york["Pool"]["excluded_reason"] == "reserved_only"
        assert _reputation(from_new_york["Pool"]) == (0.5, "usable_ips", None, 2, 0, 2, 4, 0.5, 100.0)
        assert _reputation(from_new_york["Dynamic"])[-2:] == (0.0, 0.0)
        assert _reputation(no_place["Pool"])[-4:] == (0, 0, 0.0, 0.0)
        assert _reputation(no_place["Dynamic"])[-2:] == (1.9, 100.0)

    @pytest.mark.parametrize(
        ("params", "error"),
        [
            ({"site": "co.uk"}, "not a site: co.uk is a public suffix"),
            ({"site": "www.google.com", "place": "usa"}, "Country must be a two-letter ISO 3166-1 code, such as -cc-us"),
            ({"site": "www.google.com", "place": "US/NY/new_york/x"}, "not a place: US/NY/new_york/x"),
        ],
    )
    def test_a_site_or_place_that_names_none_is_a_400(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        params: dict[str, str],
        error: str,
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data, "avoid")["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/traffic-split", params=params)
        assert (response.status_code, response.json()["detail"]) == (400, error)

    def test_every_site_and_place_is_listed(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {
                **{split.key(split.dynamic, f"US//city_{n:02}"): RequestCount(good=1) for n in range(25)},
                **{
                    ConnectorSiteKey(split.project_id, split.pool, f"site{n:02}.example.com", ""): RequestCount(good=1)
                    for n in range(25)
                },
            },
        )

        body = split.split(site=Split.SITE)

        assert len(body["reputation_sites"]) == 27
        # The country, the state and every city, the most reported first.
        assert len(body["reputation_places"]) == 27
        assert body["reputation_places"][-1] == {"place": "US//city_24", "label": "City 24, United States", "reports": 1}


class TestPlaces:
    """``GET /exit-reputation/places?site=``: one site across every place its records name, each connector at each."""

    @staticmethod
    def places(split: Split, **params: str) -> dict[str, Any]:
        response = split.client.get(f"/api/v1/projects/{split.project_id}/exit-reputation/places", params=params)
        assert response.status_code == 200, response.text
        # A field without a value is left out, never sent as null.
        assert "null" not in response.text, response.text
        body: dict[str, Any] = response.json()
        return body

    @staticmethod
    def cells(body: dict[str, Any], split: Split) -> dict[str, dict[str, dict[str, Any]]]:
        """Place -> connector name -> its cell there, the times left out."""
        names = {split.pool: "Pool", split.dynamic: "Dynamic"}
        return {
            row["place"]: {
                names[connector_id]: {key: value for key, value in cell.items() if key != "counted_at"}
                for connector_id, cell in row["cells"].items()
            }
            for row in body["places"]
        }

    def test_every_place_of_the_busiest_site_with_each_connector(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        # A thin record from New York City: too few requests for a factor of its own.
        _apply_connectors(authenticated_client, utc_now(), {split.key(split.dynamic, "US/NY/new_york"): RequestCount(requests=5, blocked=1)})

        body = self.places(split)

        assert (body["site"], body["mode"], body["reputation_applied"]) == ("www.google.com", "avoid", True)
        assert body["sites"] == [{"site": "www.google.com", "reports": 10}, {"site": "bing.com", "reports": 1}]
        assert [
            (row["place"], row["label"], row["level"], row.get("parent"), row["reports"], row.get("cities_lowered"), row.get("cities_total"))
            for row in body["places"]
        ] == [
            # The dynamic connector's own record for requests naming no place: its 2 reports, never the pool's.
            ("", "No place named", "none", None, 2, None, None),
            ("US", "United States", "country", None, 6, 0, 1),
            ("US/NY", "New York, United States", "state", "US", 6, None, None),
            ("US/NY/new_york", "New York, NY", "city", "US/NY", 1, None, None),
        ]
        cells = self.cells(body, split)
        # Two of the pool's four rows avoided: x0.5 wherever it takes the requests; its counts, for the site as a whole,
        # are left out.
        pool = {"state": "own", "reputation": 0.5, "reputation_kind": "usable_ips", "usable_ips": 2, "eligible_ips": 4}
        # The state's 5 of 20 and the city's 1 of 5 added a moment later, before Connector memory's minutes faded
        # anything: 6 of 25. The only judged dynamic connector is the best one there.
        best = {"success": 0.75, "best_success": 0.75, "best_connector_id": split.dynamic}
        assert cells["US/NY"] == {
            "Dynamic": {
                "state": "own", "reputation": 0.75, "reputation_kind": "block_rate", "reputation_place": "US/NY",
                "own_factor": 0.75, "blocked": 6, "requests": 25, "configured_share": 66.67, "effective_share": 75.0,
            } | best,
            "Pool": pool | {"configured_share": 33.33, "effective_share": 25.0},
        }
        # The city's own thin record, while its factor comes from the state; the pool knows no row in the city.
        assert cells["US/NY/new_york"] == {
            "Dynamic": {
                "state": "inherited", "reputation": 0.75, "reputation_kind": "block_rate", "reputation_place": "US/NY",
                "blocked": 1, "requests": 5, "configured_share": 100.0, "effective_share": 100.0,
            } | best,
            "Pool": pool | {"state": "takes_none", "reputation": 1.0, "usable_ips": 0, "eligible_ips": 0}
            | {"configured_share": 0.0, "effective_share": 0.0},
        }
        assert cells[""]["Dynamic"]["reputation"] == 0.95
        rows = {row["place"]: row for row in body["places"]}
        assert (rows["US/NY/new_york"]["lowest"], rows["US/NY/new_york"]["lowered"]) == (
            {"value": 0.75, "inherited": True}, False
        )
        assert (rows["US"]["lowest"], rows["US"]["lowered"], rows["US"]["not_judged"]) == (
            {"value": 0.5, "inherited": False}, True, False
        )
        dynamic, pool_connector = body["connectors"]
        assert (dynamic["name"], dynamic["dynamic"], "excluded_reason" in dynamic) == ("Dynamic", True, False)
        assert dynamic["tile"] == {
            "lowered_countries": 1, "countries": 1, "lowered_states": 1, "states": 1, "lowered_cities": 0, "cities": 0,
            "buckets": {"heavy": 0, "lowered": 3, "inherited": 1, "full": 0, "not_judged": 0},
        }
        assert (pool_connector["tile"]["usable_ips"], pool_connector["tile"]["eligible_ips"], pool_connector["tile"]["reputation"]) == (
            2, 4, 0.5
        )
        # x0.5 for the pool at no place, in the United States and in New York state: the finest place.
        assert body["summary"] == {
            "lowered_connectors": 2, "serving_connectors": 2,
            "hardest": {"connector_id": split.pool, "place": "US/NY", "value": 0.5},
        }

    def test_the_states_a_connector_has_at_a_place(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data, mode="record")
        site = "shop.example.com"
        # Too few requests from Germany for a factor; one of the pool's rows avoided for the site.
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {ConnectorSiteKey(split.project_id, split.dynamic, site, "DE"): RequestCount(requests=5, blocked=1)},
        )
        _apply(authenticated_client, utc_now(), {ExitIpKey(split.project_id, "203.0.113.3", site): ReportCounts(bad=1)})

        body = self.places(split, site=f"https://{site}/cart")

        assert (body["site"], body["mode"], body["reputation_applied"]) == (site, "record", False)
        assert {"site": site, "reports": 1} in body["sites"]
        # Germany for its record; the United States for the pool's avoided row there; no place named for neither.
        assert [(row["place"], row["reports"], row["not_judged"]) for row in body["places"]] == [
            ("DE", 1, True), ("US", 0, False)
        ]
        states = {place: {name: cell["state"] for name, cell in at.items()} for place, at in self.cells(body, split).items()}
        assert states == {
            "DE": {"Dynamic": "not_judged", "Pool": "takes_none"},
            "US": {"Dynamic": "no_reports", "Pool": "own"},
        }
        us = self.cells(body, split)["US"]
        # Record mode: the share is what Avoid would make of it.
        assert (us["Pool"]["reputation"], us["Pool"]["configured_share"], us["Pool"]["effective_share"]) == (0.75, 33.33, 27.27)
        assert body["summary"]["hardest"] == {"connector_id": split.pool, "place": "US", "value": 0.75}

    def test_a_quiet_place_shows_its_counts_as_last_decided(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        then = utc_now() - timedelta(hours=3)
        _apply_connectors(
            authenticated_client,
            then,
            {ConnectorSiteKey(split.project_id, split.dynamic, "quiet.example.com", "GB"): RequestCount(requests=20, blocked=8)},
        )

        body = self.places(split, site="quiet.example.com")

        cell = body["places"][0]["cells"][split.dynamic]
        # Three hours of "Remember for" would leave 7 and 18 of them; the counts show as decided.
        assert (cell["state"], cell["reputation"], cell["blocked"], cell["requests"]) == ("own", 0.6, 8, 20)
        assert cell["counted_at"] == then.replace(tzinfo=None).isoformat() + "Z"

    def test_countries_only_as_a_project_naming_countries_sends_them(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        site = "countries.example.com"
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {
                ConnectorSiteKey(split.project_id, split.dynamic, site, country): RequestCount(
                    requests=20, blocked=blocked, good=good
                )
                for country, blocked, good in (("GB", 2, 0), ("DE", 10, 0), ("FR", 0, 1))
            },
        )

        body = self.places(split, site=site)

        # No city listed: no city counts either.
        assert [(row["place"], row["level"], row.get("parent"), row.get("cities_total")) for row in body["places"]] == [
            ("DE", "country", None, None), ("GB", "country", None, None), ("FR", "country", None, None)
        ]
        tile = body["connectors"][0]["tile"]
        assert (tile["lowered_countries"], tile["countries"], tile["cities"]) == (2, 3, 0)
        assert body["summary"]["hardest"] == {"connector_id": split.dynamic, "place": "DE", "value": 0.5}

    def test_cities_under_their_states_and_countries(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        site = "cities.example.com"
        # One flush each, as their levels overlap: x0.5, x1, x0.8, x1 and x0.9.
        for place, blocked, good in (
            ("US/NY/new_york", 10, 0), ("US/NY/buffalo", 0, 1), ("US//austin", 4, 0), ("GB//london", 0, 1),
            ("GB//leeds", 2, 0),
        ):
            key = ConnectorSiteKey(split.project_id, split.dynamic, site, place)
            _apply_connectors(authenticated_client, utc_now(), {key: RequestCount(requests=20, blocked=blocked, good=good)})

        body = self.places(split, site=site)

        assert [(row["place"], row["label"], row["reports"]) for row in body["places"]] == [
            ("US", "United States", 15),
            ("US/NY", "New York, United States", 11),
            ("US/NY/new_york", "New York, NY", 10),
            ("US/NY/buffalo", "Buffalo, NY", 1),
            ("US//austin", "Austin, United States", 4),
            ("GB", "United Kingdom", 3),
            ("GB//leeds", "Leeds, United Kingdom", 2),
            ("GB//london", "London, United Kingdom", 1),
        ]
        rows = {row["place"]: row for row in body["places"]}
        assert (rows["US"]["cities_lowered"], rows["US"]["cities_total"]) == (2, 3)
        assert (rows["GB"]["cities_lowered"], rows["GB"]["cities_total"]) == (1, 2)

    def test_the_traffic_split_agrees_at_every_place(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = self.places(split, site=Split.SITE)

        for row in body["places"]:
            shown = split.by_name(site=Split.SITE, place=row["place"]) if row["place"] else split.by_name(site=Split.SITE)
            for connector in body["connectors"]:
                cell, there = row["cells"][connector["connector_id"]], shown[connector["name"]]
                assert (
                    cell["state"], cell["reputation"], cell.get("reputation_place"), cell.get("usable_ips"), cell["effective_share"]
                ) == (
                    there["reputation_state"], there["reputation"], there["reputation_place"], there["usable_ips"],
                    there["effective_share"],
                ), row["place"]

    def test_an_asked_site_without_records_is_kept_first(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = self.places(split, site="new.example.com")

        assert body["sites"] == [
            {"site": "new.example.com", "reports": 0}, {"site": "www.google.com", "reports": 9},
            {"site": "bing.com", "reports": 1},
        ]
        assert (body["site"], body["places"], body["summary"].get("hardest")) == ("new.example.com", [], None)
        assert [connector["name"] for connector in body["connectors"]] == ["Dynamic", "Pool"]
        # A blank site is the busiest one.
        assert self.places(split, site=" ")["site"] == "www.google.com"

    def test_the_default_site_is_the_busiest_with_a_place_to_list(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        now = utc_now()
        # The busiest site, reported through the pool alone: it avoids an IP none of the pool's rows exits from.
        _apply_connectors(
            authenticated_client,
            now,
            {ConnectorSiteKey(split.project_id, split.pool, "plain.example.com", ""): RequestCount(blocked=30)},
        )
        _apply(authenticated_client, now, {ExitIpKey(split.project_id, "198.51.100.9", "plain.example.com"): ReportCounts(bad=1)})

        # Nothing to list there: the next busiest site with a dynamic connector's records.
        assert self.places(split)["site"] == Split.SITE

        # A busier site than that, through the pool alone, avoiding one of its rows: its country is listed.
        _apply_connectors(
            authenticated_client,
            now,
            {ConnectorSiteKey(split.project_id, split.pool, "avoids.example.com", ""): RequestCount(blocked=20)},
        )
        _apply(authenticated_client, now, {ExitIpKey(split.project_id, "203.0.113.3", "avoids.example.com"): ReportCounts(bad=1)})

        body = self.places(split)

        assert [listed["site"] for listed in body["sites"]] == [
            "plain.example.com", "avoids.example.com", Split.SITE, "bing.com"
        ]
        assert (body["site"], [row["place"] for row in body["places"]]) == ("avoids.example.com", ["US"])
        # Asked for, the busiest site still shows, with nothing to list.
        assert self.places(split, site="plain.example.com")["places"] == []

    def test_with_no_place_to_list_anywhere_the_busiest_site(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        credential = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "Pool", "type": "static_proxy_provider", "config": {}},
        ).json()["id"]
        pool = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "Pool", "credential_id": credential, "config": {}, "routing_config": {"weight": 1}},
        ).json()["id"]
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {
                ConnectorSiteKey(project_id, pool, site, ""): RequestCount(blocked=blocked)
                for site, blocked in (("quiet.example.com", 1), ("busy.example.com", 3))
            },
        )

        body = authenticated_client.get(f"/api/v1/projects/{project_id}/exit-reputation/places").json()

        assert (body["site"], [listed["site"] for listed in body["sites"]], body["places"]) == (
            "busy.example.com", ["busy.example.com", "quiet.example.com"], []
        )

    def test_a_project_with_no_records(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        body = authenticated_client.get(f"/api/v1/projects/{project_id}/exit-reputation/places").json()
        # No site and no hardest hit: left out, as every field without a value is.
        assert body == {
            "mode": "record", "reputation_applied": False, "reputation_strength": 1, "reputation_floor": 0.1, "sites": [],
            "connectors": [], "places": [], "summary": {"lowered_connectors": 0, "serving_connectors": 0},
        }

    def test_a_site_that_names_none_is_a_400(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/places", params={"site": "co.uk"}
        )
        assert (response.status_code, response.json()["detail"]) == (400, "not a site: co.uk is a public suffix")

    def test_every_signed_in_role_reads_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        url = f"/api/v1/projects/{split.project_id}/exit-reputation/places"
        for headers in (editor_auth_headers, viewer_auth_headers):
            response = authenticated_client.get(url, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["site"] == "www.google.com"
        assert authenticated_client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert authenticated_client.get("/api/v1/projects/nope/exit-reputation/places").status_code == 404


def _stronger(split: Split, *, strength: int, floor: int) -> str:
    """A second Oxylabs dynamic connector on ``split`` (weight 2), blocked on 1 of 20 requests to the site from US/NY,
    with the project at ``strength`` and lowest factor ``floor``; its id."""
    response = split.client.patch(
        f"/api/v1/projects/{split.project_id}",
        json={"exit_reputation_strength": strength, "exit_reputation_floor": floor},
    )
    assert response.status_code == 200, response.text
    oxylabs = split._credential("Oxylabs 2", "oxylabs", {"proxy_type": "residential", "username": "bob", "password": "s3cret"})
    stronger = split._connector(oxylabs, "Dynamic 2", {"session_mode": "dynamic", "session_duration_minutes": 10}, {"weight": 2})
    split._row(stronger, provider="oxylabs", proxy_type="residential", dynamic_sessions="true")
    _apply_connectors(split.client, utc_now(), {split.key(stronger, "US/NY"): RequestCount(requests=20, blocked=1)})
    return stronger


# The first dynamic connector's 0.75 beside the second's 0.95, at strength 10.
COMPARED = 0.75 * (0.75 / 0.95) ** 9


class TestStrength:
    """Above strength 1 a dynamic connector is weighed against the best one at the place, never under the lowest factor;
    the views show its success, the best one's, and the value it routes by."""

    def test_the_traffic_split_weighs_each_against_the_best(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        stronger = _stronger(split, strength=10, floor=5)

        body = split.split(site=Split.SITE, place="US/NY")

        assert (body["reputation_strength"], body["reputation_floor"]) == (10, 0.05)
        connectors = {connector["name"]: connector for connector in body["connectors"]}
        weaker, best, pool = connectors["Dynamic"], connectors["Dynamic 2"], connectors["Pool"]
        assert (weaker["success"], weaker["best_success"], weaker["best_connector_id"]) == (0.75, 0.95, stronger)
        assert weaker["reputation"] == pytest.approx(COMPARED)
        assert weaker["effective_weight"] == pytest.approx(round(2 * COMPARED, 4))
        assert (best["reputation"], best["success"], best["best_success"], best["best_connector_id"]) == (
            0.95, 0.95, 0.95, stronger
        )
        # A connector with IPs is not compared.
        assert (pool["reputation"], pool["success"], pool["best_success"], pool["best_connector_id"]) == (
            0.5, None, None, None
        )
        total = 2 * COMPARED + 1.9 + 0.5
        assert weaker["effective_share"] == round(2 * COMPARED / total * 100, 2)

    def test_the_lowest_factor_holds_and_the_places_agree(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        stronger = _stronger(split, strength=10, floor=20)

        body = TestPlaces.places(split, site=Split.SITE)

        assert (body["reputation_strength"], body["reputation_floor"]) == (10, 0.2)
        rows = {row["place"]: row for row in body["places"]}
        cell = rows["US/NY"]["cells"][split.dynamic]
        assert (cell["reputation"], cell["success"], cell["best_success"], cell["best_connector_id"]) == (
            0.2, 0.75, 0.95, stronger
        )
        shown = split.by_name(site=Split.SITE, place="US/NY")["Dynamic"]
        assert (shown["reputation"], shown["effective_share"]) == (cell["reputation"], cell["effective_share"])


class TestStrengthPreview:
    """``GET /exit-reputation/strength-preview?strength=&floor=&site=``: each connector's share of a site's requests
    from its most reported places as Avoid routes them, at the saved settings and at the draft."""

    @staticmethod
    def preview(client: TestClient, project_id: str, **params: Any) -> dict[str, Any]:
        response = client.get(f"/api/v1/projects/{project_id}/exit-reputation/strength-preview", params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def test_the_shares_at_the_saved_settings_and_the_draft(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data, mode="record")
        stronger = _stronger(split, strength=1, floor=10)

        body = self.preview(authenticated_client, split.project_id, strength=10, floor=5)

        # The site the Reputation tab opens on, and its places the most reported first.
        assert body["site"] == Split.SITE
        assert [(place["place"], place["label"], place["reports"]) for place in body["places"]] == [
            ("US", "United States", 6), ("US/NY", "New York, United States", 6)
        ]
        new_york = {connector["name"]: connector for connector in body["places"][1]["connectors"]}
        assert [connector["connector_id"] for connector in body["places"][1]["connectors"]] == [
            split.dynamic, stronger, split.pool
        ]
        # Now: 0.75 x2, 0.95 x2 and 0.5 x1; the draft takes the first to 0.75 x (0.75 / 0.95)^9.
        now_total, draft_total = 1.5 + 1.9 + 0.5, 2 * COMPARED + 1.9 + 0.5
        assert {name: (connector["current_share"], connector["draft_share"]) for name, connector in new_york.items()} == {
            "Dynamic": (round(150 / now_total, 2), round(200 * COMPARED / draft_total, 2)),
            "Dynamic 2": (round(190 / now_total, 2), round(190 / draft_total, 2)),
            "Pool": (round(50 / now_total, 2), round(50 / draft_total, 2)),
        }
        # The saved settings are left as they are.
        project = authenticated_client.get(f"/api/v1/projects/{split.project_id}").json()
        assert (project["exit_reputation_strength"], project["exit_reputation_floor"]) == (1, 10)

    def test_only_the_connectors_taking_the_requests_and_the_site_asked_for(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data, pool_routing={"domain_blacklist": ["example.com"]})
        site = "shop.example.com"
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {ConnectorSiteKey(split.project_id, split.dynamic, site, "DE"): RequestCount(requests=20, blocked=2)},
        )

        body = self.preview(authenticated_client, split.project_id, strength=5, floor=10, site=f"https://{site}/cart")

        assert body["site"] == site
        (germany,) = body["places"]
        # The pool's domain filter keeps it off the site; the dynamic connector alone takes every request.
        assert germany["connectors"] == [
            {"connector_id": split.dynamic, "name": "Dynamic", "current_share": 100.0, "draft_share": 100.0}
        ]

    def test_at_most_five_places(self, authenticated_client: TestClient, sample_project_data: dict[str, Any]) -> None:
        split = Split(authenticated_client, sample_project_data)
        site = "wide.example.com"
        _apply_connectors(
            authenticated_client,
            utc_now(),
            {
                ConnectorSiteKey(split.project_id, split.dynamic, site, country): RequestCount(requests=20, blocked=n)
                for n, country in enumerate(("DE", "FR", "GB", "IT", "ES", "NL", "PL"), start=1)
            },
        )

        body = self.preview(authenticated_client, split.project_id, strength=2, floor=10, site=site)

        assert [place["place"] for place in body["places"]] == ["PL", "NL", "ES", "IT", "GB"]

    def test_a_project_with_no_records_or_a_site_with_no_places(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]

        assert self.preview(authenticated_client, project_id, strength=10, floor=10) == {"site": None, "places": []}
        assert self.preview(authenticated_client, project_id, strength=10, floor=10, site="new.example.com") == {
            "site": "new.example.com", "places": []
        }

    @pytest.mark.parametrize(
        "params",
        [
            {"strength": 0, "floor": 10},
            {"strength": 21, "floor": 10},
            {"strength": 5, "floor": 0},
            {"strength": 5, "floor": 101},
            {"floor": 10},
            {"strength": 5},
        ],
    )
    def test_the_draft_is_bounded_as_the_settings_are(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any], params: dict[str, int]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/strength-preview", params=params
        )
        assert response.status_code == 422

    def test_a_site_that_names_none_is_a_400_and_every_signed_in_role_reads_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        url = f"/api/v1/projects/{split.project_id}/exit-reputation/strength-preview"
        params = {"strength": 10, "floor": 10}

        refused = authenticated_client.get(url, params={**params, "site": "co.uk"})
        assert (refused.status_code, refused.json()["detail"]) == (400, "not a site: co.uk is a public suffix")
        viewed = authenticated_client.get(url, params=params, headers=viewer_auth_headers)
        assert viewed.status_code == 200 and viewed.json()["site"] == Split.SITE
        assert authenticated_client.get(url, params=params, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert (
            authenticated_client.get(
                "/api/v1/projects/nope/exit-reputation/strength-preview", params=params
            ).status_code
            == 404
        )


def _move(client: TestClient, when: datetime, evidence: dict[ConnectorSiteKey, RequestCount], dynamic: list[str]) -> None:
    """What the metrics flush writes for dynamic connectors' requests and reports at every level of each key's place,
    keeping each factor that moved."""

    async def applied() -> None:
        async with api_runtime(client).session_factory() as session:
            await ExitReputationRepository(session).apply_evidence(
                {level: counts for key, counts in evidence.items() for level in key.levels()},
                dynamic,
                when,
                timedelta(minutes=1),
            )
            await session.commit()

    on_app_loop(client, applied)


class TestPlaceHistory:
    """``GET /exit-reputation/places/history?site=&place=&range=``: how the connectors taking the requests at a place
    moved over a range: a dynamic one's factor own and routed, a connector with IPs' usable rows of its eligible ones."""

    @staticmethod
    def history(split: Split, **params: str) -> dict[str, Any]:
        response = split.client.get(
            f"/api/v1/projects/{split.project_id}/exit-reputation/places/history", params={"site": Split.SITE, **params}
        )
        assert response.status_code == 200, response.text
        # A field without a value is left out, never sent as null.
        assert "null" not in response.text, response.text
        body: dict[str, Any] = response.json()
        return body

    def test_each_move_of_the_connectors_taking_the_requests(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        began, first, second = (utc_now() - timedelta(minutes=minutes) for minutes in (60, 20, 10))
        # The history began with a move on another site.
        _move(authenticated_client, began, {split.key(split.dynamic)._replace(site="bing.com"): RequestCount(20, blocked=10)}, [split.dynamic])
        _move(authenticated_client, first, {split.key(split.dynamic, "US/NY"): RequestCount(requests=100)}, [split.dynamic])
        _move(
            authenticated_client,
            second,
            {split.key(split.dynamic, "US/NY"): RequestCount(requests=20, blocked=10)},
            [split.dynamic],
        )

        body = self.history(split, place="us/ny")

        assert (body["site"], body["place"], body["range"]) == ("www.google.com", "US/NY", "24h")
        assert "bucket_seconds" not in body
        assert datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"]) == timedelta(hours=24)
        # The first move kept, whatever its project: another test's app may have kept one earlier.
        assert datetime.fromisoformat(body["recorded_since"]) <= datetime.fromisoformat(_utc(began))
        start = max(body["since"], body["recorded_since"])
        assert body["rules"] == [{"at": start, "applied": True, "strength": 1, "floor": 0.1}]
        # By name: the dynamic connector, then the pool with IPs.
        dynamic, pool = body["series"]
        assert (dynamic["connector_id"], dynamic["name"], dynamic["kind"]) == (split.dynamic, "Dynamic", "block_rate")
        assert dynamic["points"] == [
            # Before its first move, what that move moved it from; nothing drawn before the history began.
            {"at": start, "own": 0.75, "level": "US/NY", "routed": 0.75},
            # Dated before the split's own flush, the 100 clean requests fade nothing: 5 of 120 blocked.
            {"at": _utc(first), "own": 0.95, "level": "US/NY", "routed": 0.95},
            {"at": _utc(second), "own": 0.9, "level": "US/NY", "routed": 0.9, "requests": 20, "blocked": 10},
        ]
        # Its four rows in New York are eligible; the reports avoided two of their IPs. Own and routed agree.
        assert (pool["connector_id"], pool["name"], pool["kind"]) == (split.pool, "Pool", "usable_ips")
        assert pool["points"] == [
            {"at": start, "own": 1.0, "routed": 1.0, "usable": 4, "eligible": 4},
            {"at": _utc(split.reported_at), "own": 0.5, "routed": 0.5, "usable": 2, "eligible": 4},
        ]

    def test_a_week_is_hourly_and_no_place_is_its_own(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = self.history(split, range="7d")

        assert (body["place"], body["range"], body["bucket_seconds"]) == ("", "7d", 3600)
        # Nothing moved here: one flat line at the factor now, an hour a point from where the range or the history
        # starts.
        dynamic, pool = body["series"]
        assert dynamic["points"][0]["at"] == max(body["since"], body.get("recorded_since", ""))
        assert len(dynamic["points"]) <= 169
        assert {(point["own"], point["level"], point["routed"]) for point in dynamic["points"]} == {(0.95, "", 0.95)}
        # The pool's hours, the same in own and routed, of its four rows.
        assert pool["kind"] == "usable_ips" and len(pool["points"]) == len(dynamic["points"])
        assert all(point["own"] == point["routed"] and point["eligible"] == 4 for point in pool["points"])
        assert all("level" not in point and "requests" not in point for point in pool["points"])
        assert self.history(split, place=" ")["place"] == ""

    def test_30_days_are_three_hour_means(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = self.history(split, place="US", range="30d")

        assert (body["range"], body["bucket_seconds"]) == ("30d", 10800)
        assert datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"]) == timedelta(days=30)
        # A point per three hours of the clock after the first, from where the range or the history starts.
        for series in body["series"]:
            points = [datetime.fromisoformat(point["at"]) for point in series["points"]]
            assert len(points) <= 241
            assert all(at.hour % 3 == 0 and (at.minute, at.second, at.microsecond) == (0, 0, 0) for at in points[1:])

    def test_nothing_is_drawn_before_the_days_the_project_keeps(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        """The moves the project kept longer ago are gone, or go with the next sweep."""
        split = Split(authenticated_client, sample_project_data)
        _move(
            authenticated_client,
            utc_now() - timedelta(days=3),
            {split.key(split.dynamic, "US"): RequestCount(20, blocked=10)},
            [split.dynamic],
        )
        response = authenticated_client.patch(
            f"/api/v1/projects/{split.project_id}", json={"exit_reputation_retention_days": 1}
        )
        assert response.status_code == 200, response.text

        body = self.history(split, range="7d")

        until = datetime.fromisoformat(body["until"])
        assert datetime.fromisoformat(body["recorded_since"]) == until - timedelta(days=1)
        assert {len(series["points"]) for series in body["series"]} <= {24, 25}

    def test_the_rule_in_force_from_the_projects_history(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        response = authenticated_client.patch(
            f"/api/v1/projects/{split.project_id}", json={"exit_reputation_strength": 10, "exit_reputation_floor": 30}
        )
        assert response.status_code == 200, response.text
        response = authenticated_client.patch(f"/api/v1/projects/{split.project_id}", json={"exit_reputation": "record"})
        assert response.status_code == 200, response.text

        rules = self.history(split, place="US", range="1h")["rules"]

        assert [(rule["applied"], rule["strength"], rule["floor"]) for rule in rules] == [
            (True, 1, 0.1),
            (True, 10, 0.3),
            (False, 10, 0.3),
        ]

    def test_a_site_or_place_that_names_none_is_a_400_and_a_range_not_offered_a_422(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        url = f"/api/v1/projects/{project_id}/exit-reputation/places/history"

        response = authenticated_client.get(url, params={"site": "co.uk"})
        assert (response.status_code, response.json()["detail"]) == (400, "not a site: co.uk is a public suffix")
        response = authenticated_client.get(url, params={"site": "google.com", "place": "US/NY/a/b"})
        assert (response.status_code, response.json()["detail"]) == (400, "not a place: US/NY/a/b")
        assert authenticated_client.get(url, params={"site": "google.com", "range": "90d"}).status_code == 422
        assert authenticated_client.get(url).status_code == 422
        response = authenticated_client.get(
            "/api/v1/projects/nope/exit-reputation/places/history", params={"site": "google.com"}
        )
        assert response.status_code == 404

    def test_every_signed_in_role_reads_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        url = f"/api/v1/projects/{split.project_id}/exit-reputation/places/history"
        for headers in (editor_auth_headers, viewer_auth_headers):
            response = authenticated_client.get(url, params={"site": Split.SITE}, headers=headers)
            assert response.status_code == 200, response.text
        response = authenticated_client.get(url, params={"site": Split.SITE}, headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
