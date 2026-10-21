# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What ``/metrics`` renders for each role: the families of every section of the process's snapshot.

The collector is driven with snapshots built by hand, one per role, and its
text is read back with ``prometheus_client``'s own parser, so a test fails on
an exposition Prometheus would refuse as well as on a wrong number.
"""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families

from api import __version__
from api.core.roles import JobKind, Role
from api.core.stats import FailureKind
from api.core.telemetry import Telemetry
from api.db.streams import FullRead
from api.models.system import (
    ApiSnapshot,
    ControllerSnapshot,
    GatewaySnapshot,
    InstanceRuntime,
    InstanceSnapshot,
    JobKindStats,
    StreamState,
    WorkerSnapshot,
    WorkerTask,
)
from api.models.tunnel import TunnelEndpointState

# A scrape as (family name, label set without role) -> value, plus the families' types.
Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]

# A position on the changes stream: what a compile read through.
COMPILED_THROUGH = "1792281600000-0"


def scrape(snapshot: InstanceSnapshot) -> tuple[Samples, dict[str, str]]:
    """Render ``snapshot`` and parse it back; every sample must carry the process's role."""
    text = Telemetry(lambda: snapshot).render().decode()
    samples: Samples = {}
    types: dict[str, str] = {}
    for family in text_string_to_metric_families(text):
        types[family.name] = family.type
        for sample in family.samples:
            labels = dict(sample.labels)
            assert labels.pop("role") == snapshot.runtime.role.value, sample
            key = (sample.name, frozenset(labels.items()))
            assert key not in samples, f"{key} twice"
            samples[key] = sample.value
    return samples, types


def value(samples: Samples, name: str, **labels: str) -> float:
    return samples[(name, frozenset(labels.items()))]


def names(samples: Samples) -> set[str]:
    return {name for name, _ in samples}


def snapshot_of(role: Role, **sections: Any) -> InstanceSnapshot:
    return InstanceSnapshot(
        runtime=InstanceRuntime(version=__version__, role=role, instance_id=f"{role}-1", pid=7, uptime_seconds=12.5),
        ready=True,
        **sections,
    )


def gateway_section(**overrides: Any) -> GatewaySnapshot:
    fields: dict[str, Any] = {
        "listener_port": 8080,
        "listening": True,
        "active_connections": 3,
        "feed_digest": "d1",
        "feed_changes": COMPILED_THROUGH,
        "feed_age_seconds": 2.5,
        "retired_slots": 1,
        "local_quarantines": 2,
        "in_flight": 4,
        "requests": 10,
        "successes": 8,
        "failures": 2,
        "bytes_sent": 100,
        "bytes_received": 2000,
        "failures_by_kind": {"upstream_timeout": 2, "a_kind_from_a_newer_release": 1},
        "pending_metric_deltas": 5,
        "metric_flush_failures": 6,
        "rate_limit_errors": 7,
        "preflight_checks": 9,
        "preflight_rejections": 3,
        "geo_databases_loaded": ["geo-1", "geo-2"],
        "geo_database_errors": {"geo-3": "unreachable"},
        "geo_databases_missing": ["geo-3"],
        "observations_pending": 11,
        "observations_dropped": 12,
        "sightings_pending": 13,
        "sightings_dropped": 14,
        "failure_log_dropped": 15,
        "mitm_captures_dropped": 16,
        "tunnels": [],
    }
    return GatewaySnapshot(**{**fields, **overrides})


def controller_section(*, leading: bool, **overrides: Any) -> ControllerSnapshot:
    fields: dict[str, Any] = {
        "is_leader": leading,
        "lease_terms": 2,
        "changes": COMPILED_THROUGH,
        "compiles": 5,
        "compile_ms": 250.0,
        "compile_error": None,
        "publishes": 4,
        "publish_ms": 20.0,
        "publish_error": "redis down",
        "sections": 12,
        "sections_written": 30,
        "sections_removed": 2,
        "quarantined": 3,
        "blocked_connectors": 1,
    }
    return ControllerSnapshot(**{**fields, **overrides})


