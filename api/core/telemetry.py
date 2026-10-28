# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Prometheus exposition of one process: each scrape turns the process's heartbeat snapshot into metric families.

A gateway's latency histograms are read from its metering instead, so the heartbeat never carries them. Every sample is
labelled with the process's ``role``, and a scrape reads memory only, never Redis or Postgres.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from prometheus_client import CONTENT_TYPE_PLAIN_0_0_4, generate_latest
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector, CollectorRegistry

from api.core.stats import FailureKind
from api.db.streams import FullRead
from api.gateway.latency import LATENCY_BUCKETS
from api.models.system import ApiSnapshot, ControllerSnapshot, GatewaySnapshot, WorkerSnapshot

if TYPE_CHECKING:
    from api.gateway.latency import LatencyHistogram
    from api.models.system import InstanceSnapshot
    from api.models.tunnel import TunnelEndpointState

# The prefix every metric name starts with.
METRIC_PREFIX = "octoprox"
# What a ``/metrics`` response declares itself as: the classic text format
# (0.0.4) that ``generate_latest`` writes by default, metric names escaped to
# underscores, which is how prometheus_client's own handlers label it for a
# scraper that does not ask for more. Not CONTENT_TYPE_LATEST: since 0.23 that
# names format 1.0.0, whose escaping is negotiated, and nothing here negotiates.
CONTENT_TYPE = CONTENT_TYPE_PLAIN_0_0_4
# Milliseconds in a second: the snapshot measures durations in ms, Prometheus
# names durations in seconds.
_MS_PER_SECOND = 1000.0
# The ``le`` label of each latency bucket, the last one above every bound.
_LATENCY_BOUNDS = (*(repr(bound) for bound in LATENCY_BUCKETS), "+Inf")


class SectionFamilies(ABC):
    """The metric families one part of a process's snapshot renders on each scrape."""

    @abstractmethod
    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        """This part's families in ``snapshot``, every sample labelled with the process's role."""


class RuntimeFamilies(SectionFamilies):
    """Who the process is, how long it has run, and whether it is ready."""

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        runtime = snapshot.runtime
        role = runtime.role.value
        info = GaugeMetricFamily(
            f"{METRIC_PREFIX}_build_info",
            "The running process: its version, role and instance id; always 1",
            labels=["role", "version", "instance_id"],
        )
        info.add_metric([role, runtime.version, runtime.instance_id], 1)
        yield info
        uptime = GaugeMetricFamily(
            f"{METRIC_PREFIX}_uptime_seconds", "Seconds since the process's node started", labels=["role"]
        )
        uptime.add_metric([role], runtime.uptime_seconds)
        yield uptime
        ready = GaugeMetricFamily(
            f"{METRIC_PREFIX}_ready", "1 while the process answers /ready with 200, else 0", labels=["role"]
        )
        ready.add_metric([role], 1 if snapshot.ready else 0)
        yield ready


class LoopLagFamilies(SectionFamilies):
    """How late the event loop ran timers over the last full minute; nothing before the first minute is up."""

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        runtime = snapshot.runtime
        role = runtime.role.value
        for name, lag_ms, documentation in (
            ("max", runtime.loop_lag_max_ms, "Worst lateness of an event loop timer over the last full minute"),
            ("mean", runtime.loop_lag_mean_ms, "Average lateness of an event loop timer over the last full minute"),
        ):
            if lag_ms is None:
                continue
            family = GaugeMetricFamily(
                f"{METRIC_PREFIX}_event_loop_lag_{name}_seconds", documentation, labels=["role"]
            )
            family.add_metric([role], lag_ms / _MS_PER_SECOND)
            yield family


class LoopFamilies(SectionFamilies):
    """Every loop of the process: whether its task still runs, its cycles, and its failures."""

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        role = snapshot.runtime.role.value
        # One series per loop name: the counters are kept by name, so two tasks
        # of one name would only repeat them, and a scrape must not hold a
        # series twice.
        running: dict[str, bool] = {}
        counters: dict[str, tuple[int, int, int]] = {}
        for task in snapshot.tasks:
            running[task.name] = running.get(task.name, False) or task.state == "running"
            counters.setdefault(task.name, (task.runs, task.failures, task.consecutive_failures))
        if not counters:
            return
        up = _gauge("loop_running", "1 while the loop's task runs, 0 once it ended (done, cancelled or failed)", "loop")
        runs = _counter("loop_runs_total", "Cycles the loop ran since the process started, idle ones included", "loop")
        failures = _counter("loop_failures_total", "Cycles of the loop that failed since the process started", "loop")
        streak = _gauge(
            "loop_consecutive_failures",
            "Cycles of the loop that failed in a row up to now; 0 once one succeeds",
            "loop",
        )
        for name, (ran, failed, in_a_row) in sorted(counters.items()):
            up.add_metric([role, name], 1 if running[name] else 0)
            runs.add_metric([role, name], ran)
            failures.add_metric([role, name], failed)
            streak.add_metric([role, name], in_a_row)
        yield from (up, runs, failures, streak)


