# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the project metrics endpoints on an api process, with what gateways and workers would leave behind.

Statuses, quarantines, metrics windows, flushed history and traffic decisions are written where those roles write
them, and the pool, the counters, the traffic split and the Prometheus export are read back over HTTP.
"""

import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text
from starlette.testclient import TestClient

from api.core import utc_now
from api.core.demand_tracker import DEMAND_REQUESTS_FIELD, demand_minute
from api.core.roles import Role
from api.core.stats import MetricDelta, StepMetricDelta
from api.core.timing import Step
from api.db.models import HostMetricsModel
from api.db.redis import (
    DEMAND_KEY,
    PROJECT_METRICS_KEY,
    PROJECT_WINDOWS,
    QUARANTINE_KEY,
    RedisClient,
)
from api.db.repository import MetricsRepository
from api.db.session import SessionFactory
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.strategies import STRATEGY_NAMES
from tests.conftest import api_runtime, on_app_loop
from tests.routes.test_connector_traffic import decide_traffic, flush_connector_window

# Cloud instances: a connector the scaling metrics count.
AWS_CREDENTIAL = {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
AWS_CONNECTOR = {
    "instance_name": "octo",
    "region": "us-east-1",
    "instance_type": "t3.micro",
    "key_pair_name": "octo",
    "security_group": "octo",
    "min_proxies": 1,
    "max_proxies": 4,
}


class TestMetricsEndpoints:
    """Tests for metrics endpoints."""

    def test_get_metrics(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting metrics for a project."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics")

        assert response.status_code == 200
        data = response.json()

        # Check pool metrics
        assert "pool" in data
        pool = data["pool"]
        assert "total_proxies" in pool
        assert "healthy_proxies" in pool
        assert "unhealthy_proxies" in pool
        assert "total_requests" in pool
        assert "total_successes" in pool
        assert "total_failures" in pool
        assert "overall_success_rate" in pool
        assert "avg_latency_ms" in pool
        assert "total_bytes_sent" in pool
        assert "total_bytes_received" in pool

        # Check strategy metrics
        assert "strategy" in data
        strategy = data["strategy"]
        assert strategy["current_strategy"] == "round_robin"
        assert isinstance(strategy["available_strategies"], list)
        # Every strategy a router has, none it lacks, simplest first as the Overview lists them.
        assert strategy["available_strategies"] == list(STRATEGY_NAMES)
        assert STRATEGY_NAMES == ("round_robin", "least_used", "random", "sticky", "health_based")

    def test_get_metrics_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting metrics for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/metrics")

        assert response.status_code == 404

    def test_get_metrics_with_proxies(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test getting metrics when proxies exist."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics")

        assert response.status_code == 200
        pool = response.json()["pool"]
        # Not checked yet: in the pool, not healthy.
        assert (pool["total_proxies"], pool["healthy_proxies"], pool["unhealthy_proxies"]) == (1, 0, 1)

    def test_the_pool_counts_statuses_and_quarantines_of_enabled_connectors(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        connector_id = created_connector["id"]
        _add_proxy(authenticated_client, connector_id, ProxyStatus.HEALTHY)
        quarantined = _add_proxy(authenticated_client, connector_id, ProxyStatus.HEALTHY)
        for status in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING, ProxyStatus.UNKNOWN):
            _add_proxy(authenticated_client, connector_id, status)
        _quarantine(authenticated_client, quarantined.id)
        # A disabled connector's proxies are not in the pool at all.
        disabled = _connector(authenticated_client, created_project, created_credential, "Disabled", enabled=False)
        _add_proxy(authenticated_client, disabled["id"], ProxyStatus.HEALTHY)

        pool = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics").json()["pool"]

        assert (pool["total_proxies"], pool["healthy_proxies"], pool["quarantined_proxies"]) == (5, 1, 1)
        assert (pool["draining_proxies"], pool["terminating_proxies"]) == (1, 1)
        # Neither routable nor on their way out: the unchecked one and the quarantined one.
        assert pool["unhealthy_proxies"] == 2

    def test_a_connector_over_its_traffic_limit_leaves_the_pool(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        capped = _connector(
            authenticated_client, created_project, created_credential, "Capped",
            traffic_config={"limit_bytes": 100, "action": "block"},
        )
        _add_proxy(authenticated_client, capped["id"], ProxyStatus.HEALTHY)
        url = f"/api/v1/projects/{created_project['id']}/metrics"
        assert authenticated_client.get(url).json()["pool"]["healthy_proxies"] == 1

        flush_connector_window(authenticated_client, capped["id"], 60, 60)
        decide_traffic(authenticated_client, capped["id"])

        pool = authenticated_client.get(url).json()["pool"]
        assert (pool["total_proxies"], pool["healthy_proxies"]) == (0, 0)

    def test_counters_are_the_totals_plus_the_window_not_flushed_yet(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        runtime = api_runtime(authenticated_client)
        flushed = MetricDelta(
            request_count=10, success_count=8, failure_count=2, latency_sum_ms=1000, bytes_sent=100, bytes_received=900
        )
        window = MetricDelta(request_count=2, success_count=2, latency_sum_ms=100, bytes_sent=1, bytes_received=9)
        on_app_loop(authenticated_client, _flush_project_totals, runtime.session_factory, project_id, flushed)
        on_app_loop(authenticated_client, _write_project_window, runtime.redis_client, project_id, window)

        pool = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics").json()["pool"]

        assert (pool["total_requests"], pool["total_successes"], pool["total_failures"]) == (12, 10, 2)
        # The latency over the ten successes.
        assert (pool["overall_success_rate"], pool["avg_latency_ms"]) == (83.33, 110.0)
        assert (pool["total_bytes_sent"], pool["total_bytes_received"]) == (101, 909)

    def test_the_strategy_in_force_is_the_projects(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        response = authenticated_client.patch(f"/api/v1/projects/{project_id}", json={"routing_strategy": "least_used"})
        assert response.status_code == 200, response.text

        strategy = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics").json()["strategy"]

        assert strategy["current_strategy"] == "least_used"

    def test_a_stored_strategy_no_router_has_is_reported_as_the_installs_default(
        self, build_app: Callable[..., FastAPI], auth_headers: dict[str, str], sample_project_data: dict[str, Any]
    ) -> None:
        """One saved before names were checked, or since removed, is routed with ``default_strategy``, as the compiler serves it."""
        with TestClient(build_app(Role.API, default_strategy="health_based"), headers=auth_headers) as client:
            project_id = client.post("/api/v1/projects", json=sample_project_data).json()["id"]
            on_app_loop(client, _store_strategy, api_runtime(client).session_factory, project_id, "fastest")

            metrics = client.get(f"/api/v1/projects/{project_id}/metrics").json()
            split = client.get(f"/api/v1/projects/{project_id}/metrics/traffic-split").json()

        assert metrics["strategy"]["current_strategy"] == "health_based"
        # The traffic split's preview, which the api's compiler builds, routes it the same.
        assert split["strategy"] == "health_based"

    def test_prometheus_metrics(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """The export is Prometheus text exposition, not a JSON string."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/prometheus")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        samples = _samples(response.text)
        project = {"project": project_id}
        for name in (
            "octoprox_proxies_total", "octoprox_proxies_healthy", "octoprox_requests_total",
            "octoprox_bytes_sent_total", "octoprox_bytes_received_total",
        ):
            assert samples[name, _key(project)] == 0

    def test_prometheus_metrics_count_the_pool_counters_and_connectors(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        runtime = api_runtime(authenticated_client)
        flushed = MetricDelta(request_count=4, success_count=3, failure_count=1, bytes_sent=10, bytes_received=20)
        on_app_loop(authenticated_client, _flush_project_totals, runtime.session_factory, project_id, flushed)
        flush_connector_window(authenticated_client, created_connector["id"], 10, 20)

        samples = _samples(authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/prometheus").text)

        project = _key({"project": project_id})
        assert (samples["octoprox_proxies_total", project], samples["octoprox_proxies_healthy", project]) == (1, 1)
        assert samples["octoprox_requests_total", project] == 4
        assert samples["octoprox_requests_failure_total", project] == 1
        connector = _key({"project": project_id, "connector": created_connector["id"], "name": created_connector["name"]})
        assert samples["octoprox_connector_traffic_bytes", connector] == 30
        assert samples["octoprox_connector_traffic_blocked", connector] == 0

    def test_prometheus_metrics_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting Prometheus metrics for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/metrics/prometheus")

        assert response.status_code == 404


class TestScaling:
    """GET /metrics/scaling: the enabled cloud connectors' instances against the demand gateways counted."""

    def test_instances_and_demand(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        cloud = _cloud_connector(authenticated_client, created_project)
        for status in (ProxyStatus.HEALTHY, ProxyStatus.HEALTHY, ProxyStatus.DRAINING):
            _add_proxy(authenticated_client, cloud["id"], status)
        # 300 requests counted this minute: 60 a minute over the five-minute window.
        redis = api_runtime(authenticated_client).redis_client
        key = DEMAND_KEY.format(project_id=project_id, minute=demand_minute(time.time()))
        on_app_loop(authenticated_client, redis.client.hset, key, DEMAND_REQUESTS_FIELD, 300)

        data = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/scaling").json()

        assert (data["current_instances"], data["healthy_instances"], data["draining_instances"]) == (3, 2, 1)
        assert (data["min_instances"], data["max_instances"], data["terminating_instances"]) == (1, 4, 0)
        assert (data["requests_per_minute"], data["rate_per_proxy"]) == (60.0, 30.0)
        assert data["demand_level"] == "MEDIUM"

    def test_no_demand_and_no_cloud(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        data = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/scaling").json()

        assert data == {
            "demand_level": "LOW", "requests_per_minute": 0.0, "rate_per_proxy": 0.0, "current_instances": 0,
            "healthy_instances": 0, "min_instances": 0, "max_instances": 0, "draining_instances": 0,
            "terminating_instances": 0,
        }

    def test_project_not_found(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/metrics/scaling").status_code == 404


class TestHistory:
    def test_the_flushed_project_windows(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        window = StepMetricDelta(bytes_sent=5)
        for steps in ({Step.ROUTE: 1.0, Step.HANDSHAKE: 39.0}, {Step.ROUTE: 1.0, Step.PREFLIGHT: 60.0, Step.HANDSHAKE: 49.0}):
            window.add_request(True, sum(steps.values()), steps=steps)
        window.add_request(False, 30000.0, steps={Step.ROUTE: 1.0, Step.HANDSHAKE: 29999.0})
        on_app_loop(authenticated_client, _save_project_history, api_runtime(authenticated_client).session_factory,
                    project_id, window)

        snapshots = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/history", params={"range": "1h"}
        ).json()["snapshots"]

        # The latency and its steps over the two successes.
        assert [(s["request_count"], s["failure_count"], s["avg_latency_ms"], s["bytes_sent"]) for s in snapshots] == [
            (3, 1, 75.0, 5),
        ]
        assert snapshots[0]["step_latency_ms"] == {
            "route": 1.0, "preflight": 30.0, "connect": 0.0, "handshake": 44.0, "response": 0.0,
        }

    def test_a_window_where_nothing_succeeded_has_no_latency(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        window = StepMetricDelta()
        window.add_request(False, 30000.0, steps={Step.ROUTE: 1.0, Step.HANDSHAKE: 29999.0})
        on_app_loop(authenticated_client, _save_project_history, api_runtime(authenticated_client).session_factory,
                    project_id, window)

        [snapshot] = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/history", params={"range": "1h"}
        ).json()["snapshots"]

        assert (snapshot["failure_count"], snapshot["avg_latency_ms"], snapshot["step_latency_ms"]) == (1, None, None)
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/history", params={"range": "7d"}
        ).status_code == 200

    def test_errors(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/metrics/history").status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/metrics/history", params={"range": "2h"}
        ).status_code == 422


class TestTrafficSplit:
    """GET /metrics/traffic-split."""

    def test_project_not_found(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get("/api/v1/projects/nope/metrics/traffic-split")
        assert response.status_code == 404

    def test_empty_project(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split")
        assert response.status_code == 200
        data = response.json()
        assert data["connectors"] == []
        assert data["total_weight"] == 0
        assert data["observed_requests"] == 0
        assert data["range"] == "1h"
        assert data["strategy"] == "round_robin"

    def test_connector_without_proxies_is_excluded(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split", params={"range": "24h"})
        assert response.status_code == 200
        data = response.json()
        assert data["range"] == "24h"
        assert len(data["connectors"]) == 1
        share = data["connectors"][0]
        assert share["connector_id"] == created_connector["id"]
        assert share["weight"] == 1
        assert share["eligible_proxies"] == 0
        assert share["expected_share"] == 0
        assert share["excluded_reason"] == "no_eligible_proxies"
        assert share["observed_share"] is None

    def test_rejects_unknown_range(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split", params={"range": "2h"})
        assert response.status_code == 422

    def test_expected_shares_follow_the_weights_of_connectors_that_can_serve(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        client, project = authenticated_client, created_project
        heavy = _connector(client, project, created_credential, "A heavy", routing_config={"weight": 3})
        light = _connector(client, project, created_credential, "B light")
        disabled = _connector(client, project, created_credential, "C disabled", routing_config={"weight": 5}, enabled=False)
        unhealthy = _connector(client, project, created_credential, "D unhealthy")
        resting = _connector(client, project, created_credential, "E resting")
        for _ in range(2):
            _add_proxy(client, heavy["id"], ProxyStatus.HEALTHY)
        _add_proxy(client, light["id"], ProxyStatus.HEALTHY)
        _add_proxy(client, light["id"], ProxyStatus.DRAINING)
        _add_proxy(client, disabled["id"], ProxyStatus.HEALTHY)
        _add_proxy(client, unhealthy["id"], ProxyStatus.UNHEALTHY)
        _quarantine(client, _add_proxy(client, resting["id"], ProxyStatus.HEALTHY).id)

        data = client.get(f"/api/v1/projects/{project['id']}/metrics/traffic-split").json()

        assert (data["strategy"], data["total_weight"]) == ("round_robin", 4)
        shares = {s["name"]: s for s in data["connectors"]}
        assert list(shares) == ["A heavy", "B light", "C disabled", "D unhealthy", "E resting"]
        assert [
            (s["expected_share"], s["eligible_proxies"], s["total_proxies"], s["excluded_reason"]) for s in shares.values()
        ] == [
            (75.0, 2, 2, None),
            # The draining row takes no new requests: not counted.
            (25.0, 1, 1, None),
            (0.0, 0, 1, "disabled"),
            (0.0, 0, 1, "no_eligible_proxies"),
            # Quarantined by a rate limit: nothing to route to until it ends.
            (0.0, 0, 1, "no_eligible_proxies"),
        ]
        assert shares["A heavy"]["weight"] == 3 and shares["A heavy"]["dynamic"] is False

    def test_observed_shares_come_from_the_connector_history(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        priced = _connector(
            authenticated_client, created_project, created_credential, "Priced", traffic_config={"price_per_gb": 2.0}
        )
        plain = _connector(authenticated_client, created_project, created_credential, "Plain")
        flush_connector_window(authenticated_client, priced["id"], 60, 40)
        flush_connector_window(authenticated_client, plain["id"], 30, 20)

        data = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split").json()

        assert (data["observed_requests"], data["observed_bytes"]) == (2, 150)
        shares = {s["name"]: s for s in data["connectors"]}
        assert (shares["Priced"]["observed_share"], shares["Priced"]["observed_bytes"]) == (50.0, 100)
        assert (shares["Priced"]["cost"], shares["Priced"]["currency"]) == (0.0, "USD")
        assert (shares["Plain"]["observed_bytes"], shares["Plain"]["cost"], shares["Plain"]["currency"]) == (50, None, None)

    def test_the_strategy_is_the_projects(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}"
        assert authenticated_client.patch(url, json={"routing_strategy": "sticky"}).status_code == 200

        assert authenticated_client.get(f"{url}/metrics/traffic-split").json()["strategy"] == "sticky"


class TestBlockedConnector:
    """A connector over its traffic limit, as the traffic split and the Prometheus export show it."""

    def test_traffic_split_and_prometheus_show_the_block(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        connector = _connector(
            authenticated_client, created_project, created_credential, "Capped",
            traffic_config={"limit_bytes": 100, "action": "block", "price_per_gb": 1.0},
        )
        _add_proxy(authenticated_client, connector["id"], ProxyStatus.HEALTHY)
        # The flush brings the period's bytes past the limit, and a worker's traffic decision blocks the connector.
        flush_connector_window(authenticated_client, connector["id"], 60, 60)
        decide_traffic(authenticated_client, connector["id"])

        split = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/traffic-split").json()
        share = split["connectors"][0]
        assert (share["excluded_reason"], share["eligible_proxies"], share["expected_share"]) == ("traffic_limit", 0, 0)
        assert share["observed_bytes"] == 120
        assert share["cost"] == 0
        assert share["currency"] == "USD"

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/prometheus")
        text = response.text
        labels = f'{{project="{project_id}",connector="{connector["id"]}",name="Capped"}}'
        assert f"octoprox_connector_traffic_bytes{labels} 120" in text
        assert f"octoprox_connector_traffic_limit_bytes{labels} 100" in text
        assert 'currency="USD"} 0' in text
        assert f"octoprox_connector_traffic_blocked{labels} 1" in text


class TestHostMetricsEndpoints:
    """The Hosts page: where a project's traffic went, by destination and connector."""

    @staticmethod
    def _seed(client: TestClient, project_id: str, connector_id: str) -> None:
        """Write a few host history rows through the app's own session, as a worker's metrics flush would."""
        on_app_loop(client, _save_host_history, api_runtime(client).session_factory, project_id, connector_id)

    def test_empty_project(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/hosts")
        assert response.status_code == 200
        data = response.json()
        assert data["hosts"] == [] and data["connectors"] == []
        assert data["totals"]["host_count"] == 0 and data["totals"]["request_count"] == 0
        assert data["enabled"] is True
        assert data["range"] == "24h"
        assert data["overflow_host"] == "(other)"
        history = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/hosts/history").json()
        assert history["series"] == [] and history["bucket_seconds"] == 900

    def test_enabled_is_this_installs_setting(
        self, authenticated_client: TestClient, created_project: dict[str, Any], test_settings: Any,
    ) -> None:
        test_settings.host_metrics_enabled = False
        data = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/hosts").json()
        assert data["enabled"] is False

    def test_hosts_with_totals_shares_and_connector_split(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id, connector_id = created_project["id"], created_connector["id"]
        self._seed(authenticated_client, project_id, connector_id)

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "1h"})
        assert response.status_code == 200
        data = response.json()
        assert [h["host"] for h in data["hosts"]] == ["shop.example.com", "news.example.org"]
        shop = data["hosts"][0]
        assert shop["request_count"] == 6 and shop["failure_count"] == 1
        assert shop["avg_latency_ms"] == 100.0
        assert shop["request_share"] == 75.0
        assert shop["bytes_share"] == round(1000 / 1100 * 100, 2)
        assert shop["connectors"] == [{
            "connector_id": connector_id, "name": created_connector["name"],
            "credential_type": "static_proxy_provider", "request_count": 6, "success_count": 5,
            "failure_count": 1, "avg_latency_ms": 100.0, "bytes_sent": 100, "bytes_received": 900,
        }]
        assert data["totals"] == {
            "host_count": 2, "request_count": 8, "success_count": 7, "failure_count": 1,
            "avg_latency_ms": 85.71, "bytes_sent": 150, "bytes_received": 950,
        }
        assert [c["connector_id"] for c in data["connectors"]] == [connector_id]

        # The older row only shows up in the widest range; search and limit narrow the list.
        wide = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d"}).json()
        assert wide["totals"]["host_count"] == 3 and wide["hosts"][0]["host"] == "old.example.net"
        narrowed = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d", "search": "EXAMPLE.ORG"},
        ).json()
        assert [h["host"] for h in narrowed["hosts"]] == ["news.example.org"]
        assert narrowed["totals"]["host_count"] == 1
        limited = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d", "limit": 1},
        ).json()
        assert len(limited["hosts"]) == 1 and limited["totals"]["host_count"] == 3 and limited["limit"] == 1
        by_connector = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"connector_id": connector_id},
        ).json()
        assert by_connector["totals"]["request_count"] == 8

    def test_history_series_and_overflow(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id, connector_id = created_project["id"], created_connector["id"]
        self._seed(authenticated_client, project_id, connector_id)

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history", params={"range": "1h", "top": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["bucket_seconds"] == 60
        assert [s["host"] for s in data["series"]] == ["shop.example.com", "(other)"]
        shop, other = data["series"]
        assert len(shop["points"]) == len(other["points"]) == 1
        assert shop["points"][0]["request_count"] == 6
        assert other["points"][0]["request_count"] == 2
        assert other["points"][0]["bytes_received"] == 50

        # Named hosts get a series each, even a quiet one, so a chart's legend is stable.
        named = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history",
            params={"range": "1h", "hosts": "news.example.org, quiet.example.com"},
        ).json()
        assert [s["host"] for s in named["series"]] == ["news.example.org", "quiet.example.com", "(other)"]
        assert named["series"][1]["points"][0]["request_count"] == 0
        assert named["series"][2]["points"][0]["request_count"] == 6
        by_connector = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history", params={"range": "1h", "connector_id": connector_id},
        ).json()
        assert [s["host"] for s in by_connector["series"]][:2] == ["shop.example.com", "news.example.org"]

    def test_errors(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
        sample_project_data: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        assert authenticated_client.get("/api/v1/projects/nope/metrics/hosts").status_code == 404
        assert authenticated_client.get("/api/v1/projects/nope/metrics/hosts/history").status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"connector_id": "nope"},
        ).status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history", params={"connector_id": "nope"},
        ).status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "2h"},
        ).status_code == 422
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"limit": 0},
        ).status_code == 422
        # Another project's connector is not this project's.
        other = authenticated_client.post(
            "/api/v1/projects",
            json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
        ).json()
        for path in ("hosts", "hosts/history"):
            assert authenticated_client.get(
                f"/api/v1/projects/{other['id']}/metrics/{path}", params={"connector_id": created_connector["id"]},
            ).status_code == 404

    def test_viewer_can_read(self, viewer_client: TestClient) -> None:
        # Read-only, so a viewer gets past the role check (and then a 404 for a project that is not there).
        assert viewer_client.get("/api/v1/projects/nope/metrics/hosts").status_code == 404
        assert viewer_client.get("/api/v1/projects/nope/metrics/hosts/history").status_code == 404


