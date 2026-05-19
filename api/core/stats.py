# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Stats calculation helpers for proxies and projects."""

from typing import Any, Protocol, TypedDict


class MetricsDict(TypedDict, total=False):
    """Type for metrics dictionaries from Redis/Postgres."""

    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HasStats(Protocol):
    """Protocol for objects that have stats fields."""

    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


def combine_metrics(
    postgres_metrics: dict[str, Any],
    redis_metrics: dict[str, Any],
) -> MetricsDict:
    """Combine metrics from Postgres (historical) and Redis (current window).

    Computes weighted average for latency based on request counts.

    Args:
        postgres_metrics: Historical metrics from Postgres.
        redis_metrics: Current window metrics from Redis.

    Returns:
        Combined metrics dictionary.
    """
    pg_requests = postgres_metrics.get("request_count", 0)
    rd_requests = redis_metrics.get("request_count", 0)
    total_requests = pg_requests + rd_requests

    pg_latency = postgres_metrics.get("avg_latency_ms", 0)
    rd_latency = redis_metrics.get("avg_latency_ms", 0)

    if total_requests > 0:
        avg_latency = (pg_latency * pg_requests + rd_latency * rd_requests) / total_requests
    else:
        avg_latency = 0.0

    return MetricsDict(
        request_count=total_requests,
        success_count=postgres_metrics.get("success_count", 0) + redis_metrics.get("success_count", 0),
        failure_count=postgres_metrics.get("failure_count", 0) + redis_metrics.get("failure_count", 0),
        avg_latency_ms=avg_latency,
        bytes_sent=postgres_metrics.get("bytes_sent", 0) + redis_metrics.get("bytes_sent", 0),
        bytes_received=postgres_metrics.get("bytes_received", 0) + redis_metrics.get("bytes_received", 0),
    )


def apply_metrics(target: HasStats, metrics: MetricsDict) -> None:
    """Apply combined metrics to a target object.

    Args:
        target: Object with stats fields (Proxy or Project).
        metrics: Combined metrics to apply.
    """
    target.request_count = metrics.get("request_count", 0)
    target.success_count = metrics.get("success_count", 0)
    target.failure_count = metrics.get("failure_count", 0)
    target.avg_latency_ms = metrics.get("avg_latency_ms", 0.0)
    target.bytes_sent = metrics.get("bytes_sent", 0)
    target.bytes_received = metrics.get("bytes_received", 0)


def increment_stats(
    target: HasStats,
    success: bool,
    latency_ms: float,
    bytes_sent: int = 0,
    bytes_received: int = 0,
) -> None:
    """Increment stats on a target object after a request.

    Updates request/success/failure counts, computes running average latency,
    and adds to byte counters.

    Args:
        target: Object with stats fields (Proxy or Project).
        success: Whether the request was successful.
        latency_ms: Request latency in milliseconds.
        bytes_sent: Bytes sent in the request.
        bytes_received: Bytes received in the response.
    """
    old_count = target.request_count
    target.request_count += 1

    if success:
        target.success_count += 1
    else:
        target.failure_count += 1

    # Compute proper weighted average: new_avg = (old_avg * old_count + new_value) / new_count
    if old_count == 0:
        target.avg_latency_ms = latency_ms
    else:
        target.avg_latency_ms = (target.avg_latency_ms * old_count + latency_ms) / target.request_count

    target.bytes_sent += bytes_sent
    target.bytes_received += bytes_received


# ---------------------------------------------------------------------------
# Delta accumulators used by the periodic-flush metric pipeline.
#
# Each instance keeps a per-entity dict of pending deltas accumulated by
# per-request handlers. Every few seconds the flush loop drains them into
# Redis (one batched ``HINCRBY`` pipeline) and announces the same deltas on
# pub/sub so peers can update their in-memory view without a Redis read.
#
# The wire format is deliberately additive ints / floats — easy to apply
# anywhere via ``apply_delta`` or merge back into the pending dict via
# ``merge_delta_into`` on failure.
# ---------------------------------------------------------------------------


# A metric delta is a plain dict of the same fields as ``MetricsDict``
# plus ``latency_sum_ms`` (running sum, so it can be combined across
# many requests without losing the weighted-average math). It's typed
# loosely on purpose: the JSON round-trip through Pub/Sub strips
# stricter types anyway.
MetricDelta = dict[str, Any]


def empty_delta() -> MetricDelta:
    """Construct a zero delta."""
    return {
        "request_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "latency_sum_ms": 0.0,
        "bytes_sent": 0,
        "bytes_received": 0,
    }


def accumulate_delta(
    delta: MetricDelta,
    success: bool,
    latency_ms: float,
    bytes_sent: int = 0,
    bytes_received: int = 0,
) -> None:
    """Fold a single request's contribution into the running delta."""
    delta["request_count"] += 1
    if success:
        delta["success_count"] += 1
    else:
        delta["failure_count"] += 1
    delta["latency_sum_ms"] += latency_ms
    delta["bytes_sent"] += bytes_sent
    delta["bytes_received"] += bytes_received


def merge_delta_into(dst: MetricDelta, src: MetricDelta) -> None:
    """Add ``src`` into ``dst`` field-by-field (used when retrying a failed flush)."""
    dst["request_count"] += src.get("request_count", 0)
    dst["success_count"] += src.get("success_count", 0)
    dst["failure_count"] += src.get("failure_count", 0)
    dst["latency_sum_ms"] += src.get("latency_sum_ms", 0.0)
    dst["bytes_sent"] += src.get("bytes_sent", 0)
    dst["bytes_received"] += src.get("bytes_received", 0)


def apply_delta(target: HasStats, delta: MetricDelta) -> None:
    """Apply a peer instance's delta to a local in-memory stats target.

    The weighted-average latency update is the same math as
    ``increment_stats``, just for a batch of N requests at once:

        new_avg = (old_avg * old_count + delta_latency_sum) / new_total_count
    """
    count = int(delta.get("request_count", 0))
    if count == 0:
        return
    old_count = target.request_count
    target.request_count = old_count + count
    target.success_count += int(delta.get("success_count", 0))
    target.failure_count += int(delta.get("failure_count", 0))
    target.bytes_sent += int(delta.get("bytes_sent", 0))
    target.bytes_received += int(delta.get("bytes_received", 0))
    latency_sum = float(delta.get("latency_sum_ms", 0.0))
    if old_count == 0:
        target.avg_latency_ms = latency_sum / count
    else:
        total_latency = target.avg_latency_ms * old_count + latency_sum
        target.avg_latency_ms = total_latency / target.request_count