class StreamFamilies(SectionFamilies):
    """Each Redis stream the process follows: its lag at the last read, entries read, and full reads by reason."""

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        if not snapshot.streams:
            return
        role = snapshot.runtime.role.value
        lag = _gauge(
            "stream_lag_seconds",
            "How far behind the stream's newest entry the process's last read found it, by the stream's entry IDs; "
            "0 when that read found nothing new or read in full",
            "stream",
        )
        entries = _counter("stream_entries_total", "Entries the process read from the stream", "stream")
        full_reads = _counter(
            "stream_full_reads_total",
            "Full reads of what the stream announces instead of following its entries, by reason: start, gap "
            "(entries lost to a trim, a wipe or a recreated stream), reconnect (a read failed), reconcile (the "
            "periodic read)",
            "stream",
            "reason",
        )
        for name, state in sorted(snapshot.streams.items()):
            if state.lag_seconds is not None:
                lag.add_metric([role, name], state.lag_seconds)
            entries.add_metric([role, name], state.entries)
            for reason, count in _counted_from_zero(FullRead, state.full_reads).items():
                full_reads.add_metric([role, name, reason], count)
        if lag.samples:
            yield lag
        yield from (entries, full_reads)


class RoleFamilies[S](SectionFamilies):
    """The families of one role's section of the snapshot; none from a process that does not run the role."""

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        section = self._section(snapshot)
        if section is not None:
            yield from self._families(snapshot.runtime.role.value, section)

    @abstractmethod
    def _section(self, snapshot: InstanceSnapshot) -> S | None:
        """The role's section of ``snapshot``; None when the process does not run the role."""

    @abstractmethod
    def _families(self, role: str, section: S) -> Iterator[Metric]:
        """The section's families, every sample labelled ``role``."""


class ApiFamilies(RoleFamilies[ApiSnapshot]):
    """An api runtime's Postgres pool, leaving out what the pool does not say."""

    def _section(self, snapshot: InstanceSnapshot) -> ApiSnapshot | None:
        return snapshot.api

    def _families(self, role: str, api: ApiSnapshot) -> Iterator[Metric]:
        for name, value, documentation in (
            ("api_db_pool_size", api.pool_size, "Connections the process's Postgres pool keeps open"),
            ("api_db_pool_checked_out", api.pool_checked_out, "Connections of the process's Postgres pool in use now"),
            (
                "api_db_pool_overflow",
                api.pool_overflow,
                "Connections the process opened beyond its Postgres pool's size",
            ),
        ):
            if value is not None:
                yield _sample(_gauge(name, documentation), role, value)


