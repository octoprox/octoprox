# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the admin system statistics endpoints."""

from datetime import timedelta
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from api.core import utc_now
from api.core.config import Settings
from api.db.models import SystemMetricsModel

ENDPOINT = "/api/v1/system/stats"
HISTORY = "/api/v1/system/stats/history"


class TestSystemStatsAccessControl:
    def test_unauthenticated_is_rejected(self, async_client: TestClient) -> None:
        assert async_client.get(ENDPOINT).status_code == 401

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(ENDPOINT).status_code == 403

    def test_editor_is_forbidden(self, editor_client: TestClient) -> None:
        assert editor_client.get(ENDPOINT).status_code == 403

    def test_admin_is_allowed(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(ENDPOINT).status_code == 200


class TestSystemStatsPayload:
    def test_runtime_describes_this_instance(
        self, authenticated_client: TestClient, test_settings: Any
    ) -> None:
        runtime = authenticated_client.get(ENDPOINT).json()["runtime"]

        assert runtime["instance_id"] == test_settings.instance_id
        assert runtime["role"] == "all"
        assert runtime["pid"] > 0
        assert runtime["uptime_seconds"] >= 0
        assert runtime["started_at"] is not None
        assert runtime["metrics_flush_interval"] == test_settings.metrics_flush_interval
        # Tests configure port 0, so this must be the port actually bound.
        assert runtime["proxy_port"] > 0

    def test_inventory_and_project_usage_follow_created_entities(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        sample_credential_data: dict[str, Any],
        sample_connector_data: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Creating one of each entity moves every matching counter by one.

        Tables are shared across tests in this module, so the assertions are
        deltas against a snapshot rather than absolute counts.
        """
        before = authenticated_client.get(ENDPOINT).json()["inventory"]

        project = authenticated_client.post("/api/v1/projects", json=sample_project_data).json()
        credential = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/credentials", json=sample_credential_data
        ).json()
        connector = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/connectors",
            json={**sample_connector_data, "credential_id": credential["id"]},
        ).json()
        authenticated_client.post(
            f"/api/v1/projects/{project['id']}/proxies",
            json={**sample_proxy_data, "connector_id": connector["id"]},
        )

        data = authenticated_client.get(ENDPOINT).json()
        after = data["inventory"]

        assert after["projects"] == before["projects"] + 1
        assert after["credentials"] == before["credentials"] + 1
        assert after["connectors"] == before["connectors"] + 1
        assert after["connectors_enabled"] == before["connectors_enabled"] + 1
        assert after["proxies"] == before["proxies"] + 1
        # The seeded admin is always present and roles add up to the total.
        assert after["users"] >= 1
        assert sum(after["users_by_role"].values()) == after["users"]
        # Health status comes from the live pool, not from Postgres.
        assert sum(after["proxies_by_status"].values()) == after["proxies"]
        assert after["providers_builtin"] > 0

        usage = {p["id"]: p for p in data["projects"]}
        assert usage[project["id"]] == {
            "id": project["id"],
            "name": project["name"],
            "credentials": 1,
            "connectors": 1,
            "proxies": 1,
        }

    def test_database_reports_sizes_for_known_tables(
        self, authenticated_client: TestClient
    ) -> None:
        database = authenticated_client.get(ENDPOINT).json()["database"]

        assert database["error"] is None
        assert database["size_bytes"] > 0
        assert database["backends"] is not None and database["backends"] >= 1
        names = {t["name"] for t in database["tables"]}
        assert {"projects", "proxies", "proxy_metrics", "project_metrics"} <= names
        for table in database["tables"]:
            assert table["total_bytes"] >= table["table_bytes"]
            # Unknown until autovacuum analyses the table, never a bogus zero.
            assert table["row_estimate"] is None or table["row_estimate"] >= 0

    def test_redis_reports_memory_and_keyspace(
        self, authenticated_client: TestClient
    ) -> None:
        redis = authenticated_client.get(ENDPOINT).json()["redis"]

        assert redis["error"] is None
        assert redis["version"]
        assert redis["used_memory_bytes"] > 0
        assert redis["truncated"] is False
        # The heartbeat loop always has a key in flight, so the keyspace is
        # never empty while the app is running.
        assert redis["total_keys"] >= 1
        assert any(group["label"] == "Instance heartbeats" for group in redis["groups"])

    def test_cache_mirrors_the_database_inventory(
        self, authenticated_client: TestClient, created_proxy: dict[str, Any]
    ) -> None:
        data = authenticated_client.get(ENDPOINT).json()
        cache, inventory = data["cache"], data["inventory"]

        assert cache["projects"] == inventory["projects"]
        assert cache["credentials"] == inventory["credentials"]
        assert cache["connectors"] == inventory["connectors"]
        assert cache["proxies"] == inventory["proxies"]
        assert cache["quarantined_proxies"] == 0
        assert cache["provider_types"] > 0

    def test_workers_list_named_background_loops(
        self, authenticated_client: TestClient
    ) -> None:
        workers = authenticated_client.get(ENDPOINT).json()["workers"]

        by_name = {task["name"]: task for task in workers["tasks"]}
        # Spelled out rather than taken from WorkerName: these names are the
        # wire contract, so renaming a member should fail here, not pass
        # silently because both sides moved together.
        expected = {
            "health_checker",
            "metrics_flusher",
            "metrics_compactor",
            "auto_scaler",
            "provider_syncer",
            "heartbeat",
            "full_reload",
            "metric_delta_publisher",
            "metric_delta_subscriber",
            "cross_instance_subscriber",
        }
        assert expected <= set(by_name)
        assert all(task["state"] == "running" for task in by_name.values())
        # Every spawned loop is described; an undescribed one is a loop that was
        # added without being added to WORKERS.
        assert all(task["description"] for task in by_name.values())

    def test_workers_say_which_are_leader_elected(
        self, authenticated_client: TestClient
    ) -> None:
        """Each singleton task names the lease that decides where it runs."""
        workers = authenticated_client.get(ENDPOINT).json()["workers"]
        by_name = {task["name"]: task for task in workers["tasks"]}

        assert by_name["metrics_flusher"]["scope"] == "singleton"
        assert by_name["metrics_flusher"]["lease"] == "metrics_flusher"
        assert by_name["auto_scaler"]["lease"] == "autoscaler"
        # Runs on every instance, so no lease to point at.
        assert by_name["health_checker"]["scope"] == "instance"
        assert by_name["health_checker"]["lease"] is None

        # A lease and its worker resolve to each other, which is what lets the
        # UI line the two lists up.
        for lease in workers["leases"]:
            assert by_name[lease["worker"]]["lease"] == lease["name"].partition(":")[0]

    def test_workers_count_the_cycles_they_ran(
        self, authenticated_client: TestClient
    ) -> None:
        """The heartbeat writes every 5s, so by now it has run and timed itself."""
        workers = authenticated_client.get(ENDPOINT).json()["workers"]
        heartbeat = next(t for t in workers["tasks"] if t["name"] == "heartbeat")

        assert heartbeat["runs"] >= 1
        # Writing a key is never a no-op, so every heartbeat cycle did work.
        assert heartbeat["idle_runs"] == 0
        assert heartbeat["failures"] == 0
        assert heartbeat["consecutive_failures"] == 0
        assert heartbeat["last_error"] is None
        assert heartbeat["last_run_at"] is not None
        assert heartbeat["last_duration_ms"] >= 0
        assert heartbeat["avg_duration_ms"] >= 0
        assert heartbeat["max_duration_ms"] >= heartbeat["avg_duration_ms"]

        # Idle cycles are a subset of runs on every worker - the UI subtracts
        # the two to show how many cycles had something to do.
        for task in workers["tasks"]:
            assert 0 <= task["idle_runs"] <= task["runs"]

    def test_workers_report_the_cadence_they_run_on(
        self, authenticated_client: TestClient, test_settings: Any
    ) -> None:
        """Each loop declares the interval it actually sleeps on.

        Note this is the cadence *in force*, not the configured one. The
        health checker binds ``settings`` at import time and ``conftest``
        patches only some modules, so here it runs on the shipped default
        rather than the fixture's 3600 - which is the kind of gap reporting
        the cadence is meant to expose.
        """
        from api.core import health_checker as health_checker_module

        workers = authenticated_client.get(ENDPOINT).json()["workers"]
        by_name = {task["name"]: task for task in workers["tasks"]}

        # Constructed with the app's Settings, so it tracks the fixture.
        assert by_name["metrics_flusher"]["interval_seconds"] == test_settings.metrics_flush_interval
        # Fixed in the loops themselves.
        assert by_name["heartbeat"]["interval_seconds"] == 5
        assert by_name["full_reload"]["interval_seconds"] == 60
        assert by_name["metric_delta_publisher"]["interval_seconds"] == 5
        # Whatever its own module bound, which is the value it sleeps on.
        assert (
            by_name["health_checker"]["interval_seconds"]
            == health_checker_module.settings.health_check_interval
        )
        # Event-driven: a peer message arrives or it does not, so there is no
        # cadence to fall behind.
        assert by_name["metric_delta_subscriber"]["interval_seconds"] is None
        assert by_name["metric_delta_subscriber"]["overruns"] == 0
        # Nothing in a healthy test run takes longer than its cadence.
        assert by_name["heartbeat"]["overruns"] == 0

    def test_workers_report_this_instance_and_its_leases(
        self, authenticated_client: TestClient, test_settings: Any
    ) -> None:
        workers = authenticated_client.get(ENDPOINT).json()["workers"]

        instances = {i["instance_id"]: i for i in workers["instances"]}
        assert test_settings.instance_id in instances
        assert instances[test_settings.instance_id]["is_self"] is True
        assert instances[test_settings.instance_id]["ttl_seconds"] > 0

        # The flusher and compactor take their global lease on startup. Redis
        # is shared across tests in this module, so a lease left by an earlier
        # app instance may still be listed - assert on attribution, which holds
        # either way, rather than on who won the race.
        leases = {lease["name"]: lease for lease in workers["leases"]}
        assert {"metrics_flusher", "metrics_compactor"} <= set(leases)
        assert leases["metrics_flusher"]["kind"] == "Metrics flush to Postgres"
        assert leases["metrics_flusher"]["worker"] == "metrics_flusher"
        assert leases["metrics_flusher"]["target"] is None
        assert leases["metrics_flusher"]["ttl_ms"] > 0
        for lease in workers["leases"]:
            assert lease["held_by_self"] == (lease["holder"] == test_settings.instance_id)

        assert workers["proxy_server_listening"] is True


class TestSystemHistoryAccessControl:
    def test_unauthenticated_is_rejected(self, async_client: TestClient) -> None:
        assert async_client.get(HISTORY).status_code == 401

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(HISTORY).status_code == 403

    def test_editor_is_forbidden(self, editor_client: TestClient) -> None:
        assert editor_client.get(HISTORY).status_code == 403


class TestSystemHistory:
    def test_empty_before_the_first_snapshot(self, authenticated_client: TestClient) -> None:
        """A fresh install must render as "collecting", not as an error."""
        resp = authenticated_client.get(HISTORY)

        assert resp.status_code == 200
        data = resp.json()
        assert data["snapshots"] == []
        assert data["table_growth"] == []
        assert data["range"] == "24h"
        assert data["bucket_seconds"] is None

    def test_rejects_an_unknown_range(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(HISTORY, params={"range": "5y"}).status_code == 422

    def test_short_ranges_are_raw_and_long_ranges_bucketed(
        self, authenticated_client: TestClient
    ) -> None:
        assert authenticated_client.get(HISTORY, params={"range": "1h"}).json()["bucket_seconds"] is None
        assert authenticated_client.get(HISTORY, params={"range": "7d"}).json()["bucket_seconds"] == 3600
        assert authenticated_client.get(HISTORY, params={"range": "90d"}).json()["bucket_seconds"] == 86400

    def test_returns_stored_snapshots_oldest_first(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        now = utc_now()
        _seed_snapshots(
            test_settings,
            [
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=minutes),
                    database_size_bytes=size,
                    proxies_total=minutes,
                    table_sizes={"proxies": size},
                    proxy_status_counts={},
                )
                for minutes, size in ((30, 100), (10, 300), (20, 200))
            ],
        )

        data = authenticated_client.get(HISTORY, params={"range": "1h"}).json()

        assert [s["database_size_bytes"] for s in data["snapshots"]] == [100, 200, 300]
        assert data["interval_seconds"] >= 0

    def test_table_growth_is_the_delta_across_the_window(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        now = utc_now()
        _seed_snapshots(
            test_settings,
            [
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=40),
                    table_sizes={"proxies": 100, "users": 50},
                    proxy_status_counts={},
                ),
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=1),
                    table_sizes={"proxies": 400, "users": 50},
                    proxy_status_counts={},
                ),
            ],
        )

        rows = authenticated_client.get(HISTORY, params={"range": "1h"}).json()["table_growth"]
        growth = {t["name"]: t for t in rows}

        assert growth["proxies"]["delta_bytes"] == 300
        assert growth["proxies"]["first_bytes"] == 100
        assert growth["proxies"]["last_bytes"] == 400
        # A table that did not move still appears, with a zero delta.
        assert growth["users"]["delta_bytes"] == 0
        # Biggest mover first.
        assert rows[0]["name"] == "proxies"


def _seed_snapshots(settings: Settings, rows: list[SystemMetricsModel]) -> None:
    """Insert snapshot rows over a synchronous connection.

    These tests drive the app through ``TestClient``, which runs it on its own
    event loop; writing with the async session factory here would bind the
    connection to a different loop and blow up mid-request.
    """
    engine = create_engine(settings.database_url_sync)
    try:
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
    finally:
        engine.dispose()
