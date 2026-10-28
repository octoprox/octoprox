# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's request latencies in histogram buckets, by connector, outcome and step, for its ``/metrics``."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field

from api.core.timing import Step

# Upper bounds of the buckets, in seconds: from a millisecond, where routing and
# a cached preflight land, past the default 30 s connection timeout.
LATENCY_BUCKETS: tuple[float, ...] = (
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)
_BOUNDS_MS = tuple(bound * 1000 for bound in LATENCY_BUCKETS)

# What one series is kept under: connector, success, and the step (None for the whole latency).
type _SeriesKey = tuple[str, bool, Step | None]


@dataclass(frozen=True, slots=True)
class LatencyHistogram:
    """One series of a gateway's request latencies since it started: a connector's, of one outcome, whole or one step."""

    connector_id: str
    connector_name: str
    success: bool
    # A Step's value, or None for the whole latency.
    step: str | None
    # How many fell in each bucket of LATENCY_BUCKETS, then above the last; not cumulative.
    buckets: list[int]
    sum_seconds: float


@dataclass(slots=True)
class _Series:
    """One histogram: counts per bucket, the last for above every bound, and the sum of what it counted."""

    counts: list[int] = field(default_factory=lambda: [0] * (len(_BOUNDS_MS) + 1))
    sum_ms: float = 0.0

    def observe(self, ms: float) -> None:
        self.counts[bisect_left(_BOUNDS_MS, ms)] += 1
        self.sum_ms += ms


class RequestLatencies:
    """Every finished request's latency and steps on this gateway since it started; never reset, as Prometheus expects."""

    def __init__(self) -> None:
        self._series: dict[_SeriesKey, _Series] = {}
        # Each connector's name as last seen, so a rename relabels its series instead of splitting them here.
        self._names: dict[str, str] = {}

    def record(
        self, connector_id: str, connector_name: str, success: bool, latency_ms: float, steps: Mapping[Step, float]
    ) -> None:
        """Count one finished request through the connector: its whole latency, and each step it went through."""
        self._names[connector_id] = connector_name
        self._observe((connector_id, success, None), latency_ms)
        for step, ms in steps.items():
            self._observe((connector_id, success, step), ms)

    def snapshot(self) -> list[LatencyHistogram]:
        """Every series as it stands, by connector, outcome and step; built for a scrape, never for the heartbeat."""
        return [
            LatencyHistogram(
                connector_id=connector_id,
                connector_name=self._names[connector_id],
                success=success,
                step=step.value if step is not None else None,
                buckets=list(series.counts),
                sum_seconds=series.sum_ms / 1000,
            )
            for (connector_id, success, step), series in sorted(
                self._series.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
            )
        ]

    def _observe(self, key: _SeriesKey, ms: float) -> None:
        series = self._series.get(key)
        if series is None:
            series = self._series[key] = _Series()
        series.observe(ms)