class GatewayFamilies(RoleFamilies[GatewaySnapshot]):
    """A gateway's traffic, routing feed, preflight, exclusions, buffers, IP databases and tunnels."""

    def _section(self, snapshot: InstanceSnapshot) -> GatewaySnapshot | None:
        return snapshot.gateway

    def _families(self, role: str, gateway: GatewaySnapshot) -> Iterator[Metric]:
        requests = _counter(
            "gateway_requests_total", "Requests this gateway carried since it started, by outcome", "outcome"
        )
        requests.add_metric([role, "success"], gateway.successes)
        requests.add_metric([role, "failure"], gateway.failures)
        yield requests
        relayed = _counter(
            "gateway_bytes_total",
            "Bytes this gateway relayed since it started: sent upstream, and received from upstream",
            "direction",
        )
        relayed.add_metric([role, "sent"], gateway.bytes_sent)
        relayed.add_metric([role, "received"], gateway.bytes_received)
        yield relayed
        failures = _counter(
            "gateway_failures_total", "Failed and refused requests since this gateway started, by failure kind", "kind"
        )
        for kind, count in _counted_from_zero(FailureKind, gateway.failures_by_kind).items():
            failures.add_metric([role, kind], count)
        yield failures
        yield _sample(_gauge("gateway_in_flight", "Transfers open through this gateway now"), role, gateway.in_flight)
        yield _sample(
            _gauge("gateway_connections", "Client connections this gateway's proxy listener serves now"),
            role,
            gateway.active_connections,
        )
        if gateway.feed_age_seconds is not None:
            yield _sample(
                _gauge(
                    "gateway_feed_age_seconds",
                    "Seconds since this gateway applied a routing feed update; it grows while nothing changes",
                ),
                role,
                gateway.feed_age_seconds,
            )
        yield _sample(
            _counter("gateway_preflight_checks_total", "Preflight echoes this gateway sent through an upstream"),
            role,
            gateway.preflight_checks,
        )
        yield _sample(
            _counter(
                "gateway_preflight_rejections_total",
                "Preflight verdicts that placed an exit elsewhere than the request asked for",
            ),
            role,
            gateway.preflight_rejections,
        )
        exclusions = _gauge(
            "gateway_exclusions",
            "Proxies this gateway keeps out of routing on its own now: retired session slots, local quarantines",
            "kind",
        )
        exclusions.add_metric([role, "retired"], gateway.retired_slots)
        exclusions.add_metric([role, "quarantined"], gateway.local_quarantines)
        yield exclusions
        pending = _gauge(
            "gateway_pending", "Entries waiting in this gateway's buffers for their write to Redis", "buffer"
        )
        pending.add_metric([role, "metric_deltas"], gateway.pending_metric_deltas)
        pending.add_metric([role, "observations"], gateway.observations_pending)
        pending.add_metric([role, "sightings"], gateway.sightings_pending)
        yield pending
        dropped = _counter(
            "gateway_dropped_total",
            "Entries this gateway lost on their way to Redis: let go from a full buffer, pushed off a full queue, "
            "or in a write Redis could not apply",
            "buffer",
        )
        dropped.add_metric([role, "observations"], gateway.observations_dropped)
        dropped.add_metric([role, "sightings"], gateway.sightings_dropped)
        dropped.add_metric([role, "failure_log"], gateway.failure_log_dropped)
        dropped.add_metric([role, "mitm_captures"], gateway.mitm_captures_dropped)
        yield dropped
        yield _sample(
            _counter(
                "gateway_metric_flush_failures_total",
                "Metric writes Redis did not take; their counts wait in memory for the next write",
            ),
            role,
            gateway.metric_flush_failures,
        )
        yield _sample(
            _counter(
                "gateway_rate_limit_errors_total",
                "Rate-limit windows that could not be counted in Redis; those requests were allowed",
            ),
            role,
            gateway.rate_limit_errors,
        )
        databases = _gauge(
            "gateway_ip_databases",
            "IP databases this gateway has open, and the ones the feed lists that it still waits for",
            "state",
        )
        databases.add_metric([role, "loaded"], len(gateway.geo_databases_loaded))
        databases.add_metric([role, "missing"], len(gateway.geo_databases_missing))
        yield databases
        yield from self._tunnel_families(role, gateway.tunnels)

    def _tunnel_families(self, role: str, tunnels: Sequence[TunnelEndpointState]) -> Iterator[Metric]:
        """The gateway's tunnel endpoints, one series per protocol it terminates; nothing without one."""
        if not tunnels:
            return
        up = _gauge(
            "gateway_tunnel_up", "1 while this gateway's endpoint for a tunnel protocol is running, else 0", "protocol"
        )
        restarts = _counter(
            "gateway_tunnel_restarts_total",
            "Times the tunnel daemon exited on its own and was started again",
            "protocol",
        )
        denied = _counter("gateway_tunnel_denied_total", "Tunnel connections refused at admission", "protocol")
        for tunnel in tunnels:
            up.add_metric([role, tunnel.protocol], 1 if tunnel.state == "running" else 0)
            restarts.add_metric([role, tunnel.protocol], tunnel.restarts)
            denied.add_metric([role, tunnel.protocol], tunnel.denied)
        yield from (up, restarts, denied)
        # Every protocol's endpoint reports the one listener they share, so it is exported once.
        yield _sample(
            _gauge(
                "gateway_tunnel_connections", "Connections this gateway's tunnel listener relays now, every protocol"
            ),
            role,
            max(tunnel.active_connections for tunnel in tunnels),
        )


