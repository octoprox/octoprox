# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports from the reports stream into the records: the workers' ingest, and the counts it keeps for Prometheus
and the System page.

A batch's reports are checked first, in its transaction: the project must record, and a report the exit headers named
must name a connector of the project, dynamic, or with IPs and an exit IP one of its rows was last seen exiting from.
What fails is dropped, counted by reason. The exit IP reports left are folded to one count per record and applied in
one transaction. The connector reports go to the Redis site windows of their connector, site and place, in one
transaction after that commit, for the next metrics flush to apply with the requests counted there. A runtime signal
follows when an avoid a project routes by moved. A batch delivered again after it committed counts its reports twice,
which the records' decay absorbs.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

import structlog

from api.core import utc_now
from api.core.consumers import GroupConsumer, decode_items
from api.core.stats import RequestCount
from api.core.workers import WorkerName
from api.db.redis import EXIT_REPORT_STREAM_KEY
from api.db.repository import ConnectorRepository, ProxyRepository
from api.db.reputation_repository import ExitReputationRepository, Recording
from api.jobs.messages import ExitReportMessage
from api.models.exit_reports import DropReason
from api.providers.sdk.strategies import META_DISCOVERED_IP
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from api.reputation.signal import ReputationSignal
from api.reputation.site import canonical_ip

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
    """How many reports of one verdict this worker took for one connector since it started, or dropped for one reason."""

    # "" for the reports that named no connector, by id and by name, and for those dropped.
    connector_id: str
    connector_name: str
    verdict: str
    count: int
    # Why the worker dropped them (a DropReason); "" for the reports it took.
    dropped: str = ""


class ExitReportCounts:
    """The reports a worker took since it started, by connector and verdict, and those it dropped, by reason and verdict;
    never reset, as Prometheus expects."""

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str]] = Counter()
        # Each connector's name as last seen, so a rename relabels its series instead of splitting them here.
        self._names: dict[str, str] = {}
        # By reason, never by connector: a dropped report's connector may be any text a client sent.
        self._dropped: Counter[tuple[str, str]] = Counter()

    def add(self, connector_id: str, connector_name: str, verdict: ExitVerdict) -> None:
        """Count one report of ``verdict`` taken for the connector ("" for none)."""
        self._names[connector_id] = connector_name
        self._counts[(connector_id, verdict.value)] += 1

    def drop(self, reason: DropReason, verdict: ExitVerdict) -> None:
        """Count one report of ``verdict`` dropped for ``reason``."""
        self._dropped[(reason.value, verdict.value)] += 1

    def dropped_by_reason(self) -> dict[str, int]:
        """The reports dropped since start by reason, whatever their verdict: a few numbers, cheap enough for the
        heartbeat."""
        by_reason: Counter[str] = Counter()
        for (reason, _verdict), count in self._dropped.items():
            by_reason[reason] += count
        return dict(sorted(by_reason.items()))

    def snapshot(self) -> list[ExitReportCount]:
        """Every count as it stands, those taken by connector and verdict, then those dropped by reason and verdict; built
        for a scrape, never for the heartbeat."""
        taken = [
            ExitReportCount(connector_id, self._names[connector_id], verdict, count)
            for (connector_id, verdict), count in sorted(self._counts.items())
        ]
        dropped = [
            ExitReportCount("", "", verdict, count, dropped=reason)
            for (reason, verdict), count in sorted(self._dropped.items())
        ]
        return taken + dropped


class _Checks(NamedTuple):
    """What a batch's reports are checked against: its projects that record and their connectors it names, and what the
    rows say of the connectors the exit headers named."""

    recording: Recording
    # The connectors the exit headers named that serve through a dynamic gateway.
    dynamic: frozenset[str] = frozenset()
    # (connector, exit IP as canonical text) of the rows last seen exiting from an IP the exit headers named.
    seen: frozenset[tuple[str, str]] = frozenset()

    def counted(self, report: ExitReportMessage) -> ExitReportMessage | DropReason:
        """What ``report`` counts for: the exit its headers named, checked against the project's rows, else the exit its
        tag's note named when one came with it; else why it is dropped, the headers' reason first."""
        if report.project_id not in self.recording.projects:
            return DropReason.PROJECT
        if not report.by_headers:
            return self._noted(report)
        outcome = self._headed(report)
        if isinstance(outcome, DropReason) and (by_note := report.by_note()) is not None:
            fallback = self._noted(by_note)
            return outcome if isinstance(fallback, DropReason) else fallback
        return outcome

    def _noted(self, report: ExitReportMessage) -> ExitReportMessage | DropReason:
        """A report a tag's note named: its exit IP counts whatever became of its connector since."""
        owned = (report.project_id, report.connector_id) in self.recording.connectors
        return report if owned or report.ip is not None else DropReason.CONNECTOR

    def _headed(self, report: ExitReportMessage) -> ExitReportMessage | DropReason:
        """A report its exit headers named, checked against the project's rows."""
        if (report.project_id, report.connector_id) not in self.recording.connectors:
            return DropReason.CONNECTOR
        if report.connector_id in self.dynamic:
            # Its exits are per request: the IP it went out through is no record of its own, and its records count at
            # every level of the place asked for.
            return report.model_copy(update={"ip": None})
        if report.ip is None:
            return DropReason.IP_NEEDED
        if (report.connector_id, report.ip) not in self.seen:
            return DropReason.IP_NOT_SEEN
        # Its rows already depend on the place asked for: its exit IP's record, and its connector's at no place.
        return report.model_copy(update={"place": ""})