class TestEveryProcess:
    @pytest.mark.parametrize("role", [Role.API, Role.GATEWAY, Role.CONTROLLER, Role.WORKER, Role.ALL])
    def test_identity_readiness_and_loops_carry_the_role(self, role: Role) -> None:
        snapshot = snapshot_of(
            role, tasks=[WorkerTask(name="heartbeat", state="running", runs=4, failures=1, consecutive_failures=1)]
        )

        samples, types = scrape(snapshot)

        assert value(samples, "octoprox_build_info", version=__version__, instance_id=f"{role}-1") == 1
        assert value(samples, "octoprox_ready") == 1
        assert value(samples, "octoprox_uptime_seconds") == 12.5
        assert value(samples, "octoprox_loop_running", loop="heartbeat") == 1
        assert value(samples, "octoprox_loop_runs_total", loop="heartbeat") == 4
        assert value(samples, "octoprox_loop_failures_total", loop="heartbeat") == 1
        assert value(samples, "octoprox_loop_consecutive_failures", loop="heartbeat") == 1
        assert types["octoprox_loop_runs"] == "counter" and types["octoprox_loop_running"] == "gauge"

    def test_a_loop_is_one_series_however_many_tasks_carry_its_name(self) -> None:
        """Two tasks of one name share their counters; a scrape holding the series twice would be refused."""
        snapshot = snapshot_of(
            Role.GATEWAY,
            tasks=[
                WorkerTask(name="openvpn_supervisor", state="failed", runs=2),
                WorkerTask(name="openvpn_supervisor", state="running", runs=2),
                WorkerTask(name="feed_follower", state="cancelled", runs=9),
            ],
        )

        samples, _ = scrape(snapshot)

        assert value(samples, "octoprox_loop_running", loop="openvpn_supervisor") == 1
        assert value(samples, "octoprox_loop_runs_total", loop="openvpn_supervisor") == 2
        # A loop whose task ended is down, whatever it ran before.
        assert value(samples, "octoprox_loop_running", loop="feed_follower") == 0

    def test_a_process_without_sections_exports_no_role_family(self) -> None:
        samples, _ = scrape(snapshot_of(Role.ECHO))

        sections = ("api", "gateway", "controller", "worker", "stream")
        assert not {name for name in names(samples) if name.split("_")[1] in sections}
        assert "octoprox_loop_runs_total" not in names(samples)


class TestStreams:
    def test_each_stream_the_process_follows(self) -> None:
        streams = {
            "changes": StreamState(
                position=COMPILED_THROUGH, lag_seconds=0.25, entries=12, full_reads={"start": 1, "reconcile": 2}
            ),
            "feed": StreamState(position=COMPILED_THROUGH, lag_seconds=0.0, entries=40, full_reads={"gap": 1}),
            # Not read yet: no lag to report, nothing read.
            "runtime": StreamState(),
        }

        samples, types = scrape(snapshot_of(Role.ALL, streams=streams))

        assert value(samples, "octoprox_stream_lag_seconds", stream="changes") == 0.25
        assert value(samples, "octoprox_stream_lag_seconds", stream="feed") == 0
        assert ("octoprox_stream_lag_seconds", frozenset({("stream", "runtime")})) not in samples
        assert {stream: value(samples, "octoprox_stream_entries_total", stream=stream) for stream in streams} == {
            "changes": 12,
            "feed": 40,
            "runtime": 0,
        }
        assert value(samples, "octoprox_stream_full_reads_total", stream="changes", reason="start") == 1
        assert value(samples, "octoprox_stream_full_reads_total", stream="changes", reason="reconcile") == 2
        assert value(samples, "octoprox_stream_full_reads_total", stream="feed", reason="gap") == 1
        assert types["octoprox_stream_lag_seconds"] == "gauge"
        assert types["octoprox_stream_entries"] == "counter" and types["octoprox_stream_full_reads"] == "counter"

    def test_every_full_read_reason_exists_from_the_first_scrape(self) -> None:
        """An alert on a growing gap count must see the first gap; a series that appears with it loses it to increase()."""
        samples, _ = scrape(snapshot_of(Role.GATEWAY, streams={"feed": StreamState()}))

        reads = {
            dict(labels)["reason"]: count
            for (name, labels), count in samples.items()
            if name == "octoprox_stream_full_reads_total"
        }
        assert reads == {reason.value: 0 for reason in FullRead}
        # Before its first read the process has no lag to report.
        assert "octoprox_stream_lag_seconds" not in names(samples)