def _connector(
    client: TestClient,
    project: dict[str, Any],
    credential: dict[str, Any],
    name: str,
    *,
    enabled: bool = True,
    **configs: Any,
) -> dict[str, Any]:
    """A static connector of the project, with the routing or traffic config given."""
    response = client.post(
        f"/api/v1/projects/{project['id']}/connectors",
        json={"name": name, "credential_id": credential["id"], "config": {}, "enabled": enabled, **configs},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def _cloud_connector(client: TestClient, project: dict[str, Any]) -> dict[str, Any]:
    """An AWS connector of the project, holding between one and four instances."""
    response = client.post(
        f"/api/v1/projects/{project['id']}/credentials",
        json={"name": f"aws-{uuid.uuid4().hex[:6]}", "type": "aws", "config": AWS_CREDENTIAL},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/projects/{project['id']}/connectors",
        json={"name": f"aws-{uuid.uuid4().hex[:6]}", "credential_id": response.json()["id"], "config": AWS_CONNECTOR},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def _add_proxy(client: TestClient, connector_id: str, status: ProxyStatus) -> Proxy:
    """A proxy stored with ``status``, as a job adds one or a health check left it."""
    proxy = Proxy(host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
                  connector_id=connector_id, status=status)
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)
    return proxy


def _quarantine(client: TestClient, proxy_id: str) -> None:
    """Quarantine a proxy for ten minutes, as a gateway's rate limiter does when it trips."""
    redis = api_runtime(client).redis_client
    on_app_loop(client, redis.client.zadd, QUARANTINE_KEY, {proxy_id: time.time() + 600})


def _samples(text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    """Every sample of a Prometheus text exposition, by name and labels; the parse fails on anything else."""
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def _key(labels: dict[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(labels.items())


async def _store_strategy(session_factory: SessionFactory, project_id: str, strategy: str) -> None:
    """Store a project's routing strategy as given, past the check a save through the api makes."""
    async with session_factory() as session:
        await session.execute(
            text("UPDATE projects SET routing_strategy = :strategy WHERE id = :id"), {"strategy": strategy, "id": project_id}
        )
        await session.commit()


async def _flush_project_totals(session_factory: SessionFactory, project_id: str, delta: MetricDelta) -> None:
    """A project's all-time totals, as a worker's metrics flush adds them."""
    async with session_factory() as session:
        await MetricsRepository(session).upsert_totals(PROJECT_WINDOWS, {project_id: delta})
        await session.commit()


async def _write_project_window(redis_client: RedisClient, project_id: str, delta: MetricDelta) -> None:
    """The project's metrics window as gateways leave it in Redis, not flushed yet."""
    await redis_client.client.hset(PROJECT_METRICS_KEY.format(project_id=project_id), mapping=delta.to_dict())


async def _save_project_history(session_factory: SessionFactory, project_id: str, delta: MetricDelta) -> None:
    """One flushed window of the project's history, stamped now."""
    async with session_factory() as session:
        await MetricsRepository(session).save_project_metrics_snapshots({project_id: delta})
        await session.commit()


async def _save_host_history(session_factory: SessionFactory, project_id: str, connector_id: str) -> None:
    """Host history rows for two hosts this hour, one row for a connector that is gone, and one ten days old."""
    async with session_factory() as session:
        repo = MetricsRepository(session)
        dropped = await repo.save_host_metrics_snapshots({
            (project_id, connector_id, "shop.example.com"): MetricDelta(
                request_count=6, success_count=5, failure_count=1, latency_sum_ms=500, bytes_sent=100, bytes_received=900,
            ),
            (project_id, connector_id, "news.example.org"): MetricDelta(
                request_count=2, success_count=2, latency_sum_ms=100, bytes_sent=50, bytes_received=50,
            ),
            (project_id, "no-such-connector", "lost.example.net"): MetricDelta(request_count=1, success_count=1),
        })
        assert dropped == [(project_id, "no-such-connector", "lost.example.net")]
        # Outside every range but 30d.
        session.add(HostMetricsModel(
            project_id=project_id, connector_id=connector_id, host="old.example.net",
            timestamp=utc_now() - timedelta(days=10), request_count=40, success_count=40,
            failure_count=0, avg_latency_ms=10.0, bytes_sent=1, bytes_received=1, granularity=3600,
        ))
        await session.commit()