class ExitReportIngest(GroupConsumer):
    """Checks and applies the exit reports the api appended on the workers, a batch at a time: to the exit IP records in
    one transaction, and to the site windows the metrics flush applies to the connector records; it signals the
    controller after the commit when routing moved.

    A report is dropped, counted by reason, when its project is gone or records nothing (also when its exit reputation
    went off since the report was taken), when the connector it alone counts for is gone or another project's, and when
    the exit headers that named it name no exit of the project's rows.
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
        dropped: list[tuple[ExitReportMessage, DropReason]] = []
        async with self._session_factory() as session:
            checks = await self._checks(session, reports)
            counted: list[ExitReportMessage] = []
            for report in reports:
                outcome = checks.counted(report)
                if isinstance(outcome, DropReason):
                    dropped.append((report, outcome))
                else:
                    counted.append(outcome)
            ips: dict[ExitIpKey, ReportCounts] = ReportCounts.fold(
                (key, report.counts) for report in counted if (key := report.ip_key) is not None
            )
            changes = await ExitReputationRepository(session).apply_reports(ips, utc_now())
            ip_keys = {change.key for change in changes.ips}
            connectors: dict[ConnectorSiteKey, ReportCounts] = ReportCounts.fold(
                (window, report.counts)
                for report in counted
                if (window := report.connector_key) is not None
                and (window.project_id, window.connector_id) in checks.recording.connectors
            )
            applied: list[ExitReportMessage] = []
            for report in counted:
                if report.ip_key in ip_keys or report.connector_key in connectors:
                    applied.append(report)
                else:
                    # Its exit IP alone counted it, and its project's exit reputation went off since the check.
                    dropped.append((report, DropReason.PROJECT))
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
        self._count_dropped(dropped)
        logger.debug(
            "Exit reports applied",
            count=len(applied),
            dropped=len(dropped),
            ip_records=len(changes.ips),
            connector_windows=len(connectors),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    async def _checks(session: AsyncSession, reports: Sequence[ExitReportMessage]) -> _Checks:
        """What the batch's reports are checked against: one statement for its projects and one for the connectors they
        name; for the connectors of the project the exit headers named, one for which are dynamic and one for the exit
        IPs named."""
        recording = await ExitReputationRepository(session).recording_connectors(
            {(report.project_id, report.connector_id) for report in reports}
            | {(report.project_id, report.noted.connector_id) for report in reports if report.noted is not None}
        )
        headers = [
            report
            for report in reports
            if report.by_headers and (report.project_id, report.connector_id) in recording.connectors
        ]
        named = {report.connector_id for report in headers if report.connector_id}
        if not named:
            return _Checks(recording)
        proxies = ProxyRepository(session)
        dynamic = await proxies.dynamic_connectors(named)
        with_ips = named - dynamic
        spellings = {
            text
            for report in headers
            if report.connector_id in with_ips and report.ip
            for text in _spellings(report.ip)
        }
        values = await proxies.metadata_values(with_ips, META_DISCOVERED_IP, among=spellings)
        seen = {
            (connector_id, ip)
            for connector_id, texts in values.items()
            for text in texts
            if (ip := canonical_ip(text)) is not None
        }
        return _Checks(recording, frozenset(dynamic), frozenset(seen))

    def _count_dropped(self, dropped: Sequence[tuple[ExitReportMessage, DropReason]]) -> None:
        """Count the batch's dropped reports, and log them once, by reason."""
        if not dropped:
            return
        for report, reason in dropped:
            self.counts.drop(reason, report.verdict)
        reasons = Counter(reason.value for _report, reason in dropped)
        logger.warning("Exit reports dropped", count=len(dropped), reasons=dict(sorted(reasons.items())))

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


def _spellings(ip: str) -> tuple[str, ...]:
    """The texts a row's stored exit IP may hold for the canonical ``ip``: itself, and an IPv4's mapped IPv6 form, which
    some echoes answer with."""
    return (ip, f"::ffff:{ip}") if "." in ip else (ip,)