class TestApi:
    def test_the_pool(self) -> None:
        api = ApiSnapshot(pool_size=5, pool_checked_out=2, pool_overflow=1)

        samples, _ = scrape(snapshot_of(Role.API, api=api))

        assert value(samples, "octoprox_api_db_pool_size") == 5
        assert value(samples, "octoprox_api_db_pool_checked_out") == 2
        assert value(samples, "octoprox_api_db_pool_overflow") == 1
        assert not {name for name in names(samples) if name.startswith(("octoprox_gateway", "octoprox_worker"))}

    def test_what_the_pool_does_not_say_is_left_out(self) -> None:
        samples, _ = scrape(snapshot_of(Role.API, api=ApiSnapshot()))

        assert not {name for name in names(samples) if name.startswith("octoprox_api_")}


class TestGateway:
    def test_traffic_feed_preflight_exclusions_buffers_and_databases(self) -> None:
        samples, types = scrape(snapshot_of(Role.GATEWAY, gateway=gateway_section()))

        assert value(samples, "octoprox_gateway_requests_total", outcome="success") == 8
        assert value(samples, "octoprox_gateway_requests_total", outcome="failure") == 2
        assert value(samples, "octoprox_gateway_bytes_total", direction="sent") == 100
        assert value(samples, "octoprox_gateway_bytes_total", direction="received") == 2000
        assert value(samples, "octoprox_gateway_in_flight") == 4
        assert value(samples, "octoprox_gateway_connections") == 3
        assert value(samples, "octoprox_gateway_feed_age_seconds") == 2.5
        assert value(samples, "octoprox_gateway_preflight_checks_total") == 9
        assert value(samples, "octoprox_gateway_preflight_rejections_total") == 3
        assert value(samples, "octoprox_gateway_exclusions", kind="retired") == 1
        assert value(samples, "octoprox_gateway_exclusions", kind="quarantined") == 2
        assert {buffer: value(samples, "octoprox_gateway_pending", buffer=buffer) for buffer in (
            "metric_deltas", "observations", "sightings"
        )} == {"metric_deltas": 5, "observations": 11, "sightings": 13}
        assert {buffer: value(samples, "octoprox_gateway_dropped_total", buffer=buffer) for buffer in (
            "observations", "sightings", "failure_log", "mitm_captures"
        )} == {"observations": 12, "sightings": 14, "failure_log": 15, "mitm_captures": 16}
        assert value(samples, "octoprox_gateway_metric_flush_failures_total") == 6
        assert value(samples, "octoprox_gateway_rate_limit_errors_total") == 7
        assert value(samples, "octoprox_gateway_ip_databases", state="loaded") == 2
        assert value(samples, "octoprox_gateway_ip_databases", state="missing") == 1
        assert types["octoprox_gateway_requests"] == "counter"
        assert types["octoprox_gateway_in_flight"] == "gauge"

    def test_every_failure_kind_exists_from_the_first_scrape(self) -> None:
        """A counter that appears with its first increment loses that increment to rate(); unknown kinds are kept."""
        samples, _ = scrape(snapshot_of(Role.GATEWAY, gateway=gateway_section()))

        failures = {
            dict(labels)["kind"]: count
            for (name, labels), count in samples.items()
            if name == "octoprox_gateway_failures_total"
        }
        assert set(failures) == {kind.value for kind in FailureKind} | {"a_kind_from_a_newer_release"}
        assert failures["upstream_timeout"] == 2 and failures["no_proxy_available"] == 0
        assert failures["a_kind_from_a_newer_release"] == 1

    def test_before_the_first_feed_its_age_is_left_out(self) -> None:
        section = gateway_section(feed_digest=None, feed_changes=None, feed_age_seconds=None)

        samples, _ = scrape(snapshot_of(Role.GATEWAY, gateway=section))

        assert "octoprox_gateway_feed_age_seconds" not in names(samples)

    def test_tunnel_endpoints_per_protocol_and_the_shared_listener_once(self) -> None:
        tunnels = [
            TunnelEndpointState(protocol="wireguard", instance_id="gateway-1", state="running", active_connections=6),
            TunnelEndpointState(
                protocol="openvpn", instance_id="gateway-1", state="failed", active_connections=6, restarts=3, denied=2
            ),
        ]

        samples, _ = scrape(snapshot_of(Role.GATEWAY, gateway=gateway_section(tunnels=tunnels)))

        assert value(samples, "octoprox_gateway_tunnel_up", protocol="wireguard") == 1
        assert value(samples, "octoprox_gateway_tunnel_up", protocol="openvpn") == 0
        assert value(samples, "octoprox_gateway_tunnel_restarts_total", protocol="openvpn") == 3
        assert value(samples, "octoprox_gateway_tunnel_denied_total", protocol="openvpn") == 2
        assert value(samples, "octoprox_gateway_tunnel_connections") == 6

    def test_no_tunnel_families_without_a_tunnel(self) -> None:
        samples, _ = scrape(snapshot_of(Role.GATEWAY, gateway=gateway_section()))

        assert not {name for name in names(samples) if name.startswith("octoprox_gateway_tunnel")}


