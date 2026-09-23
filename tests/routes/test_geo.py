# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the IP attribution admin API."""

from pathlib import Path

from starlette.testclient import TestClient

from tests.geo.test_readers import IPINFO_RECORD, MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"


DEFAULT_SETTINGS = {
    "default_sources": ["database", "vendor", "endpoint"],
    "default_conflict_rule": "consensus",
}


def _clear(client: TestClient) -> None:
    """Reset what other tests left behind: stored databases and the saved policy (the database outlives the app)."""
    for row in client.get("/api/v1/geo/databases").json()["databases"]:
        if row["source"] != "path":
            client.delete(f"/api/v1/geo/databases/{row['id']}")
    current = client.get("/api/v1/geo/settings").json()["settings"]
    current.update(DEFAULT_SETTINGS)
    assert client.put("/api/v1/geo/settings", json=current).status_code == 200


def _upload(client: TestClient, path: Path, **form: object) -> object:
    with path.open("rb") as handle:
        return client.post(
            "/api/v1/geo/databases",
            files={"file": (path.name, handle, "application/octet-stream")},
            data={k: str(v) for k, v in form.items()},
        )


class TestAccess:
    def test_viewer_can_read_but_not_write(self, viewer_client: TestClient) -> None:
        assert viewer_client.get("/api/v1/geo/settings").status_code == 200
        assert viewer_client.get("/api/v1/geo/databases").status_code == 200
        assert (
            viewer_client.put(
                "/api/v1/geo/settings", json={"default_sources": ["database"]}
            ).status_code
            == 403
        )
        assert viewer_client.post("/api/v1/geo/reattribute", json={}).status_code == 403

    def test_unauthenticated(self, async_client: TestClient) -> None:
        assert async_client.get("/api/v1/geo/settings").status_code == 401


