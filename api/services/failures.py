# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project failure log (the Failures page): examples from the failure log gateways add to in Redis, and a summary
from the ``failure_metrics`` history the workers' metrics flush job writes.

The project is read from Postgres in a session closed before Redis is read; Redis not answering fails the read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from api.core import utc_now
from api.core.stats import FailureKind
from api.db.repository import ConnectorRepository, MetricsRepository, ProjectRepository
from api.models.failures import (
    ConnectorFailureCount,
    FailureCursor,
    FailureKindCount,
    FailureLogHead,
    FailureLogResponse,
    FailureLogRow,
    FailureRecord,
    FailureSeries,
    FailureSeriesPoint,
    FailureSummaryResponse,
    UpstreamStatusCount,
)
from api.models.metrics import HOST_HISTORY_BUCKETS, RANGE_CONFIG
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.models.metrics import MetricsRange

# Every kind a failure is logged under, the streams a page reads across.
ALL_KINDS = [kind.value for kind in FailureKind]
# A page's next_cursor, rank:kind:fingerprint, and an example's id, kind:fingerprint.
CURSOR_PATTERN = r"^\d+:[a-z_]+:[0-9a-f]+$"
FAILURE_ID_PATTERN = r"^[a-z_]+:[0-9a-f]+$"
# Examples a page holds unless asked for fewer, and at most.
DEFAULT_FAILURE_PAGE = 200
MAX_FAILURE_PAGE = 1000


class FailureLogService:
    """Reads a project's failure examples and its counted failures, and empties its log.

    Args:
        enabled: Whether gateways log failures at all; with max_entries and retention_hours, what the answers tell.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        *,
        enabled: bool,
        max_entries: int,
        retention_hours: int,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._enabled = enabled
        self._max_entries = max_entries
        self._retention_hours = retention_hours

    async def page(
        self,
        project_id: str,
        kind: FailureKind | None = None,
        *,
        count: int = DEFAULT_FAILURE_PAGE,
        cursor: str | None = None,
    ) -> FailureLogResponse:
        """The project's examples, the ones seen last first, across every kind or of ``kind``, from after ``cursor``."""
        await self._require_project(project_id)
        kinds = [kind.value] if kind else ALL_KINDS
        after: FailureCursor | None = None
        if cursor:
            rank, after_kind, fingerprint = cursor.split(":")
            after = FailureCursor(int(rank), after_kind, fingerprint)

        # One more than asked tells whether there is an older page.
        rows, total = await asyncio.gather(
            self._redis.get_failures(project_id, kinds, count=count + 1, after=after),
            self._redis.count_failures(project_id, kinds),
        )
        page = rows[:count]
        return FailureLogResponse(
            records=[FailureRecord.of(row) for row in page],
            next_cursor=_cursor(page[-1]) if len(rows) > count and page else None,
            total=total,
            # The first page's top: an example added to after the read ranks above it.
            newest=rows[0].cursor.rank if rows and after is None else 0,
            enabled=self._enabled,
            max_entries=self._max_entries,
            retention_hours=self._retention_hours,
        )

    async def head(
        self, project_id: str, kind: FailureKind | None = None, *, since: int = 0
    ) -> FailureLogHead:
        """How many examples are new or seen again since the page read at ``since``, and how many there are."""
        await self._require_project(project_id)
        kinds = [kind.value] if kind else ALL_KINDS
        total, changed = await asyncio.gather(
            self._redis.count_failures(project_id, kinds),
            self._redis.failures_changed(project_id, kinds, since),
        )
        return FailureLogHead(changed=changed, total=total)

    async def summary(self, project_id: str, window: MetricsRange) -> FailureSummaryResponse:
        """Every failure of ``window`` from the flushed history: by kind, vendor status and connector, most first, and
        over time by kind with every bucket of the window."""
        delta, _limit, _bucket = RANGE_CONFIG[window]
        bucket_seconds = HOST_HISTORY_BUCKETS[window]
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            now = utc_now()
            since = now - delta
            repo = MetricsRepository(session)
            breakdown = await repo.get_failure_breakdown(project_id, since)
            history = await repo.get_failure_history(project_id, since, bucket_seconds)
            names = {
                c.id: c.name for c in await ConnectorRepository(session).get_by_project(project_id)
            }

        by_kind: dict[str, int] = {}
        statuses: dict[str, dict[int, int]] = {}
        by_connector: dict[str | None, int] = {}
        for row in breakdown:
            n = row["failure_count"]
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + n
            if row["upstream_status"] is not None:
                per_kind = statuses.setdefault(row["kind"], {})
                per_kind[row["upstream_status"]] = per_kind.get(row["upstream_status"], 0) + n
            by_connector[row["connector_id"]] = by_connector.get(row["connector_id"], 0) + n
        ranked = sorted(by_kind, key=lambda k: (-by_kind[k], k))

        # Every bucket of the window for every kind, zeros where it was quiet, so the series stack.
        first = int(since.replace(tzinfo=UTC).timestamp()) // bucket_seconds * bucket_seconds
        last = int(now.replace(tzinfo=UTC).timestamp())
        buckets = [
            datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
            for epoch in range(first, last + 1, bucket_seconds)
        ]
        points: dict[str, dict[datetime, int]] = {}
        for row in history:
            points.setdefault(row["kind"], {})[row["timestamp"]] = row["failure_count"]
        # A row the controller stamped past this process's clock (a few ms of skew at
        # a bucket boundary) gets its bucket too, rather than missing from the chart.
        buckets = sorted({*buckets, *(row["timestamp"] for row in history)})

        return FailureSummaryResponse(
            range=window,
            since=since,
            bucket_seconds=bucket_seconds,
            total=sum(by_kind.values()),
            kinds=[
                FailureKindCount(
                    kind=k,
                    count=by_kind[k],
                    upstream_statuses=[
                        UpstreamStatusCount(status=s, count=n)
                        for s, n in sorted(
                            statuses.get(k, {}).items(), key=lambda item: (-item[1], item[0])
                        )
                    ],
                )
                for k in ranked
            ],
            connectors=[
                ConnectorFailureCount(
                    connector_id=cid, name=names.get(cid) if cid else None, count=n
                )
                for cid, n in sorted(by_connector.items(), key=lambda item: -item[1])
            ],
            series=[
                FailureSeries(
                    kind=k,
                    points=[
                        FailureSeriesPoint(timestamp=ts, count=points.get(k, {}).get(ts, 0))
                        for ts in buckets
                    ],
                )
                for k in ranked
            ],
            enabled=self._enabled,
        )

    async def get(self, project_id: str, failure_id: str) -> FailureRecord:
        """One example by its id (kind:fingerprint); not found once the log no longer keeps it."""
        await self._require_project(project_id)
        kind, _, fingerprint = failure_id.partition(":")
        row = await self._redis.get_failure(project_id, kind, fingerprint)
        if row is None:
            raise NotFoundError("Failure example not found")
        return FailureRecord.of(row)

    async def clear(self, project_id: str) -> None:
        """Empty the project's log of every kind; the counts in history stay, and new failures keep being logged."""
        await self._require_project(project_id)
        await self._redis.clear_failures(project_id, ALL_KINDS)

    async def _require_project(self, project_id: str) -> None:
        """Not found unless the project exists, read in a session closed before any Redis read."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")


def _cursor(row: FailureLogRow) -> str:
    """The opaque cursor of a failure log row: its rank, kind and fingerprint."""
    return f"{row.cursor.rank}:{row.cursor.kind}:{row.cursor.fingerprint}"