class TestController:
    def test_the_leader_exports_its_own_counters_and_the_install_wide_readings(self) -> None:
        samples, types = scrape(snapshot_of(Role.CONTROLLER, controller=controller_section(leading=True)))

        assert value(samples, "octoprox_controller_leader") == 1
        assert value(samples, "octoprox_controller_lease_terms_total") == 2
        assert value(samples, "octoprox_controller_compiles_total") == 5
        assert value(samples, "octoprox_controller_compile_duration_seconds") == 0.25
        assert value(samples, "octoprox_controller_publishes_total") == 4
        assert value(samples, "octoprox_controller_publish_duration_seconds") == 0.02
        assert value(samples, "octoprox_controller_failing", step="compile") == 0
        assert value(samples, "octoprox_controller_failing", step="publish") == 1
        assert value(samples, "octoprox_controller_sections_written_total") == 30
        assert value(samples, "octoprox_controller_sections_removed_total") == 2
        assert value(samples, "octoprox_controller_feed_sections") == 12
        assert value(samples, "octoprox_controller_quarantined_proxies") == 3
        assert value(samples, "octoprox_controller_blocked_connectors") == 1
        assert types["octoprox_controller_compiles"] == "counter"

    def test_a_standby_exports_only_its_own_counters(self) -> None:
        """What a standby saw while it last led is stale: its old publish error must not alert, nor its readings sum."""
        samples, _ = scrape(snapshot_of(Role.CONTROLLER, controller=controller_section(leading=False)))

        assert value(samples, "octoprox_controller_leader") == 0
        assert value(samples, "octoprox_controller_compiles_total") == 5
        for leader_only in (
            "octoprox_controller_failing",
            "octoprox_controller_feed_sections",
            "octoprox_controller_quarantined_proxies",
            "octoprox_controller_blocked_connectors",
        ):
            assert leader_only not in names(samples), leader_only

    def test_a_leader_before_its_first_compile(self) -> None:
        section = ControllerSnapshot(is_leader=True, lease_terms=1)

        samples, _ = scrape(snapshot_of(Role.CONTROLLER, controller=section))

        assert value(samples, "octoprox_controller_feed_sections") == 0
        for unknown in ("octoprox_controller_compile_duration_seconds", "octoprox_controller_publish_duration_seconds"):
            assert unknown not in names(samples), unknown