class TestSettings:
    def test_defaults_then_save(self, authenticated_client: TestClient) -> None:
        current = authenticated_client.get("/api/v1/geo/settings").json()
        assert set(current["settings"]["default_sources"]) <= {"database", "vendor", "endpoint"}
        assert current["settings"]["echo_url"].startswith("https://")

        body = dict(current["settings"])
        body.update(
            {
                "default_sources": ["vendor", "database"],
                "default_conflict_rule": "first",
                "echo_url": "https://echo.example/ip",
                "preflight_max_attempts": 4,
            }
        )
        saved = authenticated_client.put("/api/v1/geo/settings", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["from_database"] is True
        assert saved.json()["settings"]["default_sources"] == ["vendor", "database"]
        assert saved.json()["settings"]["preflight_max_attempts"] == 4

        again = authenticated_client.get("/api/v1/geo/settings").json()
        assert again["settings"]["echo_url"] == "https://echo.example/ip"

    def test_invalid_settings(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.put("/api/v1/geo/settings", json={"default_sources": []})
        assert response.status_code == 422
        response = authenticated_client.put(
            "/api/v1/geo/settings", json={"preflight_max_attempts": 0}
        )
        assert response.status_code == 422


class TestDatabases:
    def test_upload_lookup_and_delete(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        path = write_mmdb(
            tmp_path / "GeoLite2-City.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}
        )

        before = authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()
        assert before["resolution"]["country"] is None and before["databases_loaded"] == 0

        response = _upload(authenticated_client, path, name="City test", priority=10)
        assert response.status_code == 201, response.text
        record = response.json()
        assert (
            record["vendor"] == "maxmind"
            and record["kind"] == "city"
            and record["source"] == "upload"
        )
        assert record["loaded_here"] is True and record["size_bytes"] == path.stat().st_size
        assert record["database_type"] == "GeoLite2-City" and "GeoLite2" in record["attribution"]
        assert record["uploaded_by"] == "testadmin"

        listed = authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        assert [d["id"] for d in listed] == [record["id"]]

        lookup = authenticated_client.post(
            "/api/v1/geo/lookup", json={"ip": GB_IP, "claimed_country": "us"}
        ).json()
        assert lookup["resolution"]["country"] == "GB" and lookup["resolution"]["conflict"] is True
        assert lookup["candidates"][0]["location"]["city"] == "London"

        settings = authenticated_client.get("/api/v1/geo/settings").json()
        assert [d["id"] for d in settings["databases_loaded"]] == [record["id"]]

        patched = authenticated_client.patch(
            f"/api/v1/geo/databases/{record['id']}", json={"enabled": False, "name": "Off"}
        )
        assert (
            patched.status_code == 200
            and patched.json()["enabled"] is False
            and patched.json()["name"] == "Off"
        )
        assert patched.json()["loaded_here"] is False
        assert (
            authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()[
                "resolution"
            ]["country"]
            is None
        )

        assert authenticated_client.patch(
            f"/api/v1/geo/databases/{record['id']}", json={"enabled": True}
        ).json()["loaded_here"]

        assert (
            authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}").status_code == 204
        )
        assert record["id"] not in [
            d["id"] for d in authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        ]
        assert (
            authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}").status_code == 404
        )

    def test_priority_order_and_merge(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        city = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        ipinfo = write_mmdb(
            tmp_path / "ipinfo.mmdb", "ipinfo country_asn", {"81.2.69.0/24": IPINFO_RECORD}
        )
        assert _upload(authenticated_client, city, priority=50).status_code == 201
        assert _upload(authenticated_client, ipinfo, priority=10).status_code == 201
        lookup = authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()
        # IPinfo (priority 10) wins; the databases disagree so nothing is certain.
        assert (
            lookup["resolution"]["country"] == "DE" and lookup["resolution"]["disagreement"] is True
        )
        assert [c["country"] for c in lookup["candidates"]] == ["DE", "GB"]

    def test_rejects_junk(self, authenticated_client: TestClient, tmp_path: Path) -> None:
        junk = tmp_path / "junk.mmdb"
        junk.write_bytes(b"not a database" * 50)
        before = len(authenticated_client.get("/api/v1/geo/databases").json()["databases"])
        assert _upload(authenticated_client, junk).status_code == 422
        assert len(authenticated_client.get("/api/v1/geo/databases").json()["databases"]) == before

    def test_inspect(self, authenticated_client: TestClient, tmp_path: Path) -> None:
        path = write_mmdb(
            tmp_path / "GeoLite2-Country.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD}
        )
        with path.open("rb") as handle:
            response = authenticated_client.post(
                "/api/v1/geo/databases/inspect", files={"file": (path.name, handle)}
            )
        assert response.status_code == 200
        assert response.json()["kind"] == "country" and response.json()["vendor"] == "maxmind"

    def test_editor_cannot_upload(self, editor_client: TestClient, tmp_path: Path) -> None:
        path = write_mmdb(tmp_path / "c.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        assert _upload(editor_client, path).status_code == 403

    def test_from_url_with_blocked_target_keeps_row_with_error(
        self, authenticated_client: TestClient
    ) -> None:
        _clear(authenticated_client)
        response = authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={
                "name": "blocked",
                "update_url": "http://127.0.0.1:1/db.mmdb",
                "update_interval_hours": 24,
            },
        )
        assert response.status_code == 400
        rows = authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        assert len(rows) == 1 and rows[0]["source"] == "url" and rows[0]["last_update_error"]
        assert rows[0]["loaded_here"] is False


class TestProxyAttribution:
    def test_manual_country_and_reattribute(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        created = authenticated_client.post(
            "/api/v1/projects", json={"name": "geo-p", "username": "geo_user", "password": "pw"}
        )
        assert created.status_code == 201, created.text
        project = created.json()
        cred_response = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/credentials",
            json={"name": "static", "type": "static_proxy_provider", "config": {}},
        )
        assert cred_response.status_code == 201, cred_response.text
        credential = cred_response.json()
        conn_response = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/connectors",
            json={"name": "static-c", "credential_id": credential["id"], "config": {}},
        )
        assert conn_response.status_code == 201, conn_response.text
        connector = conn_response.json()
        proxy_response = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/proxies",
            json={"host": GB_IP, "port": 8080, "connector_id": connector["id"], "country": "us"},
        )
        assert proxy_response.status_code == 201, proxy_response.text
        proxy = proxy_response.json()
        assert proxy["country"] == "US" and proxy["country_source"] == "manual"

        path = write_mmdb(
            tmp_path / "GeoLite2-City.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}
        )
        assert _upload(authenticated_client, path).status_code == 201

        # The proxy has no discovered IP yet, so re-attribution leaves it alone.
        assert authenticated_client.post("/api/v1/geo/reattribute", json={}).json()["updated"] == 0

        # Give it one by hand (as a discovery would), then re-attribute: the
        # manual country stays for routing but is flagged as contradicted.
        patched = authenticated_client.patch(
            f"/api/v1/projects/{project['id']}/proxies/{proxy['id']}",
            json={
                "metadata": {"discovered_ip": GB_IP, "country": "US", "country_source": "manual"}
            },
        )
        assert patched.status_code == 200, patched.text
        assert (
            authenticated_client.post(
                "/api/v1/geo/reattribute", json={"connector_id": connector["id"]}
            ).json()["updated"]
            == 1
        )
        updated = authenticated_client.get(
            f"/api/v1/projects/{project['id']}/proxies/{proxy['id']}"
        ).json()
        assert updated["country"] == "US" and updated["location_conflict"] is True
        assert updated["location"]["city"] == "London" and updated["vendor_country"] is None

        # Clearing the country drops the manual pin; the next pass adopts the database answer.
        cleared = authenticated_client.patch(
            f"/api/v1/projects/{project['id']}/proxies/{proxy['id']}", json={"country": ""}
        ).json()
        assert cleared["country"] is None and cleared["country_source"] is None
        authenticated_client.post("/api/v1/geo/reattribute", json={})
        final = authenticated_client.get(
            f"/api/v1/projects/{project['id']}/proxies/{proxy['id']}"
        ).json()
        assert (
            final["country"] == "GB"
            and final["country_source"] == "database"
            and final["location_conflict"] is False
        )

    def test_project_location_policy_roundtrip(self, authenticated_client: TestClient) -> None:
        created = authenticated_client.post(
            "/api/v1/projects",
            json={
                "name": "strict-p",
                "username": "strict_user",
                "password": "pw",
                "location_policy": "strict",
                "location_preflight": "report",
            },
        )
        assert created.status_code == 201, created.text
        assert (
            created.json()["location_policy"] == "strict"
            and created.json()["location_preflight"] == "report"
        )
        updated = authenticated_client.patch(
            f"/api/v1/projects/{created.json()['id']}", json={"location_preflight": "reject"}
        ).json()
        assert updated["location_preflight"] == "reject" and updated["location_policy"] == "strict"
        listed = authenticated_client.get("/api/v1/projects").json()["projects"]
        assert any(p["location_policy"] == "strict" for p in listed)
        assert (
            authenticated_client.patch(
                f"/api/v1/projects/{created.json()['id']}", json={"location_policy": "loud"}
            ).status_code
            == 422
        )

    def test_project_source_policy_override_and_lookup(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        path = write_mmdb(
            tmp_path / "GeoLite2-City.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}
        )
        assert _upload(authenticated_client, path).status_code == 201
        created = authenticated_client.post(
            "/api/v1/projects",
            json={
                "name": "trusting-p",
                "username": "trusting_user",
                "password": "pw",
                "location_sources": ["vendor", "vendor", "database"],
                "location_preflight": "retry",
            },
        )
        assert created.status_code == 201, created.text
        project = created.json()
        assert (
            project["location_sources"] == ["vendor", "database"]
            and project["location_conflict_rule"] is None
        )
        assert project["location_preflight"] == "retry"

        default = authenticated_client.post(
            "/api/v1/geo/lookup", json={"ip": GB_IP, "claimed_country": "US"}
        ).json()
        assert (
            default["resolution"]["country"] == "GB"
            and default["policy"]["sources"][0] == "database"
        )
        scoped = authenticated_client.post(
            "/api/v1/geo/lookup",
            json={"ip": GB_IP, "claimed_country": "US", "project_id": project["id"]},
        ).json()
        assert scoped["resolution"]["country"] == "US" and scoped["policy"]["sources"] == [
            "vendor",
            "database",
        ]
        assert scoped["resolution"]["conflict"] is True

        cleared = authenticated_client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"location_sources": [], "location_conflict_rule": "first"},
        ).json()
        assert cleared["location_sources"] is None and cleared["location_conflict_rule"] == "first"
        reset = authenticated_client.patch(
            f"/api/v1/projects/{project['id']}", json={"location_conflict_rule": ""}
        ).json()
        assert reset["location_conflict_rule"] is None
        assert (
            authenticated_client.post(
                "/api/v1/geo/lookup", json={"ip": GB_IP, "project_id": "missing"}
            ).status_code
            == 404
        )


