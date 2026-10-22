# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Metric batches for proxies, projects, connectors, hosts and tunnel devices."""

from collections.abc import Iterator
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

# The row a flush window's hosts past the cap are folded into (see
# ``Settings.host_metrics_max_hosts``). Not a valid hostname, so it cannot
# collide with a real destination.
HOST_OVERFLOW = "(other)"

# Longest host the history table stores; anything longer is not a hostname
# and is folded into the overflow row rather than truncated into a lookalike.
MAX_HOST_LENGTH = 255

# What one failure count is kept under (the failure_metrics table): project,
# connector (None when no proxy was chosen), kind (a FailureKind), and the
# vendor's HTTP status when it refused the request (else None).
type FailureCountKey = tuple[str, str | None, str, int | None]


@dataclass(frozen=True, slots=True)
class FailureExample:
    """One failure log example as a gateway's window hands it to Redis: what failed, and how often and when.

    Every gateway's windows add up in the one row its ``fingerprint`` names; ``fields`` (JSON) describe it, written
    when the row is new. Times are epoch milliseconds.
    """

    project_id: str
    kind: str
    fingerprint: str
    fields: str
    count: int
    first_ms: int
    last_ms: int
    latency_total: float = 0.0
    latency_n: int = 0


# What one host row is counted under (the Hosts page): project, connector,
# and the host as ``normalize_host`` gives it (or ``HOST_OVERFLOW``).
type HostKey = tuple[str, str, str]

# What one demand count is kept under (the demand hashes): project, the epoch
# minute it fell in, and the hash field (completed requests or refusals, see
# api.core.demand_tracker).
type DemandCountKey = tuple[str, int, str]


class FailureKind(StrEnum):
    """What went wrong, from the client's side of the proxy, in three groups: the request metrics' failures, refusals, interception.

    A refusal came before any proxy was used, so the metrics count it nowhere, and an intercepted request's connection
    the metrics counted as a success already.
    """

    # Counted as failures by the request metrics.
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    UPSTREAM_REJECTED = "upstream_rejected"
    UPSTREAM_ERROR = "upstream_error"
    CLIENT_CLOSED = "client_closed"
    INTERRUPTED = "interrupted"
    # Refused before a proxy was used (traffic_limit_exceeded can also cut a
    # request that already had one, and is then counted).
    NO_PROXY_AVAILABLE = "no_proxy_available"
    ALL_PROXIES_QUARANTINED = "all_proxies_quarantined"
    TRAFFIC_LIMIT_EXCEEDED = "traffic_limit_exceeded"
    PREFLIGHT_MISMATCH = "preflight_mismatch"
    INVALID_LOCATION = "invalid_location"
    # One request inside an intercepted (MITM) connection.
    INTERCEPTED_REQUEST_FAILED = "intercepted_request_failed"


def normalize_host(host: str | None) -> str | None:
    """The form a destination host is counted under, or None for nothing countable.

    Case and a trailing dot are folded, a bracketed IPv6 literal keeps its brackets, and whitespace, control
    characters and empty values are no hosts.
    """
    if not host:
        return None
    value = host.strip().lower().rstrip(".")
    if not value or " " in value or not value.isprintable():
        return None
    if len(value) > MAX_HOST_LENGTH:
        return HOST_OVERFLOW
    return value


