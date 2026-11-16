# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports from the reports stream into the records: the workers' ingest, and the counts it keeps for Prometheus.

A batch's exit IP reports are folded to one count per record and applied in one transaction. Its connector reports go to
the Redis site windows of their connector, site and place, in one transaction after that commit, for the next metrics
flush to apply with the requests counted there. A runtime signal follows when an avoid a project routes by moved. A
batch delivered again after it committed counts its reports twice, which the records' decay absorbs.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.consumers import GroupConsumer, decode_items
from api.core.stats import RequestCount
from api.core.workers import WorkerName
from api.db.redis import EXIT_REPORT_STREAM_KEY
from api.db.repository import ConnectorRepository
from api.db.reputation_repository import ExitReputationRepository
from api.jobs.messages import ExitReportMessage
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from api.reputation.signal import ReputationSignal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import StreamEntry
    from api.models.exit_reports import ExitVerdict

logger = structlog.get_logger()

# Entries one read of the reports stream takes, applied in one transaction.
REPORT_BATCH_SIZE = 500
# Entry IDs a log line names at most.
_LOGGED_IDS = 5


@dataclass(frozen=True, slots=True)
class ExitReportCount:
    """How many reports of one verdict this worker took for one connector since it started."""

    # "" for the reports that named no connector, by id and by name.
    connector_id: str
    connector_name: str
    verdict: str
    count: int


class ExitReportCounts:
    """The reports a worker took since it started, by connector and verdict; never reset, as Prometheus expects."""

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str]] = Counter()
        # Each connector's name as last seen, so a rename relabels its series instead of splitting them here.
        self._names: dict[str, str] = {}

    def add(self, connector_id: str, connector_name: str, verdict: ExitVerdict) -> None:
        """Count one report of ``verdict`` taken for the connector ("" for none)."""
        self._names[connector_id] = connector_name
        self._counts[(connector_id, verdict.value)] += 1

    def snapshot(self) -> list[ExitReportCount]:
        """Every count as it stands, by connector and verdict; built for a scrape, never for the heartbeat."""
        return [
            ExitReportCount(connector_id, self._names[connector_id], verdict, count)
            for (connector_id, verdict), count in sorted(self._counts.items())
        ]


class ExitReportIngest(GroupConsumer):
    """Applies the exit reports the api appended on the workers, a batch at a time: to the exit IP records in one
    transaction, and to the site windows the metrics flush applies to the connector records; it signals the controller
    after the commit when routing moved.

    A project that is gone, or whose exit reputation went off since the report was taken, records nothing, and neither
    does a connector that is gone or another project's.
    """

    def __init__(self, redis_client: RedisClient, session_factory: SessionFactory) -> None:
        super().__init__(
            redis_client,
            EXIT_REPORT_STREAM_KEY,
            name=WorkerName.EXIT_REPORT_INGEST,
            count=REPORT_BATCH_SIZE,
        )
        self._session_factory = session_factory
        self._signal = ReputationSignal(redis_client)
        self.counts = ExitReportCounts()

    async def _handle(self, entries: list[StreamEntry]) -> None:
        """Apply one batch, its malformed entries logged and completed with it; raising leaves the whole batch pending."""
        reports, malformed = decode_items(entries, ExitReportMessage.model_validate_json)
        if malformed:
            logger.warning(
                "Malformed exit reports skipped",
                count=len(malformed),
                ids=[entry.id for entry in malformed[:_LOGGED_IDS]],
            )
        if not reports:
            return
        started = time.monotonic()
        # In the order they were made, so a record keeps the latest report's reason.
        reports.sort(key=lambda report: report.reported_at)
        ips: dict[ExitIpKey, ReportCounts] = ReportCounts.fold(
            (key, report.counts) for report in reports if (key := report.ip_key) is not None
        )
        async with self._session_factory() as session:
            repository = ExitReputationRepository(session)
            taken = await repository.recording_connectors(
                {(report.project_id, report.connector_id) for report in reports if report.connector_id}
            )
            changes = await repository.apply_reports(ips, utc_now())
            ip_keys = {change.key for change in changes.ips}
            connectors: dict[ConnectorSiteKey, ReportCounts] = ReportCounts.fold(
                (window, report.counts)
                for report in reports
                if (window := report.connector_key) is not None and (window.project_id, window.connector_id) in taken
            )
            applied = [
                report
                for report in reports
                if report.ip_key in ip_keys or report.connector_key in connectors
            ]
            names = await self._connector_names(session, {report.connector_id or "" for report in applied})
            await session.commit()
        try:
            await self._redis.add_site_reports(
                {key: RequestCount(blocked=counts.bad, good=counts.good) for key, counts in connectors.items()}
            )
        finally:
            # The avoids committed: the controller hears of them even when the windows wait for the batch's next delivery.
            await self._signal.send(changes)
        for report in applied:
            connector_id = report.connector_id or ""
            self.counts.add(connector_id, names.get(connector_id, ""), report.verdict)
        logger.debug(
            "Exit reports applied",
            count=len(applied),
            skipped=len(reports) - len(applied),
            ip_records=len(changes.ips),
            connector_windows=len(connectors),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    async def _connector_names(
        session: AsyncSession, connector_ids: Collection[str]
    ) -> dict[str, str]:
        """The names of the connectors among ``connector_ids`` that still exist, one query."""
        wanted = [connector_id for connector_id in connector_ids if connector_id]
        if not wanted:
            return {}
        return {
            connector.id: connector.name
            for connector in await ConnectorRepository(session).get_many(wanted)
        }
