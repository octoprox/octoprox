# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Metric batches for proxies, projects and connectors."""

from dataclasses import dataclass, fields
from typing import Any, Protocol


class HasStats(Protocol):
    """Protocol for objects that have stats fields."""

    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


# ---------------------------------------------------------------------------
# One additive shape for every batch of metrics.
#
# Each instance keeps a per-entity dict of pending deltas accumulated by
# per-request handlers. Every few seconds the flush loop drains them into
# Redis (one batched ``HINCRBY`` pipeline) and announces the same deltas on
# pub/sub so peers can update their in-memory view without a Redis read.
# The Redis window and the Postgres history totals come back in the same
# shape, so hydrating an entity is a sum followed by ``set_on``.
#
# The wire format is the model's fields as additive ints / floats, so a
# delta can be applied anywhere and merged back into the pending dict when
# a flush fails.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricDelta:
    """A batch of requests' contribution to one entity's metrics, in additive form.

    Latency is carried as a sum, not an average: sums combine across
    batches and sources without losing the weighted-average math, and the
    average is derived when something displays it.

    A slotted dataclass rather than a pydantic model on purpose: every
    completed request and every progress report mutates three of these in
    place, and a pydantic field assignment costs about a microsecond
    against a few nanoseconds for a slot. The wire form is handled by
    ``to_dict`` and ``from_dict`` instead.
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
    def summed(cls, *batches: "MetricDelta | None") -> "MetricDelta":
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

    def set_on(self, target: HasStats) -> None:
        """Make the target's counters equal this batch (hydration from the sources of truth)."""
        target.request_count = self.request_count
        target.success_count = self.success_count
        target.failure_count = self.failure_count
        target.avg_latency_ms = self.avg_latency_ms
        target.bytes_sent = self.bytes_sent
        target.bytes_received = self.bytes_received

    def apply_to(self, target: HasStats) -> None:
        """Add the delta to an in-memory stats target.

        The target keeps an average, so the update is weighted by counts:

            new_avg = (old_avg * old_count + delta_latency_sum) / new_total_count
        """
        target.bytes_sent += self.bytes_sent
        target.bytes_received += self.bytes_received
        if self.request_count == 0:
            # Progress of transfers still running: bytes only, no request yet.
            return
        old_count = target.request_count
        target.request_count = old_count + self.request_count
        target.success_count += self.success_count
        target.failure_count += self.failure_count
        if old_count == 0:
            target.avg_latency_ms = self.latency_sum_ms / self.request_count
        else:
            total_latency = target.avg_latency_ms * old_count + self.latency_sum_ms
            target.avg_latency_ms = total_latency / target.request_count

    def to_dict(self) -> dict[str, Any]:
        """The wire form: field name to additive value."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, raw: Any) -> "MetricDelta":
        """A delta from its wire form. Missing fields read as zero; wrong types raise.

        Raises TypeError for a non-mapping and ValueError for a value that is
        not a number, so a peer on a version with fewer fields still applies
        cleanly while garbage does not.
        """
        if not isinstance(raw, dict):
            raise TypeError(f"metric delta must be a mapping, got {type(raw).__name__}")
        delta = cls()
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise ValueError(f"{f.name} must be a number, got {value!r}")
            setattr(delta, f.name, float(value) if f.type is float else int(value))
        return delta

    @staticmethod
    def dump_many(deltas: dict[str, "MetricDelta"]) -> dict[str, dict[str, Any]]:
        """The wire form of a per-entity batch, for the pub/sub payload."""
        return {entity_id: delta.to_dict() for entity_id, delta in deltas.items()}

    @classmethod
    def parse_many(cls, raw: Any) -> dict[str, "MetricDelta"]:
        """Per-entity deltas from a pub/sub payload; entries that do not parse are dropped."""
        if not isinstance(raw, dict):
            return {}
        parsed: dict[str, MetricDelta] = {}
        for entity_id, entry in raw.items():
            try:
                parsed[str(entity_id)] = cls.from_dict(entry)
            except (TypeError, ValueError):
                continue
        return parsed