# ---------------------------------------------------------------------------
# One additive shape for every batch of metrics.
#
# Each gateway keeps a per-entity dict of pending deltas accumulated by
# per-request handlers (api.gateway.metering). Every few seconds one batched
# ``HINCRBY`` pipeline adds them to the Redis windows, and a worker's metrics
# flush job takes the windows into the Postgres history and totals. The Redis
# window and the stored totals come back in the same shape, so an entity's
# counters are a sum of the two.
#
# The wire format is the model's fields as additive ints / floats, so a
# delta can be added anywhere and merged back into the pending dict when a
# flush fails.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WindowCounts:
    """Additive counters kept in one Redis hash, a field each: the shape every metrics window is read and written in."""

    def to_dict(self) -> dict[str, Any]:
        """The wire form: field name to additive value."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def increments(self) -> Iterator[tuple[str, int | float, bool]]:
        """Each non-zero counter as (field, amount, fraction), the amount of its field's declared type and fraction for a float field.

        The declared type decides, not the amount's: Redis refuses HINCRBY on a field that already holds a fraction, so a
        float field is always added with HINCRBYFLOAT.
        """
        for f in fields(self):
            value = getattr(self, f.name)
            if value:
                fraction = f.type is float
                yield f.name, float(value) if fraction else int(value), fraction

    @classmethod
    def from_dict[C: "WindowCounts"](cls: type[C], raw: Any) -> C:
        """Counters from their wire form, a missing field read as zero, so a peer with fewer fields still applies.

        A non-mapping raises TypeError, and a value that is not a number ValueError.
        """
        if not isinstance(raw, dict):
            raise TypeError(f"metric delta must be a mapping, got {type(raw).__name__}")
        counts = cls()
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise ValueError(f"{f.name} must be a number, got {value!r}")
            setattr(counts, f.name, float(value) if f.type is float else int(value))
        return counts


@dataclass(slots=True)
class MetricDelta(WindowCounts):
    """A batch of requests' contribution to one entity's metrics, in additive form, its latency carried as a sum.

    A slotted dataclass rather than a pydantic model, since every completed request mutates three of these in place.
    """

    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    latency_sum_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0

    def add_request(
        self,
        success: bool,
        latency_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Fold a single completed request into the delta."""
        self.request_count += 1
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        self.latency_sum_ms += latency_ms
        self.add_bytes(bytes_sent, bytes_received)

    def add_bytes(self, bytes_sent: int, bytes_received: int) -> None:
        """Fold bytes into the delta without counting a request.

        A completed request folds its remainder through ``add_request``; a
        transfer still running reports its progress here (see ``TrafficMeter``).
        """
        self.bytes_sent += bytes_sent
        self.bytes_received += bytes_received

    @property
    def avg_latency_ms(self) -> float:
        """Mean latency of the requests in the batch, 0 when there are none."""
        return self.latency_sum_ms / self.request_count if self.request_count else 0.0

    @classmethod
    def summed[D: "MetricDelta"](cls: type[D], *batches: "MetricDelta | None") -> D:
        """One delta holding the sum of ``batches``; None entries are skipped."""
        total = cls()
        for batch in batches:
            if batch is not None:
                total.merge(batch)
        return total

    def merge(self, other: "MetricDelta") -> None:
        """Add ``other`` into this delta field by field (used when retrying a failed flush)."""
        self.request_count += other.request_count
        self.success_count += other.success_count
        self.failure_count += other.failure_count
        self.latency_sum_ms += other.latency_sum_ms
        self.bytes_sent += other.bytes_sent
        self.bytes_received += other.bytes_received


@dataclass(slots=True)
class TunnelPeerMetricDelta(MetricDelta):
    """A tunnel device's batch: the request shape plus the two name-resolution signals.

    They count the connections relayed by address because the device resolves names outside the tunnel, and the
    encrypted-DNS connections closed.
    """

    by_address: int = 0
    encrypted_dns_blocked: int = 0

    def merge(self, other: MetricDelta) -> None:
        # Named, not super(): slots=True rebuilds the class, so the zero-argument
        # form would bind to the class the dataclass decorator threw away.
        MetricDelta.merge(self, other)
        if isinstance(other, TunnelPeerMetricDelta):
            self.by_address += other.by_address
            self.encrypted_dns_blocked += other.encrypted_dns_blocked


@dataclass(slots=True)
class FailureCount(WindowCounts):
    """How many requests failed under one failure count key (``FailureCountKey``) in a window."""

    failure_count: int = 0


def fold_host_tail(host_deltas: dict[HostKey, MetricDelta], max_hosts: int) -> dict[HostKey, MetricDelta]:
    """Keep each project's busiest ``max_hosts`` hosts named, by requests then bytes; fold the rest per connector.

    The rest count under ``HOST_OVERFLOW``, and with no ceiling (0) the window comes back as it is, every delta
    otherwise a fresh one.
    """
    if max_hosts <= 0 or not host_deltas:
        return host_deltas
    volume: dict[tuple[str, str], tuple[int, int]] = {}
    for (project_id, _connector_id, host), delta in host_deltas.items():
        if host == HOST_OVERFLOW:
            continue
        requests, size = volume.get((project_id, host), (0, 0))
        volume[(project_id, host)] = (requests + delta.request_count, size + delta.bytes_sent + delta.bytes_received)
    by_project: dict[str, list[tuple[str, tuple[int, int]]]] = {}
    for (project_id, host), rank in volume.items():
        by_project.setdefault(project_id, []).append((host, rank))
    named: set[tuple[str, str]] = set()
    for project_id, hosts in by_project.items():
        if len(hosts) > max_hosts:
            hosts.sort(key=lambda entry: entry[1], reverse=True)
        named.update((project_id, host) for host, _rank in hosts[:max_hosts])
    folded: dict[HostKey, MetricDelta] = {}
    for (project_id, connector_id, host), delta in host_deltas.items():
        key = (project_id, connector_id, host if (project_id, host) in named else HOST_OVERFLOW)
        folded.setdefault(key, MetricDelta()).merge(delta)
    return folded
