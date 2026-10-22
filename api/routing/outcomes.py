# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""This gateway's request outcomes per proxy, which the ``health_based`` strategy routes on."""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from api.core.stats import MetricDelta


class LocalOutcomes:
    """This gateway's outcomes per proxy since it started; one per gateway, recorded by its request metering."""

    def __init__(self) -> None:
        # Only proxies with an outcome recorded, until a routing snapshot without them is applied.
        self._proxies: dict[str, MetricDelta] = {}

    def record(self, proxy_id: str, success: bool, latency_ms: float) -> None:
        """Count one finished request through the proxy, with its latency in milliseconds: until its tunnel was up or it was answered."""
        self._proxies.setdefault(proxy_id, MetricDelta()).add_request(success, latency_ms)

    def of_proxy(self, proxy_id: str) -> tuple[int, int, float]:
        """The proxy's ``(requests, successes, latency sum in ms)`` on this gateway; zeros for a proxy never seen."""
        seen = self._proxies.get(proxy_id)
        if seen is None:
            return 0, 0, 0.0
        return seen.request_count, seen.success_count, seen.latency_sum_ms

    def retain(self, proxy_ids: AbstractSet[str]) -> None:
        """Forget every proxy not in ``proxy_ids``; the gateway calls it with each routing snapshot it applies."""
        for proxy_id in [p for p in self._proxies if p not in proxy_ids]:
            del self._proxies[proxy_id]