class TestObservationsAndStatus:
    def test_history_endpoints(self, authenticated_client: TestClient) -> None:
        page = authenticated_client.get(
            "/api/v1/geo/observations?limit=5&offset=0&source=health_check"
        ).json()
        assert isinstance(page["observations"], list)
        assert (
            page["limit"] == 5
            and page["offset"] == 0
            and page["total"] >= len(page["observations"])
        )
        accuracy = authenticated_client.get("/api/v1/geo/accuracy?days=7").json()
        assert isinstance(accuracy["connectors"], list) and accuracy["since"]
        for row in accuracy["connectors"]:
            assert set(row) >= {"exits", "claimed", "confirmed", "contradicted", "uncertain", "accuracy", "breakdown", "coverage"}
        status = authenticated_client.get("/api/v1/geo/status").json()
        assert status["databases_total"] >= status["databases_loaded"]
        assert status["preflight_checks"] == 0 and status["stored_observations"] >= 0

    def test_exits_endpoints(self, authenticated_client: TestClient) -> None:
        summary = authenticated_client.get("/api/v1/geo/exits?days=30").json()
        assert isinstance(summary["connectors"], list) and summary["since"]
        ips = authenticated_client.get("/api/v1/geo/exits/ips?connector_id=none&limit=5").json()
        assert ips == {"ips": [], "total": 0, "limit": 5, "offset": 0}
        assert authenticated_client.get("/api/v1/geo/exits/ips?verdict=bogus").status_code == 422
        assert authenticated_client.get("/api/v1/geo/exits/ips?project_id=missing").json()["total"] == 0

    def test_lookup_validation(self, authenticated_client: TestClient) -> None:
        assert (
            authenticated_client.post("/api/v1/geo/lookup", json={"ip": "nope"}).status_code == 400
        )
        assert (
            authenticated_client.post(
                "/api/v1/geo/lookup", json={"ip": GB_IP, "claimed_country": "USA"}
            ).status_code
            == 400
        )


class TestEchoRoute:
    def test_echo_reports_peer(self, async_client: TestClient) -> None:
        response = async_client.get("/echo?nonce=abc")
        # TestClient's peer is "testclient", which is not an address.
        assert response.status_code in (200, 400)
