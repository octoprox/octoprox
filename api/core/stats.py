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

