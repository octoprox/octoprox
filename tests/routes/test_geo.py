# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the IP attribution admin API, on an api process with no worker, so a ``reattribute`` key stays queued.

Where the pass itself matters the worker's job runs the queued key here; what a write announces is read off the changes
stream, and gateways from the heartbeats the tests advertise the way a gateway would.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.testclient import TestClient

from api.core import utc_now
from api.core.config import Settings
from api.core.roles import JobKind
from api.db.geo_repository import (
    ExitSighting,
    GeoDatabaseRepository,
    GeoSettingsRepository,
    ObservationRepository,
)
from api.db.redis import CHANGES_STREAM_KEY, JOB_STREAM_KEY
from api.db.repository import ProxyRepository
from api.geo.models import GeoDatabaseRecord, GeoSettings, IpObservation, ObservationSource
from api.geo.store import path_database_id
from api.geo.updater import DownloadError
from api.jobs.attribution import ReattributeJob
from api.jobs.job import JobBatch
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from tests.conftest import api_runtime, on_app_loop, queued_job_keys
from tests.geo.test_places import write_geonames
from tests.geo.test_readers import IPINFO_RECORD, MAXMIND_RECORD, write_mmdb
from tests.routes.gateway_heartbeats import advertise_gateway, withdraw_gateway
from tests.routes.test_providers import Unheard, announced_since, stream_head

GB_IP = "81.2.69.160"
# An Oxylabs residential account, and a connector rendering a vendor session per request through it.
OXYLABS_CREDENTIAL = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
OXYLABS_DYNAMIC = {"session_mode": "dynamic", "session_duration_minutes": 10}


DEFAULT_SETTINGS = {
    "default_conflict_rule": "consensus",
    "default_city_radius_km": 25,
}


def _clear(client: TestClient) -> None:
    """Reset what other tests left behind: stored databases and the saved policy (the database outlives the app)."""
    for row in client.get("/api/v1/geo/databases").json()["databases"]:
        if row["source"] != "path":
            client.delete(f"/api/v1/geo/databases/{row['id']}")
    current = client.get("/api/v1/geo/settings").json()["settings"]
    current.update(DEFAULT_SETTINGS)
    assert client.put("/api/v1/geo/settings", json=current).status_code == 200


def _upload(client: TestClient, path: Path, **form: object) -> Any:
    with path.open("rb") as handle:
        return client.post(
            "/api/v1/geo/databases",
            files={"file": (path.name, handle, "application/octet-stream")},
            data={k: str(v) for k, v in form.items()},
        )


def _city_database(directory: Path, description: str = "test") -> Path:
    return write_mmdb(
        directory / f"GeoLite2-City-{description}.mmdb",
        "GeoLite2-City",
        {"81.2.69.0/24": MAXMIND_RECORD},
        description=description,
    )


def _drop_jobs(client: TestClient) -> None:
    """Forget the re-attributions earlier writes queued, so a test sees only its own."""
    redis = api_runtime(client).redis_client
    on_app_loop(client, redis.client.delete, JOB_STREAM_KEY.format(kind=JobKind.REATTRIBUTE))


def _reattributions(client: TestClient) -> list[str]:
    """The ``reattribute`` keys queued, oldest first: no worker takes them here."""
    return on_app_loop(client, queued_job_keys, api_runtime(client).redis_client, JobKind.REATTRIBUTE)


def _row(client: TestClient, database_id: str) -> dict[str, Any]:
    rows = client.get("/api/v1/geo/databases").json()["databases"]
    return next(row for row in rows if row["id"] == database_id)


def _save_elsewhere(client: TestClient, settings: dict[str, Any]) -> None:
    """Store the settings row as another api would, its change entry not here yet: nothing tells this one."""

    async def save() -> None:
        async with api_runtime(client).session_factory() as session:
            await GeoSettingsRepository(session).save(GeoSettings(**settings), updated_by="elsewhere")
            await session.commit()

    on_app_loop(client, save)


