# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit reputation reads: a project's exit IP records, one IP's, per-IP totals, the traffic split for a site and
the connectors across sites, for any signed-in role.

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
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from api.routes import exit_reputation
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
                {level: counts for key, counts in evidence.items() for level in key.levels()}, (), when
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
        now = utc_now()
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
            {"place": "US/NY", "label": "US, NY", "reports": 5},
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


class TestConnectors:
    """``GET /exit-reputation/connectors``: every connector on each of the project's busiest sites."""

    def test_every_connector_on_each_busy_site_for_the_place(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        response = authenticated_client.get(
            f"/api/v1/projects/{split.project_id}/exit-reputation/connectors", params={"place": "US/NY"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["place"], body["sites"], body["reputation_applied"]) == (
            "US/NY", ["www.google.com", "bing.com"], True
        )
        assert body["places"] == [
            {"place": "US", "label": "United States", "reports": 5},
            {"place": "US/NY", "label": "US, NY", "reports": 5},
        ]
        assert [(c["name"], c["dynamic"], c["weight"]) for c in body["connectors"]] == [
            ("Dynamic", True, 2),
            ("Pool", False, 1),
        ]
        dynamic, pool = body["connectors"]
        assert dynamic["cells"]["www.google.com"] == {
            "reputation": 0.75, "reputation_kind": "block_rate", "reputation_place": "US/NY", "blocked": 5,
            "requests": 20, "usable_ips": None, "eligible_ips": None, "effective_share": 75.0,
        }
        assert pool["cells"]["www.google.com"]["effective_share"] == 25.0
        # Nothing avoided or blocked on bing.com: the configured split.
        assert pool["cells"]["bing.com"] == {
            "reputation": 1.0, "reputation_kind": "usable_ips", "reputation_place": None, "blocked": 0,
            "requests": 0, "usable_ips": 4, "eligible_ips": 4, "effective_share": 33.33,
        }
        assert dynamic["cells"]["bing.com"]["reputation_kind"] == "block_rate"

    def test_without_a_place_requests_naming_none_are_weighed(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        split = Split(authenticated_client, sample_project_data)

        body = authenticated_client.get(f"/api/v1/projects/{split.project_id}/exit-reputation/connectors").json()

        assert body["place"] is None
        dynamic = body["connectors"][0]["cells"]["www.google.com"]
        assert (dynamic["reputation"], dynamic["reputation_place"], dynamic["blocked"], dynamic["requests"]) == (
            0.95, "", 2, 40
        )

    def test_a_project_with_no_records(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        body = authenticated_client.get(f"/api/v1/projects/{project_id}/exit-reputation/connectors").json()
        assert body == {"place": None, "places": [], "sites": [], "reputation_applied": False, "connectors": []}

    def test_a_place_that_names_none_is_a_400(
        self, authenticated_client: TestClient, sample_project_data: dict[str, Any]
    ) -> None:
        project_id = _project(authenticated_client, sample_project_data)["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/exit-reputation/connectors", params={"place": "U/S/A/B"}
        )
        assert response.status_code == 400

    def test_every_signed_in_role_reads_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        split = Split(authenticated_client, sample_project_data)
        url = f"/api/v1/projects/{split.project_id}/exit-reputation/connectors"
        for headers in (editor_auth_headers, viewer_auth_headers):
            response = authenticated_client.get(url, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["sites"] == ["www.google.com", "bing.com"]
        assert authenticated_client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert authenticated_client.get("/api/v1/projects/nope/exit-reputation/connectors").status_code == 404