class LatencyFamilies(SectionFamilies):
    """A gateway's request latency and each of its steps, as histograms by connector and outcome.

    Read from the gateway's metering on each scrape, since they grow with every connector and the heartbeat, which
    publishes the snapshot, has no use for them.
    """

    def __init__(self, latencies: Callable[[], Sequence[LatencyHistogram]]) -> None:
        self._latencies = latencies

    def families(self, snapshot: InstanceSnapshot) -> Iterator[Metric]:
        histograms = self._latencies()
        if not histograms:
            return
        role = snapshot.runtime.role.value
        labels = ["role", "connector", "name", "outcome"]
        whole = HistogramMetricFamily(
            f"{METRIC_PREFIX}_gateway_request_duration_seconds",
            "Time from taking a request up to its tunnel being up or its response starting, or to its failure, "
            "by connector and outcome",
            labels=labels,
        )
        steps = HistogramMetricFamily(
            f"{METRIC_PREFIX}_gateway_request_step_duration_seconds",
            "The request duration by step: route (choosing the proxy), preflight (checking its exit), connect (to the "
            "vendor), handshake (the vendor reaching the target) and response (a plain HTTP request's status line)",
            labels=[*labels, "step"],
        )
        for series in histograms:
            values = [role, series.connector_id, series.connector_name, "success" if series.success else "failure"]
            family = whole if series.step is None else steps
            if series.step is not None:
                values.append(series.step)
            family.add_metric(values, _cumulative(series.buckets), series.sum_seconds)
        yield from (whole, steps)


class ControllerFamilies(RoleFamilies[ControllerSnapshot]):
    """A controller's lease, compiles and publishes; failing steps and install readings while it leads."""

    def _section(self, snapshot: InstanceSnapshot) -> ControllerSnapshot | None:
        return snapshot.controller

    def _families(self, role: str, controller: ControllerSnapshot) -> Iterator[Metric]:
        yield _sample(
            _gauge("controller_leader", "1 while this process holds the controller lease and runs its loops, else 0"),
            role,
            1 if controller.is_leader else 0,
        )
        yield _sample(
            _counter("controller_lease_terms_total", "Times this process took the controller lease"),
            role,
            controller.lease_terms,
        )
        yield _sample(
            _counter("controller_compiles_total", "Compiles of the routing definitions"), role, controller.compiles
        )
        if controller.compile_ms is not None:
            yield _sample(
                _gauge(
                    "controller_compile_duration_seconds", "How long the last compile of the routing definitions took"
                ),
                role,
                controller.compile_ms / _MS_PER_SECOND,
            )
        yield _sample(_counter("controller_publishes_total", "Routing feed publishes"), role, controller.publishes)
        if controller.publish_ms is not None:
            yield _sample(
                _gauge("controller_publish_duration_seconds", "How long the last routing feed publish took"),
                role,
                controller.publish_ms / _MS_PER_SECOND,
            )
        yield _sample(
            _counter(
                "controller_sections_written_total",
                "Routing feed sections publishes wrote, new or changed: what the gateways had to fetch",
            ),
            role,
            controller.sections_written,
        )
        yield _sample(
            _counter("controller_sections_removed_total", "Routing feed sections publishes removed"),
            role,
            controller.sections_removed,
        )
        if controller.is_leader:
            yield from self._leader_families(role, controller)

    def _leader_families(self, role: str, controller: ControllerSnapshot) -> Iterator[Metric]:
        """What only the leading controller's numbers mean now: failing steps, and what it sees of the whole install.

        A standby would repeat what it saw while it last led, so these come only from the leader, and a sum over every
        controller counts each once.
        """
        failing = _gauge(
            "controller_failing",
            "1 while the last compile or publish failed and none has succeeded since, else 0",
            "step",
        )
        failing.add_metric([role, "compile"], 1 if controller.compile_error is not None else 0)
        failing.add_metric([role, "publish"], 1 if controller.publish_error is not None else 0)
        yield failing
        yield _sample(_gauge("controller_feed_sections", "Sections the routing feed holds"), role, controller.sections)
        yield _sample(
            _gauge("controller_quarantined_proxies", "Proxies in rate-limit quarantine, as published"),
            role,
            controller.quarantined,
        )
        yield _sample(
            _gauge("controller_blocked_connectors", "Connectors blocked by their traffic limit, as published"),
            role,
            controller.blocked_connectors,
        )