def _serve_downloads(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Vendor downloads answer with ``path``'s bytes: the network and its egress guard are not under test here."""

    async def download(record: object, guard: object) -> bytes:
        return path.read_bytes()

    monkeypatch.setattr("api.geo.updater.download", download)


@pytest.fixture
def gateways(authenticated_client: TestClient) -> Iterator[Callable[..., None]]:
    """Advertise live gateways with the given heartbeat fields; they leave when the test ends."""
    advertised: list[str] = []

    def advertise(instance_id: str, **fields: Any) -> None:
        advertised.append(instance_id)
        advertise_gateway(authenticated_client, instance_id, **fields)

    yield advertise
    for instance_id in advertised:
        withdraw_gateway(authenticated_client, instance_id)


class TestAccess:
    def test_viewer_can_read_but_not_write(self, viewer_client: TestClient) -> None:
        assert viewer_client.get("/api/v1/geo/settings").status_code == 200
        assert viewer_client.get("/api/v1/geo/databases").status_code == 200
        assert viewer_client.get("/api/v1/geo/status").status_code == 200
        assert (
            viewer_client.put(
                "/api/v1/geo/settings", json={"default_conflict_rule": "first"}
            ).status_code
            == 403
        )
        assert viewer_client.post("/api/v1/geo/reattribute", json={}).status_code == 403

    def test_unauthenticated(self, async_client: TestClient) -> None:
        assert async_client.get("/api/v1/geo/settings").status_code == 401


class TestSettings:
    def test_defaults_then_save(self, authenticated_client: TestClient) -> None:
        current = authenticated_client.get("/api/v1/geo/settings").json()
        assert current["settings"]["default_conflict_rule"] in {"consensus", "first"}
        assert current["settings"]["echo_url"].startswith("https://")

        body = dict(current["settings"])
        body.update(
            {
                "default_conflict_rule": "first",
                "echo_url": "https://echo.example/ip",
                "preflight_max_attempts": 4,
            }
        )
        saved = authenticated_client.put("/api/v1/geo/settings", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["from_database"] is True
        assert saved.json()["settings"]["default_conflict_rule"] == "first"
        assert saved.json()["settings"]["preflight_max_attempts"] == 4

        again = authenticated_client.get("/api/v1/geo/settings").json()
        assert again["settings"]["echo_url"] == "https://echo.example/ip"

    def test_invalid_settings(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.put("/api/v1/geo/settings", json={"default_conflict_rule": "loud"})
        assert response.status_code == 422
        response = authenticated_client.put(
            "/api/v1/geo/settings", json={"preflight_max_attempts": 0}
        )
        assert response.status_code == 422
        for radius in (-1, 501):
            response = authenticated_client.put("/api/v1/geo/settings", json={"default_city_radius_km": radius})
            assert response.status_code == 422

    def test_city_radius_round_trip(self, authenticated_client: TestClient) -> None:
        _clear(authenticated_client)
        body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
        assert body["default_city_radius_km"] == 25
        body["default_city_radius_km"] = 12.5
        saved = authenticated_client.put("/api/v1/geo/settings", json=body)
        assert saved.status_code == 200, saved.text
        assert authenticated_client.get("/api/v1/geo/settings").json()["settings"]["default_city_radius_km"] == 12.5
        _clear(authenticated_client)

    def test_a_new_inherited_policy_re_judges_every_proxy(self, authenticated_client: TestClient) -> None:
        """The conflict rule and city radius projects inherit judge exits; the echo and retention settings do not."""
        _clear(authenticated_client)
        body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
        _drop_jobs(authenticated_client)

        body.update({"echo_timeout_seconds": 9, "observation_retention_days": 3, "preflight_max_attempts": 2})
        assert authenticated_client.put("/api/v1/geo/settings", json=body).status_code == 200
        assert _reattributions(authenticated_client) == []

        body["default_conflict_rule"] = "first"
        assert authenticated_client.put("/api/v1/geo/settings", json=body).status_code == 200
        assert _reattributions(authenticated_client) == ["all"]

        _drop_jobs(authenticated_client)
        body["default_city_radius_km"] = 40
        assert authenticated_client.put("/api/v1/geo/settings", json=body).status_code == 200
        assert _reattributions(authenticated_client) == ["all"]
        _clear(authenticated_client)


class TestSettingsFollowPostgres:
    """Another api's save shows in this api's next answer: the settings routes read the row first.

    The save is stored here as yet unannounced, and the hourly reconcile is
    far off, so only the routes themselves can pick it up.
    """

    @pytest.fixture
    def test_settings(self, test_settings: Settings) -> Settings:
        test_settings.reconcile_interval = 3600.0
        return test_settings

    def test_a_save_made_through_another_api_is_answered_at_once(self, authenticated_client: TestClient) -> None:
        _clear(authenticated_client)
        body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]

        _save_elsewhere(authenticated_client, dict(body, default_conflict_rule="first", default_city_radius_km=40))

        answer = authenticated_client.get("/api/v1/geo/settings").json()
        assert answer["from_database"] is True
        assert answer["settings"]["default_conflict_rule"] == "first"
        assert answer["settings"]["default_city_radius_km"] == 40
        _clear(authenticated_client)

    def test_a_save_is_judged_against_the_stored_policy(self, authenticated_client: TestClient) -> None:
        """Another api stored "first"; saving "consensus" here changes the inherited rule, so proxies are re-judged."""
        _clear(authenticated_client)
        body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
        _save_elsewhere(authenticated_client, dict(body, default_conflict_rule="first"))
        _drop_jobs(authenticated_client)

        assert authenticated_client.put("/api/v1/geo/settings", json=body).status_code == 200

        assert _reattributions(authenticated_client) == ["all"]
        _clear(authenticated_client)


class TestChangeEntries:
    """Each write is announced after its commit: the other api and worker processes read it again then."""

    def test_a_settings_save_appends_one_geo_settings_entry(self, authenticated_client: TestClient) -> None:
        _clear(authenticated_client)
        body = authenticated_client.get("/api/v1/geo/settings").json()["settings"]
        after = stream_head(authenticated_client)

        saved = authenticated_client.put("/api/v1/geo/settings", json=dict(body, echo_timeout_seconds=9))
        assert saved.status_code == 200
        # Refused: nothing stored, nothing announced.
        refused = authenticated_client.put("/api/v1/geo/settings", json={"default_conflict_rule": "loud"})
        assert refused.status_code == 422

        assert announced_since(authenticated_client, after) == [("geo_settings", "")]
        _clear(authenticated_client)

    def test_each_database_write_appends_one_geo_database_entry(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        after = stream_head(authenticated_client)

        record = _upload(authenticated_client, _city_database(tmp_path)).json()
        url = f"/api/v1/geo/databases/{record['id']}"
        assert authenticated_client.patch(url, json={"name": "x"}).status_code == 200
        assert authenticated_client.delete(url).status_code == 204
        # Gone: nothing changed, nothing announced.
        assert authenticated_client.delete(url).status_code == 404
        assert authenticated_client.patch("/api/v1/geo/databases/missing", json={"name": "x"}).status_code == 404

        assert announced_since(authenticated_client, after) == [("geo_database", record["id"])] * 3

    def test_a_database_added_by_url_is_announced_before_its_download(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row stands whatever the download does (here it fails), so the processes listing databases learn of it.

        What the download then writes is the updater's to announce.
        """
        _clear(authenticated_client)
        after = stream_head(authenticated_client)
        redis = api_runtime(authenticated_client).redis_client
        at_download: list[list[tuple[str, str]]] = []

        async def download(record: object, guard: object) -> bytes:
            found = await redis.client.xrange(CHANGES_STREAM_KEY, min=f"({after}")
            at_download.append([(fields["kind"], fields["ref"]) for _, fields in found])
            raise DownloadError("the vendor is down")

        monkeypatch.setattr("api.geo.updater.download", download)
        response = authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={"name": "vendor", "update_url": "https://vendor.example/db.mmdb", "update_interval_hours": 24},
        )
        assert response.status_code == 400
        (row,) = authenticated_client.get("/api/v1/geo/databases").json()["databases"]

        assert at_download == [[("geo_database", row["id"])]]
        _clear(authenticated_client)


class TestDatabases:
    def test_upload_lookup_and_delete(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        _drop_jobs(authenticated_client)
        path = _city_database(tmp_path)

        before = authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()
        assert before["resolution"]["resolved_country"] is None and before["databases_loaded"] == 0

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
        assert record["gateways_loaded"] == 0 and record["gateway_errors"] == {}
        # Every proxy is re-judged with the new database, by a worker.
        assert _reattributions(authenticated_client) == ["all"]

        listed = authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        assert [d["id"] for d in listed] == [record["id"]]

        lookup = authenticated_client.post(
            "/api/v1/geo/lookup", json={"ip": GB_IP, "claimed_country": "us"}
        ).json()
        assert lookup["resolution"]["resolved_country"] == "GB" and lookup["resolution"]["country_conflict"] is True
        assert lookup["candidates"][0]["location"]["city"] == "London"

        settings = authenticated_client.get("/api/v1/geo/settings").json()
        assert [d["id"] for d in settings["databases_loaded"]] == [record["id"]]

        # A new name judges nothing.
        _drop_jobs(authenticated_client)
        renamed = authenticated_client.patch(f"/api/v1/geo/databases/{record['id']}", json={"name": "Renamed"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
        assert _reattributions(authenticated_client) == []

        patched = authenticated_client.patch(
            f"/api/v1/geo/databases/{record['id']}", json={"enabled": False, "name": "Off"}
        )
        assert (
            patched.status_code == 200
            and patched.json()["enabled"] is False
            and patched.json()["name"] == "Off"
        )
        assert patched.json()["loaded_here"] is False
        assert _reattributions(authenticated_client) == ["all"]
        assert (
            authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()[
                "resolution"
            ]["resolved_country"]
            is None
        )

        # Moving a database nothing consults judges nothing either.
        _drop_jobs(authenticated_client)
        moved = authenticated_client.patch(f"/api/v1/geo/databases/{record['id']}", json={"priority": 5})
        assert moved.status_code == 200 and moved.json()["priority"] == 5
        assert _reattributions(authenticated_client) == []

        assert authenticated_client.patch(
            f"/api/v1/geo/databases/{record['id']}", json={"enabled": True}
        ).json()["loaded_here"]
        assert _reattributions(authenticated_client) == ["all"]

        _drop_jobs(authenticated_client)
        assert (
            authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}").status_code == 204
        )
        assert _reattributions(authenticated_client) == ["all"]
        assert record["id"] not in [
            d["id"] for d in authenticated_client.get("/api/v1/geo/databases").json()["databases"]
        ]
        assert (
            authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}").status_code == 404
        )
        assert authenticated_client.patch("/api/v1/geo/databases/missing", json={"name": "x"}).status_code == 404

    def test_a_disabled_upload_judges_nothing(self, authenticated_client: TestClient, tmp_path: Path) -> None:
        _clear(authenticated_client)
        _drop_jobs(authenticated_client)
        response = _upload(authenticated_client, _city_database(tmp_path), enabled=False)
        assert response.status_code == 201, response.text
        assert response.json()["enabled"] is False and response.json()["loaded_here"] is False
        assert _reattributions(authenticated_client) == []
        assert authenticated_client.delete(f"/api/v1/geo/databases/{response.json()['id']}").status_code == 204
        assert _reattributions(authenticated_client) == []

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
            lookup["resolution"]["resolved_country"] == "DE" and lookup["resolution"]["country_conflict"] is None
        )
        assert [c["country"] for c in lookup["candidates"]] == ["DE", "GB"]

    def test_geonames_file_judges_city_claims_by_distance(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        city = write_mmdb(tmp_path / "GeoLite2-City.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        assert _upload(authenticated_client, city).status_code == 201
        claim = {"ip": GB_IP, "claimed_country": "GB", "claimed_city": "Croydon"}
        before = authenticated_client.post("/api/v1/geo/lookup", json=claim).json()["resolution"]
        assert before["city_conflict"] is True and before["city_distance_km"] is None  # names only

        response = _upload(authenticated_client, write_geonames(tmp_path / "cities500.zip", zipped=True))
        assert response.status_code == 201, response.text
        record = response.json()
        assert (record["vendor"], record["kind"], record["format"]) == ("geonames", "places", "geonames")
        assert record["database_type"] == "GeoNames cities500" and "GeoNames" in record["attribution"]
        assert record["loaded_here"] is True and record["name"] == "cities500"

        lookup = authenticated_client.post("/api/v1/geo/lookup", json=claim).json()
        # The places file answers no IP; it places the claimed city, 13 km from the database's London.
        assert len(lookup["candidates"]) == 2 and lookup["policy"]["city_radius_km"] == 25
        assert lookup["resolution"]["city_conflict"] is False
        assert 12 < lookup["resolution"]["city_distance_km"] < 14.5
        # A GeoNames file answers no IP, so it does not count as an IP database loaded.
        assert lookup["databases_loaded"] == 1

        project = authenticated_client.post(
            "/api/v1/projects",
            json={"name": "strict-city", "username": "strict_city", "password": "pw", "location_city_radius_km": 5},
        )
        assert project.status_code == 201, project.text
        assert project.json()["location_city_radius_km"] == 5
        scoped = authenticated_client.post("/api/v1/geo/lookup", json={**claim, "project_id": project.json()["id"]}).json()
        assert scoped["policy"]["city_radius_km"] == 5 and scoped["resolution"]["city_conflict"] is True

        cleared = authenticated_client.patch(f"/api/v1/projects/{project.json()['id']}", json={"location_city_radius_km": ""})
        assert cleared.status_code == 200 and cleared.json()["location_city_radius_km"] is None
        inheriting = authenticated_client.post("/api/v1/geo/lookup", json={**claim, "project_id": project.json()["id"]}).json()
        assert inheriting["policy"]["city_radius_km"] == 25 and inheriting["resolution"]["city_conflict"] is False
        assert authenticated_client.patch(
            f"/api/v1/projects/{project.json()['id']}", json={"location_city_radius_km": -3}
        ).status_code == 422

        assert authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}").status_code == 204
        after = authenticated_client.post("/api/v1/geo/lookup", json=claim).json()["resolution"]
        assert after["city_conflict"] is True
        _clear(authenticated_client)

    def test_rejects_junk(self, authenticated_client: TestClient, tmp_path: Path) -> None:
        junk = tmp_path / "junk.mmdb"
        junk.write_bytes(b"not a database" * 50)
        before = len(authenticated_client.get("/api/v1/geo/databases").json()["databases"])
        assert _upload(authenticated_client, junk).status_code == 422
        assert len(authenticated_client.get("/api/v1/geo/databases").json()["databases"]) == before
        empty = tmp_path / "empty.mmdb"
        empty.write_bytes(b"")
        assert _upload(authenticated_client, empty).status_code == 400

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
        _drop_jobs(authenticated_client)
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
        # No file, nothing to judge with.
        assert _reattributions(authenticated_client) == []

    def test_pausing_a_schedule_keeps_it(self, authenticated_client: TestClient) -> None:
        _clear(authenticated_client)
        authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={"name": "blocked", "update_url": "http://127.0.0.1:1/db.mmdb", "update_interval_hours": 24},
        )
        row = authenticated_client.get("/api/v1/geo/databases").json()["databases"][0]
        assert row["update_paused"] is False and row["next_update_at"] is not None

        paused = authenticated_client.patch(f"/api/v1/geo/databases/{row['id']}", json={"update_paused": True})
        assert paused.status_code == 200
        assert paused.json()["update_paused"] is True and paused.json()["next_update_at"] is None
        assert paused.json()["update_url"] == row["update_url"] and paused.json()["update_interval_hours"] == 24

        resumed = authenticated_client.patch(f"/api/v1/geo/databases/{row['id']}", json={"update_paused": False})
        assert resumed.json()["update_paused"] is False and resumed.json()["next_update_at"] == row["next_update_at"]

        # Disabling stops the schedule too, and enabling brings it back as it was.
        disabled = authenticated_client.patch(f"/api/v1/geo/databases/{row['id']}", json={"enabled": False})
        assert disabled.json()["next_update_at"] is None and disabled.json()["update_paused"] is False
        enabled = authenticated_client.patch(f"/api/v1/geo/databases/{row['id']}", json={"enabled": True})
        assert enabled.json()["next_update_at"] == row["next_update_at"]

    def test_a_download_asks_for_one_re_attribution(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """From-url and refresh download here; the updater asks for the pass when it stored a new file, once."""
        _clear(authenticated_client)
        _serve_downloads(monkeypatch, _city_database(tmp_path, "first build"))
        _drop_jobs(authenticated_client)

        added = authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={"name": "vendor", "update_url": "https://vendor.example/GeoLite2-City.mmdb"},
        )
        assert added.status_code == 201, added.text
        row = added.json()
        assert row["sha256"] and row["has_file"] is True and row["last_update_error"] is None
        assert row["loaded_here"] is True
        assert _reattributions(authenticated_client) == ["all"]
        lookup = authenticated_client.post("/api/v1/geo/lookup", json={"ip": GB_IP}).json()
        assert lookup["resolution"]["resolved_country"] == "GB"

        # The same file again: nothing changed, nothing to re-judge.
        _drop_jobs(authenticated_client)
        same = authenticated_client.post(f"/api/v1/geo/databases/{row['id']}/refresh")
        assert same.status_code == 200 and same.json()["sha256"] == row["sha256"]
        assert _reattributions(authenticated_client) == []

        # A new build is opened here at once and asks for the pass once.
        _serve_downloads(monkeypatch, _city_database(tmp_path, "next build"))
        newer = authenticated_client.post(f"/api/v1/geo/databases/{row['id']}/refresh")
        assert newer.status_code == 200 and newer.json()["sha256"] != row["sha256"]
        assert newer.json()["loaded_here"] is True
        assert _reattributions(authenticated_client) == ["all"]
        _clear(authenticated_client)

    def test_vendor_credentials_are_kept_but_never_shown(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Any signed-in user may list databases: an answer says whether credentials are stored, never what they are."""
        _clear(authenticated_client)
        _serve_downloads(monkeypatch, _city_database(tmp_path))
        added = authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={
                "name": "vendor",
                "update_url": "https://vendor.example/GeoLite2-City.mmdb",
                "update_auth": {"username": "acct", "password": "vendor-secret"},
            },
        )
        assert added.status_code == 201, added.text
        row = added.json()
        assert row["has_update_auth"] is True and "update_auth" not in row
        assert _row(authenticated_client, row["id"])["has_update_auth"] is True
        assert "vendor-secret" not in authenticated_client.get("/api/v1/geo/databases").text

        cleared = authenticated_client.patch(f"/api/v1/geo/databases/{row['id']}", json={"update_auth": {}})
        assert cleared.status_code == 200 and cleared.json()["has_update_auth"] is False
        _clear(authenticated_client)

    def test_a_token_in_the_download_url_is_never_shown_and_survives_an_edit(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """IPinfo takes its token in the query and others in the user part: both are masked, and kept as stored."""
        _clear(authenticated_client)
        data = _city_database(tmp_path).read_bytes()
        fetched: list[str | None] = []

        async def download(record: GeoDatabaseRecord, guard: object) -> bytes:
            fetched.append(record.update_url)
            return data

        monkeypatch.setattr("api.geo.updater.download", download)
        url = "https://acct:url-key@vendor.example/country_asn.mmdb?token=url-token&suffix=mmdb"
        added = authenticated_client.post(
            "/api/v1/geo/databases/from-url", json={"name": "vendor", "update_url": url}
        )
        assert added.status_code == 201, added.text
        row = added.json()
        shown = "https://***@vendor.example/country_asn.mmdb?token=***&suffix=***"
        assert row["update_url"] == shown and _row(authenticated_client, row["id"])["update_url"] == shown
        listed = authenticated_client.get("/api/v1/geo/databases").text
        assert "url-token" not in listed and "url-key" not in listed

        # An edit that sends the row back as shown keeps the stored URL: the next download still has the token.
        edited = authenticated_client.patch(
            f"/api/v1/geo/databases/{row['id']}", json={"update_url": shown, "priority": 7}
        )
        assert edited.status_code == 200 and edited.json()["priority"] == 7
        assert authenticated_client.post(f"/api/v1/geo/databases/{row['id']}/refresh").status_code == 200
        assert fetched == [url, url]

        # A new URL replaces it.
        replaced = authenticated_client.patch(
            f"/api/v1/geo/databases/{row['id']}", json={"update_url": "https://vendor.example/other.mmdb"}
        )
        assert replaced.json()["update_url"] == "https://vendor.example/other.mmdb"
        _clear(authenticated_client)

    def test_refresh_needs_a_download_url_and_reports_failures(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        assert authenticated_client.post("/api/v1/geo/databases/missing/refresh").status_code == 404
        uploaded = _upload(authenticated_client, _city_database(tmp_path)).json()
        assert authenticated_client.post(f"/api/v1/geo/databases/{uploaded['id']}/refresh").status_code == 400

        authenticated_client.post(
            "/api/v1/geo/databases/from-url",
            json={"name": "blocked", "update_url": "http://127.0.0.1:1/db.mmdb"},
        )
        blocked = next(
            row for row in authenticated_client.get("/api/v1/geo/databases").json()["databases"]
            if row["source"] == "url"
        )
        failed = authenticated_client.post(f"/api/v1/geo/databases/{blocked['id']}/refresh")
        assert failed.status_code == 502 and failed.json()["detail"]
        _clear(authenticated_client)


class TestDeleteOfAnOpenDatabase:
    """A delete of a database this process has open with no row in Postgres: refused only for a config file.

    The routes announce their writes to a log no refresher follows, and no full read comes within the hour, so this
    process keeps open what each test leaves open.
    """

    @pytest.fixture
    def configured(self, tmp_path: Path) -> Path:
        """A city database file the config names."""
        return _city_database(tmp_path, "configured")

    @pytest.fixture
    def test_settings(self, test_settings: Settings, configured: Path) -> Settings:
        test_settings.reconcile_interval = 3600.0
        test_settings.geo_databases = [{"path": str(configured)}]
        return test_settings

    @pytest.fixture(autouse=True)
    def unheard(self, monkeypatch: pytest.MonkeyPatch) -> Unheard:
        """The geo routes announce their writes here instead: this api's refresher never hears of them."""
        log = Unheard()
        monkeypatch.setattr("api.routes.geo.change_log_of", lambda request: log)
        return log

    def test_a_config_file_database_is_refused(self, authenticated_client: TestClient, configured: Path) -> None:
        database_id = path_database_id(str(configured))
        store = api_runtime(authenticated_client).geo_database_store
        assert store.is_loaded(database_id)

        response = authenticated_client.delete(f"/api/v1/geo/databases/{database_id}")

        assert response.status_code == 400
        assert response.json()["detail"] == "Operator-managed databases are removed from the config file"
        assert store.is_loaded(database_id)

    def test_a_stored_database_still_open_after_its_delete_is_not_found(
        self, authenticated_client: TestClient, tmp_path: Path
    ) -> None:
        """A read of the list made just before the delete committed opens it here again, until the refresher's next read."""
        created = _upload(authenticated_client, _city_database(tmp_path))
        assert created.status_code == 201, created.text
        database_id = created.json()["id"]
        runtime = api_runtime(authenticated_client)

        async def stored() -> GeoDatabaseRecord | None:
            async with runtime.session_factory() as session:
                return await GeoDatabaseRepository(session).get_by_id(database_id)

        record = on_app_loop(authenticated_client, stored)
        assert record is not None
        assert authenticated_client.delete(f"/api/v1/geo/databases/{database_id}").status_code == 204
        on_app_loop(authenticated_client, runtime.geo_database_store.apply_record, record)
        assert runtime.geo_database_store.is_loaded(database_id)

        again = authenticated_client.delete(f"/api/v1/geo/databases/{database_id}")

        assert again.status_code == 404 and again.json()["detail"] == "Database not found"


class TestGateways:
    """Where the databases are open and what preflight did come from the gateways' heartbeats."""

    def test_a_database_says_on_how_many_gateways_it_is_open(
        self, authenticated_client: TestClient, gateways: Callable[..., None], tmp_path: Path
    ) -> None:
        _clear(authenticated_client)
        record = _upload(authenticated_client, _city_database(tmp_path)).json()
        total = _row(authenticated_client, record["id"])["gateways_total"]

        gateways("gw-test-a", geo_databases_loaded=[record["id"]])
        gateways(
            "gw-test-b",
            geo_databases_missing=[record["id"]],
            geo_database_errors={record["id"]: "fetch failed: connection refused"},
        )
        gateways("gw-test-c")

        row = _row(authenticated_client, record["id"])
        assert row["loaded_here"] is True
        assert (row["gateways_loaded"], row["gateways_total"]) == (1, total + 3)
        assert row["gateway_errors"] == {"gw-test-b": "fetch failed: connection refused"}
        # A write's answer reads them too.
        patched = authenticated_client.patch(f"/api/v1/geo/databases/{record['id']}", json={"name": "Renamed"}).json()
        assert (patched["gateways_loaded"], patched["gateways_total"]) == (1, total + 3)
        _clear(authenticated_client)

    def test_status_sums_preflight_and_lists_each_gateway(
        self, authenticated_client: TestClient, gateways: Callable[..., None]
    ) -> None:
        baseline = authenticated_client.get("/api/v1/geo/status").json()
        gateways(
            "gw-test-a",
            preflight_checks=7,
            preflight_rejections=2,
            geo_databases_loaded=["db-1"],
            observations_pending=3,
        )
        gateways(
            "gw-test-b",
            preflight_checks=5,
            geo_databases_missing=["db-1"],
            geo_database_errors={"db-1": "fetch failed: 404"},
            sightings_pending=6,
            sightings_dropped=4,
        )

        status = authenticated_client.get("/api/v1/geo/status").json()
        assert status["preflight_checks"] == baseline["preflight_checks"] + 12
        assert status["preflight_rejections"] == baseline["preflight_rejections"] + 2
        by_id = {gateway["instance_id"]: gateway for gateway in status["gateways"]}
        assert by_id["gw-test-a"] == {
            "instance_id": "gw-test-a",
            "databases_loaded": 1,
            "databases_missing": [],
            "load_errors": {},
            "preflight_checks": 7,
            "preflight_rejections": 2,
            "observations_pending": 3,
            "observations_dropped": 0,
            "sightings_pending": 0,
            "sightings_dropped": 0,
        }
        assert by_id["gw-test-b"]["databases_missing"] == ["db-1"]
        assert by_id["gw-test-b"]["load_errors"] == {"db-1": "fetch failed: 404"}
        assert (by_id["gw-test-b"]["sightings_pending"], by_id["gw-test-b"]["sightings_dropped"]) == (6, 4)

    def test_unreadable_heartbeats_show_no_gateways(
        self, authenticated_client: TestClient, gateways: Callable[..., None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateways("gw-test-a", preflight_checks=3)

        async def unreachable() -> dict[str, Any]:
            raise RedisConnectionError("Connection refused")

        monkeypatch.setattr(api_runtime(authenticated_client), "instance_snapshots", unreachable)
        status = authenticated_client.get("/api/v1/geo/status")
        assert status.status_code == 200
        assert status.json()["gateways"] == [] and status.json()["preflight_checks"] == 0
        listed = authenticated_client.get("/api/v1/geo/databases")
        assert listed.status_code == 200
        assert all(row["gateways_total"] == 0 for row in listed.json()["databases"])

    def test_heartbeats_cut_by_a_deadline_are_logged_by_its_type(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type."""
        monkeypatch.setattr(
            api_runtime(authenticated_client), "instance_snapshots", AsyncMock(side_effect=TimeoutError)
        )

        with patch("api.routes.geo.logger") as log:
            status = authenticated_client.get("/api/v1/geo/status")

        assert status.status_code == 200 and status.json()["gateways"] == []
        log.warning.assert_called_once_with("Gateway heartbeats unavailable, shown without them", error="TimeoutError")


def _add_proxy(client: TestClient, connector_id: str, **metadata: Any) -> Proxy:
    """A healthy proxy exiting at ``GB_IP``, stored as a job stores one (the proxies routes are another module's)."""
    proxy = Proxy(
        host=GB_IP, port=8080, protocol=ProxyProtocol.HTTP, connector_id=connector_id,
        status=ProxyStatus.HEALTHY, metadata=metadata,
    )
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)
    return proxy


def _stored(client: TestClient, proxy_id: str) -> Proxy:
    async def read() -> Proxy | None:
        async with api_runtime(client).session_factory() as session:
            return await ProxyRepository(session).get_by_id(proxy_id)

    proxy = on_app_loop(client, read)
    assert proxy is not None
    return proxy


def _run_reattribution(client: TestClient, key: str) -> None:
    """Run a queued re-attribution key as a worker would, with this process's attribution stack."""
    runtime = api_runtime(client)
    job = ReattributeJob(runtime.attribution, runtime.geo_service)
    on_app_loop(client, job.run, JobBatch(ids=("1-0",), keys=(key,)))


class TestReattribute:
    def test_queues_every_proxy_or_one_connector(
        self, authenticated_client: TestClient, created_connector: dict[str, Any]
    ) -> None:
        connector_id = created_connector["id"]
        _drop_jobs(authenticated_client)
        everything = authenticated_client.post("/api/v1/geo/reattribute", json={})
        assert everything.status_code == 202
        assert everything.json() == {"kind": "reattribute", "key": "all"}
        one = authenticated_client.post("/api/v1/geo/reattribute", json={"connector_id": connector_id})
        assert one.status_code == 202
        assert one.json() == {"kind": "reattribute", "key": f"connector:{connector_id}"}
        assert authenticated_client.post("/api/v1/geo/reattribute", json={"connector_id": "missing"}).status_code == 404
        # Each press is an entry of its own: a worker reading them together runs ``all`` once.
        assert authenticated_client.post("/api/v1/geo/reattribute", json={}).status_code == 202
        assert _reattributions(authenticated_client) == ["all", f"connector:{connector_id}", "all"]

    def test_answers_503_when_the_job_stream_does_not_take_the_key(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis refused it, or did not answer within the request deadline: nothing was queued, so it says so."""

        async def refused(*args: object) -> int:
            raise RedisConnectionError("Connection refused")

        monkeypatch.setattr(api_runtime(authenticated_client).jobs, "publish", refused)
        response = authenticated_client.post("/api/v1/geo/reattribute", json={})
        assert response.status_code == 503 and "try again" in response.json()["detail"]
        assert response.headers["Retry-After"] == "5"

    def test_the_queued_pass_judges_with_the_uploaded_database(
        self, authenticated_client: TestClient, created_connector: dict[str, Any], tmp_path: Path
    ) -> None:
        """A manual country stays for routing but is flagged once a database places the exit elsewhere."""
        _clear(authenticated_client)
        proxy = _add_proxy(
            authenticated_client, created_connector["id"], discovered_ip=GB_IP, country="US", country_source="manual"
        )
        assert _upload(authenticated_client, _city_database(tmp_path)).status_code == 201

        key = authenticated_client.post(
            "/api/v1/geo/reattribute", json={"connector_id": created_connector["id"]}
        ).json()["key"]
        _run_reattribution(authenticated_client, key)

        metadata = _stored(authenticated_client, proxy.id).metadata
        assert metadata["country"] == "US" and metadata["country_source"] == "manual"
        assert metadata["location_conflict"] is True and metadata["location"]["city"] == "London"
        _clear(authenticated_client)


class TestProjectPolicies:
    def test_project_location_decisions_roundtrip(self, authenticated_client: TestClient) -> None:
        created = authenticated_client.post(
            "/api/v1/projects",
            json={
                "name": "strict-p",
                "username": "strict_user",
                "password": "pw",
                "location_contradicted": "refuse",
                "location_unknown": "refuse",
                "location_preflight": True,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["location_contradicted"] == "refuse" and body["location_unknown"] == "refuse"
        assert body["location_preflight"] is True
        updated = authenticated_client.patch(
            f"/api/v1/projects/{body['id']}", json={"location_preflight": False, "location_contradicted": "vendor"}
        ).json()
        assert updated["location_preflight"] is False
        assert updated["location_contradicted"] == "vendor" and updated["location_unknown"] == "refuse"
        listed = authenticated_client.get("/api/v1/projects").json()["projects"]
        assert any(p["location_unknown"] == "refuse" for p in listed)
        assert (
            authenticated_client.patch(
                f"/api/v1/projects/{body['id']}", json={"location_contradicted": "strict"}
            ).status_code
            == 422
        )
        assert (
            authenticated_client.patch(
                f"/api/v1/projects/{body['id']}", json={"location_unknown": "evidence"}
            ).status_code
            == 422
        )
        # A fresh project routes on the evidence, trusts the vendor where nothing is known, preflight off.
        plain = authenticated_client.post(
            "/api/v1/projects", json={"name": "plain-p", "username": "plain_user", "password": "pw"}
        ).json()
        assert plain["location_contradicted"] == "evidence" and plain["location_unknown"] == "vendor"
        assert plain["location_preflight"] is False

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
                "location_conflict_rule": "first",
                "location_preflight": True,
            },
        )
        assert created.status_code == 201, created.text
        project = created.json()
        assert project["location_conflict_rule"] == "first" and "location_sources" not in project
        assert project["location_preflight"] is True

        default = authenticated_client.post(
            "/api/v1/geo/lookup", json={"ip": GB_IP, "claimed_country": "US"}
        ).json()
        assert (
            default["resolution"]["resolved_country"] == "GB"
            and default["policy"]["conflict_rule"] == "consensus"
        )
        scoped = authenticated_client.post(
            "/api/v1/geo/lookup",
            json={"ip": GB_IP, "claimed_country": "US", "project_id": project["id"]},
        ).json()
        # The value is the database's under either rule; the project's rule travels with the lookup.
        assert scoped["resolution"]["resolved_country"] == "GB" and scoped["policy"]["conflict_rule"] == "first"
        assert scoped["resolution"]["country_conflict"] is True

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


def _contradicted_exit(client: TestClient, connector_id: str, project_id: str) -> None:
    """What a worker's observation ingest writes for one contradicted health check of the connector."""
    now = utc_now()

    async def flushed() -> None:
        async with api_runtime(client).session_factory() as session:
            repository = ObservationRepository(session)
            await repository.insert_many(
                [
                    IpObservation(
                        observed_at=now, connector_id=connector_id, project_id=project_id,
                        source=ObservationSource.HEALTH_CHECK, ip="203.0.113.7", claimed_country="US",
                        resolved_country="GB", country_conflict=True,
                    )
                ]
            )
            await repository.add_exit_ips(
                {
                    (connector_id, "203.0.113.7"): ExitSighting(
                        first_seen=now, last_seen=now, country="GB", source="health_check",
                        claimed_country="US", country_conflict=True,
                    )
                }
            )
            await session.commit()

    on_app_loop(client, flushed)


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
            and page["total_exact"] is True
        )
        # The exact count takes the same filters; a time with an offset is read as UTC.
        counted = authenticated_client.get(
            "/api/v1/geo/observations/count?source=health_check&until=2026-10-16T12:00:00%2B02:00"
        )
        assert counted.status_code == 200 and counted.json()["total"] >= 0
        assert authenticated_client.get("/api/v1/geo/observations/count?verdict=bogus").status_code == 422
        accuracy = authenticated_client.get("/api/v1/geo/accuracy?days=7").json()
        assert isinstance(accuracy["connectors"], list) and accuracy["since"]
        for row in accuracy["connectors"]:
            assert set(row) >= {"exits", "country", "state", "city", "breakdown", "coverage"}
        status = authenticated_client.get("/api/v1/geo/status").json()
        assert status["databases_total"] >= status["databases_loaded"]
        assert status["preflight_checks"] >= 0 and status["stored_observations"] >= 0
        assert isinstance(status["queued_observations"], int) and status["queued_observations"] >= 0
        assert isinstance(status["gateways"], list)

    def test_exits_endpoints(self, authenticated_client: TestClient) -> None:
        summary = authenticated_client.get("/api/v1/geo/exits?days=30").json()
        assert isinstance(summary["connectors"], list) and summary["since"]
        ips = authenticated_client.get("/api/v1/geo/exits/ips?connector_id=none&limit=5").json()
        assert ips == {"ips": [], "total": 0, "total_exact": True, "limit": 5, "offset": 0}
        assert authenticated_client.get("/api/v1/geo/exits/ips/count?connector_id=none").json() == {"total": 0}
        assert authenticated_client.get("/api/v1/geo/exits/ips?verdict=bogus").status_code == 422
        assert authenticated_client.get("/api/v1/geo/exits/ips?project_id=missing").json()["total"] == 0
        assert authenticated_client.get("/api/v1/geo/exits/ips/count?project_id=missing").json() == {"total": 0}

    def test_rows_are_named_from_the_connector_and_project_rows(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any]
    ) -> None:
        connector_id, project_id = created_connector["id"], created_project["id"]
        names = (created_connector["name"], created_project["name"])
        _contradicted_exit(authenticated_client, connector_id, project_id)

        page = authenticated_client.get("/api/v1/geo/observations", params={"connector_id": connector_id}).json()
        assert [(row["connector_name"], row["project_name"]) for row in page["observations"]] == [names]
        ips = authenticated_client.get("/api/v1/geo/exits/ips", params={"project_id": project_id}).json()["ips"]
        assert [(row["ip"], row["connector_name"], row["project_id"], row["project_name"]) for row in ips] == [
            ("203.0.113.7", *names[:1], project_id, names[1])
        ]
        assert authenticated_client.get(
            "/api/v1/geo/exits/ips/count", params={"project_id": project_id}
        ).json() == {"total": 1}
        accuracy = authenticated_client.get("/api/v1/geo/accuracy", params={"project_id": project_id}).json()
        assert [
            (row["connector_id"], row["connector_name"], row["project_id"], row["coverage"])
            for row in accuracy["connectors"]
        ] == [(connector_id, names[0], project_id, None)]
        assert accuracy["connectors"][0]["country"]["contradicted"] == 1
        exits = authenticated_client.get("/api/v1/geo/exits", params={"connector_id": connector_id}).json()
        assert [(row["connector_name"], row["unique_total"]) for row in exits["connectors"]] == [(names[0], 1)]

    def test_sharing_compares_a_project_s_connectors(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        second = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={**sample_connector_data, "name": "Second", "credential_id": created_connector["credential_id"]},
        )
        assert second.status_code == 201, second.text
        ids = sorted([created_connector["id"], second.json()["id"]])
        for connector_id in ids:
            _contradicted_exit(authenticated_client, connector_id, project_id)

        sharing = authenticated_client.get("/api/v1/geo/exits/sharing", params={"project_id": project_id}).json()
        assert (sharing["exits"], sharing["shared"]) == (1, 1)
        assert sorted(
            (row["connector_name"], row["provider"], row["exits"], row["shared"]) for row in sharing["connectors"]
        ) == sorted((name, created_connector["credential_type"], 1, 1) for name in (created_connector["name"], "Second"))
        assert sharing["pairs"] == [{"connector_id": ids[0], "other_id": ids[1], "shared": 1}]
        assert authenticated_client.get("/api/v1/geo/exits/sharing", params={"project_id": "missing"}).status_code == 404
        assert authenticated_client.get("/api/v1/geo/exits/sharing").status_code == 422

    def test_a_dynamic_sessions_connector_s_rows_say_what_preflight_lets_them_see(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        """Only preflight sees a dynamic connector's exits: all with it off, then the share of session-less requests."""
        project_id = created_project["id"]
        credential = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "Oxylabs", "type": "oxylabs", "config": OXYLABS_CREDENTIAL},
        )
        assert credential.status_code == 201, credential.text
        connector = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "Dynamic", "credential_id": credential.json()["id"], "config": OXYLABS_DYNAMIC},
        )
        assert connector.status_code == 201, connector.text
        connector_id = connector.json()["id"]
        _contradicted_exit(authenticated_client, connector_id, project_id)

        def coverage() -> list[Any]:
            return [
                row["coverage"]
                for path in ("/api/v1/geo/accuracy", "/api/v1/geo/exits")
                for row in authenticated_client.get(path, params={"connector_id": connector_id}).json()["connectors"]
            ]

        assert coverage() == [{"dynamic": True, "preflight_on": False, "sampled_percent": 0}] * 2
        preflight = authenticated_client.patch(f"/api/v1/projects/{project_id}", json={"location_preflight": True})
        assert preflight.status_code == 200, preflight.text
        assert coverage() == [{"dynamic": True, "preflight_on": True, "sampled_percent": 5}] * 2

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