class TestWorker:
    def test_jobs_per_kind_and_the_sightings_and_observations_ingested(self) -> None:
        worker = WorkerSnapshot(
            jobs={
                JobKind.HEALTH: JobKindStats(runs=12, failures=1, duration_seconds=3.5, in_flight=4),
                JobKind.SCALE: JobKindStats(),
                JobKind.METRICS_FLUSH: JobKindStats(runs=3, duration_seconds=0.75),
            },
            sightings_consumed=90,
            sightings_dropped=2,
            observations_consumed=5000,
            observations_dropped=7,
        )

        samples, types = scrape(snapshot_of(Role.WORKER, worker=worker))

        assert value(samples, "octoprox_worker_job_runs_total", kind="health") == 12
        assert value(samples, "octoprox_worker_job_failures_total", kind="health") == 1
        assert value(samples, "octoprox_worker_job_duration_seconds_total", kind="health") == 3.5
        assert value(samples, "octoprox_worker_jobs_in_flight", kind="health") == 4
        assert value(samples, "octoprox_worker_job_runs_total", kind="scale") == 0
        assert value(samples, "octoprox_worker_job_duration_seconds_total", kind="metrics_flush") == 0.75
        # Only the kinds this worker takes.
        assert ("octoprox_worker_job_runs_total", frozenset({("kind", "sync")})) not in samples
        assert value(samples, "octoprox_worker_sightings_consumed_total") == 90
        assert value(samples, "octoprox_worker_sightings_dropped_total") == 2
        assert value(samples, "octoprox_worker_observations_consumed_total") == 5000
        assert value(samples, "octoprox_worker_observations_dropped_total") == 7
        assert types["octoprox_worker_job_duration_seconds"] == "counter"
        assert types["octoprox_worker_observations_consumed"] == "counter"
        assert types["octoprox_worker_observations_dropped"] == "counter"

    def test_a_worker_that_ingested_nothing_yet_counts_from_zero(self) -> None:
        """Every count exists from the first scrape, so increase() sees the first sighting or observation lost."""
        samples, _ = scrape(snapshot_of(Role.WORKER, worker=WorkerSnapshot(jobs={})))

        for counter in (
            "octoprox_worker_sightings_consumed_total",
            "octoprox_worker_sightings_dropped_total",
            "octoprox_worker_observations_consumed_total",
            "octoprox_worker_observations_dropped_total",
        ):
            assert value(samples, counter) == 0, counter


def test_a_process_of_role_all_exports_every_section_under_its_role() -> None:
    snapshot = snapshot_of(
        Role.ALL,
        tasks=[WorkerTask(name="feed_builder", state="running")],
        streams={"changes": StreamState(position=COMPILED_THROUGH, lag_seconds=0.0, entries=1)},
        api=ApiSnapshot(pool_size=5),
        gateway=gateway_section(),
        controller=controller_section(leading=True),
        worker=WorkerSnapshot(jobs={JobKind.HEALTH: JobKindStats(runs=1)}),
    )

    samples, _ = scrape(snapshot)  # every sample labelled role="all"

    for name in (
        "octoprox_api_db_pool_size",
        "octoprox_gateway_requests_total",
        "octoprox_controller_leader",
        "octoprox_worker_job_runs_total",
        "octoprox_worker_observations_consumed_total",
        "octoprox_loop_runs_total",
        "octoprox_stream_entries_total",
    ):
        assert name in names(samples), name


def test_the_families_render_a_section_at_a_time_in_order() -> None:
    """The process, its event loop's lag, its loops and its streams, then each role's section in turn."""
    snapshot = InstanceSnapshot(
        runtime=InstanceRuntime(
            version=__version__, role=Role.ALL, instance_id="all-1", pid=7, loop_lag_max_ms=40.0, loop_lag_mean_ms=4.0
        ),
        tasks=[WorkerTask(name="feed_builder", state="running")],
        streams={"changes": StreamState(position=COMPILED_THROUGH, lag_seconds=0.0, entries=1)},
        api=ApiSnapshot(pool_size=5),
        gateway=gateway_section(),
        controller=controller_section(leading=True),
        worker=WorkerSnapshot(jobs={JobKind.HEALTH: JobKindStats(runs=1)}),
    )
    sections = ["process", "event_loop_lag", "loop", "stream", "api", "gateway", "controller", "worker"]

    def section(family: str) -> str:
        name = family.removeprefix("octoprox_")
        if name in ("build_info", "uptime_seconds", "ready"):
            return "process"
        return "event_loop_lag" if name.startswith("event_loop_lag") else name.split("_")[0]

    text = Telemetry(lambda: snapshot).render().decode()
    order = [sections.index(section(family.name)) for family in text_string_to_metric_families(text)]

    assert order == sorted(order) and set(order) == set(range(len(sections)))
