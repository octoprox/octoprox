# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project failure log (the Failures page): what its endpoints answer, the examples and the counted summary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, Field


class FailureCursor(NamedTuple):
    """Where a failure log example stands: its rank (when Redis last took an addition to it, epoch ms), kind and fingerprint.

    Pages order by the three, highest first.
    """

    rank: int
    kind: str
    fingerprint: str


@dataclass(frozen=True)
class FailureLogRow:
    """One failure log example as read back: its description (the window that made it) and what every gateway added up."""

    cursor: FailureCursor
    fields: dict[str, Any]
    count: int
    first_ms: int
    last_ms: int
    latency_ms: float | None

    @staticmethod
    def fields_of(fingerprint: str) -> list[str]:
        """An example's fields in its kind's hash, as :meth:`read` takes their values: its description, then its tallies."""
        fp = fingerprint
        return [fp, f"{fp}:count", f"{fp}:first", f"{fp}:last", f"{fp}:latency", f"{fp}:timed"]

    @classmethod
    def read(cls, cursor: FailureCursor, values: Sequence[Any]) -> FailureLogRow | None:
        """The example from its fields' values; None when it has none, trimmed since its index was read."""
        description, n, first, last, latency, timed = values
        if description is None:
            return None
        return cls(
            cursor=cursor,
            fields=json.loads(description),
            count=int(n or 0),
            first_ms=int(first or 0),
            last_ms=int(last or 0),
            latency_ms=round(float(latency) / int(timed), 1)
            if latency and timed and int(timed)
            else None,
        )


class FailureRecord(BaseModel):
    """One example: a failure, and every identical one since, whichever gateway saw it."""

    id: str = Field(
        description="Kind and fingerprint, e.g. upstream_timeout:3f9a0c1b2d4e5f60; the same while it recurs"
    )
    kind: str = Field(description="What went wrong, e.g. upstream_timeout, no_proxy_available")
    status: int = Field(
        description="The status the client was answered (a tunnel device's connection is closed instead)"
    )
    message: str
    error_type: str | None = None
    method: str | None = None
    target: str | None = Field(
        None, description="host:port, or the URL without its query for an intercepted request"
    )
    session_id: str | None = None
    location: str | None = None
    peer_id: str | None = None
    peer_name: str | None = None
    proxy_id: str | None = None
    connector_id: str | None = None
    connector_name: str | None = None
    proxy: str | None = Field(None, description="host:port of the upstream proxy")
    upstream_status: int | None = Field(
        None, description="The vendor's HTTP status when it refused the request"
    )
    upstream_message: str | None = Field(
        None,
        description="What the vendor said about its refusal besides the status: its error headers and body text",
    )
    latency_ms: float | None = Field(None, description="Average time until the failure")
    step: str | None = Field(
        None,
        description="The step of the request's latency it failed in: route, preflight, connect, handshake or response",
    )
    steps: dict[str, float] | None = Field(
        None,
        description="Milliseconds each step took for the first failure of the example, by step",
    )
    count: int = Field(description="How many failures this example stands for")
    first_at: str
    last_at: str

    @classmethod
    def of(cls, row: FailureLogRow) -> FailureRecord:
        """The example as read back from the log, its id kind and fingerprint, its times naive UTC to the microsecond."""
        return cls(
            **row.fields,
            id=f"{row.cursor.kind}:{row.cursor.fingerprint}",
            kind=row.cursor.kind,
            latency_ms=row.latency_ms,
            count=row.count,
            first_at=_iso(row.first_ms),
            last_at=_iso(row.last_ms),
        )


class FailureLogResponse(BaseModel):
    """A page of a project's examples, the ones seen last first."""

    records: list[FailureRecord]
    next_cursor: str | None = None
    total: int = Field(
        description="Examples the log holds (of the kind asked for, or of all kinds)"
    )
    newest: int = Field(
        description="Where the log stood when read; pass it to /head to learn how many examples changed"
    )
    enabled: bool
    max_entries: int = Field(description="Examples kept per kind")
    retention_hours: int


class FailureLogHead(BaseModel):
    """Where the log stands now, so a page can say how many examples are new or seen again since it was read."""

    changed: int
    total: int


class UpstreamStatusCount(BaseModel):
    status: int
    count: int


class FailureKindCount(BaseModel):
    kind: str
    count: int
    upstream_statuses: list[UpstreamStatusCount] = Field(
        description="The vendor's statuses within this kind, most first"
    )


class ConnectorFailureCount(BaseModel):
    connector_id: str | None = Field(
        description="None for requests refused before a proxy was chosen"
    )
    name: str | None
    count: int


class FailureSeriesPoint(BaseModel):
    timestamp: datetime
    count: int


class FailureSeries(BaseModel):
    kind: str
    points: list[FailureSeriesPoint]


class FailureSummaryResponse(BaseModel):
    """Every failure of the window, counted: by kind, vendor status and connector, and over time by kind."""

    range: str
    since: datetime
    bucket_seconds: int
    total: int
    kinds: list[FailureKindCount] = Field(description="Most first")
    connectors: list[ConnectorFailureCount] = Field(description="Most first")
    series: list[FailureSeries] = Field(
        description="One per kind seen, most first, each with every bucket of the window"
    )
    enabled: bool


def _iso(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(epoch_ms / 1000, UTC)
        .replace(tzinfo=None)
        .isoformat(timespec="microseconds")
    )