class WorkerFamilies(RoleFamilies[WorkerSnapshot]):
    """A worker's batches, failures, time and entries in hand per job kind, and the sightings and observations it ingested."""

    def _section(self, snapshot: InstanceSnapshot) -> WorkerSnapshot | None:
        return snapshot.worker

    def _families(self, role: str, worker: WorkerSnapshot) -> Iterator[Metric]:
        runs = _counter("worker_job_runs_total", "Batches of each job kind this worker ran", "kind")
        failures = _counter(
            "worker_job_failures_total",
            "Batches of each job kind whose run raised: delivered again, and given up after repeated deliveries",
            "kind",
        )
        spent = _counter(
            "worker_job_duration_seconds_total", "Seconds this worker spent running each job kind's batches", "kind"
        )
        in_flight = _gauge(
            "worker_jobs_in_flight",
            "Entries of each job kind this worker holds now, in the batches it is running or giving up",
            "kind",
        )
        for kind, stats in sorted(worker.jobs.items()):
            runs.add_metric([role, kind.value], stats.runs)
            failures.add_metric([role, kind.value], stats.failures)
            spent.add_metric([role, kind.value], stats.duration_seconds)
            in_flight.add_metric([role, kind.value], stats.in_flight)
        yield from (runs, failures, spent, in_flight)
        for name, value, documentation in (
            (
                "worker_sightings_consumed_total",
                worker.sightings_consumed,
                "Preflight sightings this worker attributed",
            ),
            (
                "worker_sightings_dropped_total",
                worker.sightings_dropped,
                "Preflight sightings this worker gave up after repeated deliveries, unattributed",
            ),
            (
                "worker_observations_consumed_total",
                worker.observations_consumed,
                "IP observations and exit judgements this worker ingested",
            ),
            (
                "worker_observations_dropped_total",
                worker.observations_dropped,
                "IP observations and exit judgements this worker gave up after repeated deliveries, never ingested",
            ),
        ):
            yield _sample(_counter(name, documentation), role, value)


class SnapshotCollector(Collector):
    """Turns the process's snapshot, and a gateway's latency histograms, into metric families each time Prometheus scrapes."""

    def __init__(
        self, snapshot: Callable[[], InstanceSnapshot], latencies: Callable[[], Sequence[LatencyHistogram]]
    ) -> None:
        self._snapshot = snapshot
        # Every part of the process, in the order a scrape renders its families.
        self._sections: tuple[SectionFamilies, ...] = (
            RuntimeFamilies(),
            LoopLagFamilies(),
            LoopFamilies(),
            StreamFamilies(),
            ApiFamilies(),
            GatewayFamilies(),
            LatencyFamilies(latencies),
            ControllerFamilies(),
            WorkerFamilies(),
        )

    def collect(self) -> Iterator[Metric]:
        """Every family of one snapshot, built once per scrape."""
        snapshot = self._snapshot()
        for section in self._sections:
            yield from section.families(snapshot)


class Telemetry:
    """The process's metrics registry: one collector over its snapshot.

    A registry of its own rather than ``prometheus_client``'s global one, so
    a process renders exactly what its node reports, and two nodes built in
    one test process never share a metric.

    Args:
        latencies: the gateway's latency histograms as they stand; none for a process without a gateway.
    """

    def __init__(
        self,
        snapshot: Callable[[], InstanceSnapshot],
        latencies: Callable[[], Sequence[LatencyHistogram]] = tuple,
    ) -> None:
        self.registry = CollectorRegistry(auto_describe=False)
        self.registry.register(SnapshotCollector(snapshot, latencies))

    def render(self) -> bytes:
        """The text exposition of every family, from a snapshot built now."""
        return generate_latest(self.registry)


# --- Helpers ---------------------------------------------------------------------------


def _gauge(name: str, documentation: str, *labels: str) -> GaugeMetricFamily:
    """A gauge family named ``octoprox_<name>``, labelled ``role`` and then ``labels``."""
    return GaugeMetricFamily(f"{METRIC_PREFIX}_{name}", documentation, labels=["role", *labels])


def _counter(name: str, documentation: str, *labels: str) -> CounterMetricFamily:
    """A counter family named ``octoprox_<name>`` (ending in ``_total``), labelled ``role`` and then ``labels``."""
    return CounterMetricFamily(f"{METRIC_PREFIX}_{name}", documentation, labels=["role", *labels])


def _sample[F: GaugeMetricFamily | CounterMetricFamily](family: F, role: str, value: float) -> F:
    """``family`` with its one sample, labelled with the process's role."""
    family.add_metric([role], value)
    return family


def _cumulative(counts: Sequence[int]) -> list[tuple[str, float]]:
    """Per-bucket counts (``LATENCY_BUCKETS``, then above the last) as a histogram's cumulative buckets, ``+Inf`` last."""
    buckets: list[tuple[str, float]] = []
    total = 0
    for bound, count in zip(_LATENCY_BOUNDS, counts, strict=True):
        total += count
        buckets.append((bound, total))
    return buckets


def _counted_from_zero(values: type[StrEnum], counts: Mapping[str, int]) -> dict[str, int]:
    """Every one of ``values`` with its count, zero for those not counted yet, then any other value counted.

    Each series then exists from the first scrape, so ``rate()`` and ``increase()`` see a first gap or a first failure.
    """
    return {str(value): 0 for value in values} | dict(counts)
