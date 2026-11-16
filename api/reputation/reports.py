# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports as the api takes them: each resolved on its own to what it counts for, and the ones resolved appended
to the reports stream, where the workers decide what they do to the records.

A report names its request by the proxy username it used, and the request's exit in one of two ways: the exit headers
the response carried, when they are complete (a dynamic connector's id alone, or a connector with IPs and the exit IP
of one of its rows), else the ``-tag-`` in the username, by the note a gateway made of the tagged request. A row with
IPs counts the report for its exit IP and its connector; a dynamic connector, whose exits are per request, alone, at the
place the username names; see docs/exit-reputation.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.logging import error_text
from api.core.username_params import parse_username_params
from api.db.redis import EXIT_REPORT_STREAM_KEY, EXIT_REPORT_STREAM_MAXLEN, TAG_NOTE_TTL_SECONDS
from api.db.repository import ConnectorRepository, ProxyRepository
from api.jobs.messages import ExitReportMessage
from api.models.exit_reports import ExitReportIn, ExitReportResult, ExitReportsResponse
from api.providers.sdk.strategies import META_DISCOVERED_IP
from api.reputation.site import canonical_ip, report_site
from api.reputation.tag_note import TagNote

if TYPE_CHECKING:
    from datetime import datetime

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.models.project import Project

logger = structlog.get_logger()

# What a report naming a tag no gateway noted lately is told. A note is written with TAG_NOTE_TTL_SECONDS, and again
# once half of that has passed while the tag is in use, so it outlives the tag's last request by half to all of it.
NO_NOTE = (
    f"no recent request with this tag (a note is kept {TAG_NOTE_TTL_SECONDS / 120:g} to "
    f"{TAG_NOTE_TTL_SECONDS / 60:g} minutes after the tag's last request)"
)
# What a report that names its exit neither way is told.
NO_EXIT = "name the exit with a -tag- in the username, or with the exit headers"


class ExitReportsUnavailableError(RuntimeError):
    """Redis did not answer within the request deadline, so the reports could not be resolved or appended (503); the
    client sends them again."""


class _Exits(NamedTuple):
    """What the project's rows say of the connectors that reports name by their exit headers."""

    # The project's connectors among those named, and those of them serving through dynamic gateways.
    connectors: frozenset[str] = frozenset()
    dynamic: frozenset[str] = frozenset()
    # (connector, exit IP as canonical text) of the rows last seen exiting from an IP a report named.
    ips: frozenset[tuple[str, str]] = frozenset()


@dataclass(slots=True)
class _Report:
    """One report on its way through the intake: what it resolved to, or why it was refused."""

    index: int
    report: ExitReportIn
    site: str | None = None
    # The tag the username carries, and the place it names (a LocationTarget key, "" for none).
    tag: str | None = None
    place: str = ""
    # The exit headers sent, as given; ``by_headers`` once they named the exit, else why they did not.
    header_connector: str | None = None
    header_ip: str | None = None
    by_headers: bool = False
    header_error: str | None = None
    ip: str | None = None
    connector_id: str | None = None
    proxy_id: str | None = None
    error: str | None = None

    @property
    def refused(self) -> bool:
        return self.error is not None

    @property
    def needs_note(self) -> bool:
        """Whether its tag's note is to be read: it is not refused, and its exit headers did not name the exit."""
        return not self.refused and not self.by_headers and self.tag is not None

    def name(self, project: Project) -> None:
        """Read the site, the username's tag and place, and the exit headers, refusing what is wrong without a lookup."""
        try:
            self.site = report_site(self.report.site)
        except ValueError as exc:
            self.error = str(exc)
            return
        params = parse_username_params(self.report.username.strip())
        if params.username != project.username:
            self.error = "username is not this project's"
        elif params.location_error:
            self.error = f"username: {params.location_error}"
        else:
            self.tag = params.tag
            self.place = params.location.key if params.location else ""
            self.header_connector = _given(self.report.connector_id)
            self.header_ip = _given(self.report.ip)

    def ip_spellings(self) -> set[str]:
        """The texts a row's stored exit IP may hold for the exit IP header sent: as sent, its canonical text, and an
        IPv4's mapped IPv6 form, which some echoes answer with."""
        if self.header_ip is None or (ip := canonical_ip(self.header_ip)) is None:
            return set()
        spellings = {self.header_ip, ip}
        if "." in ip:
            spellings.add(f"::ffff:{ip}")
        return spellings

    def name_by_headers(self, exits: _Exits) -> None:
        """Name the exit by the exit headers sent when they are complete: a dynamic connector's id alone, or a connector
        with IPs and the exit IP one of its rows was last seen exiting from; otherwise note why they are not."""
        if self.refused or (self.header_connector is None and self.header_ip is None):
            return
        connector_id = self.header_connector
        if connector_id is None:
            self.header_error = "ip needs connector_id"
        elif connector_id not in exits.connectors:
            self.header_error = f"connector {connector_id} is not in this project"
        elif connector_id in exits.dynamic:
            # Its exits are per request: the connector alone counts the report, at the place the username names.
            self.connector_id, self.by_headers = connector_id, True
        elif self.header_ip is None:
            self.header_error = f"connector {connector_id} has IPs, so ip is needed too"
        elif (ip := canonical_ip(self.header_ip)) is None:
            self.header_error = f"not an IP: {self.header_ip}"
        elif (connector_id, ip) not in exits.ips:
            self.header_error = f"no proxy of connector {connector_id} was last seen exiting from {ip}"
        else:
            # Its rows already depend on the place asked for, so its evidence is kept at none.
            self.connector_id, self.ip, self.place, self.by_headers = connector_id, ip, "", True

    def resolve(self, notes: Mapping[str, TagNote | None]) -> None:
        """Finish what the report counts for: by the exit headers when they named the exit, else from its tag's note. A
        note of a row with IPs gives its exit IP and its connector, at no place, since its rows already depend on the
        place asked for; one of a dynamic gateway its connector, at the place."""
        if self.refused or self.by_headers:
            return
        note = notes.get(self.tag) if self.tag is not None else None
        if note is None:
            reason = NO_EXIT if self.tag is None else NO_NOTE
            self.error = f"{reason}; exit headers: {self.header_error}" if self.header_error else reason
            return
        self.connector_id, self.proxy_id = note.connector_id, note.proxy_id
        self.ip = canonical_ip(note.ip) if note.ip else None
        if self.ip is not None:
            self.place = ""

    def message(
        self, project_id: str, reported_at: datetime, reporter_id: str | None, token_id: str | None
    ) -> ExitReportMessage:
        """What the workers are handed for this report, once it resolved."""
        assert self.site is not None
        return ExitReportMessage(
            project_id=project_id,
            site=self.site,
            verdict=self.report.verdict,
            ip=self.ip,
            connector_id=self.connector_id,
            place=self.place,
            proxy_id=self.proxy_id,
            reason=_given(self.report.reason),
            reported_at=reported_at,
            reporter_id=reporter_id,
            token_id=token_id,
        )

    def result(self) -> ExitReportResult:
        """What the client is told about this report."""
        if self.refused:
            return ExitReportResult(index=self.index, status="rejected", site=self.site, error=self.error)
        return ExitReportResult(
            index=self.index,
            status="accepted",
            ip=self.ip,
            site=self.site,
            connector_id=self.connector_id,
            place=self.place,
        )


class ExitReportIntake:
    """Takes a project's exit reports: resolves each by its exit headers against the project's rows in Postgres, or by
    the note gateways made of its tagged request in Redis, and appends the ones resolved to the reports stream in one
    pipeline.

    It decides nothing about the exits or connectors: the workers do, against the project's settings, when they apply
    them.
    """

    def __init__(self, session_factory: SessionFactory, redis_client: RedisClient) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    async def take(
        self,
        project: Project,
        reports: Sequence[ExitReportIn],
        *,
        reporter_id: str | None,
        token_id: str | None,
    ) -> ExitReportsResponse:
        """Resolve each report on its own and append the ones resolved; what became of each, in order.

        Raises:
            ExitReportsUnavailableError: Redis did not answer within the request deadline.
        """
        pending = [_Report(index, report) for index, report in enumerate(reports)]
        for report in pending:
            report.name(project)
        exits = await self._exits(project.id, pending)
        for report in pending:
            report.name_by_headers(exits)
        notes = await self._notes(project.id, (report.tag for report in pending if report.needs_note and report.tag))
        for report in pending:
            report.resolve(notes)
        reported_at = utc_now()
        messages = [
            report.message(project.id, reported_at, reporter_id, token_id) for report in pending if not report.refused
        ]
        await self._append(messages)
        logger.info(
            "Exit reports taken",
            project_id=project.id,
            accepted=len(messages),
            rejected=len(pending) - len(messages),
        )
        return ExitReportsResponse(
            accepted=len(messages),
            rejected=len(pending) - len(messages),
            results=[report.result() for report in pending],
        )

    async def _exits(self, project_id: str, pending: Sequence[_Report]) -> _Exits:
        """What the project's rows say of the connectors the reports name by their exit headers: a statement for the
        connectors, one for which serve through dynamic gateways, and one for the exit IPs named, none without
        headers."""
        named = [report for report in pending if not report.refused and report.header_connector]
        # An id with a control character names no row, and Postgres refuses a NUL in text: such a report's headers are
        # left to name a connector that is not in the project.
        wanted = {
            report.header_connector for report in named if report.header_connector and report.header_connector.isprintable()
        }
        if not wanted:
            return _Exits()
        async with self._session_factory() as session:
            connectors = await ConnectorRepository(session).get_many(wanted)
            mine = {connector.id for connector in connectors if connector.project_id == project_id}
            proxies = ProxyRepository(session)
            dynamic = await proxies.dynamic_connectors(mine)
            with_ips = mine - dynamic
            spellings = {text for report in named if report.header_connector in with_ips for text in report.ip_spellings()}
            seen = await proxies.metadata_values(with_ips, META_DISCOVERED_IP, among=spellings)
        ips = {
            (connector_id, ip)
            for connector_id, values in seen.items()
            for value in values
            if (ip := canonical_ip(value)) is not None
        }
        return _Exits(frozenset(mine), frozenset(dynamic), frozenset(ips))

    async def _notes(self, project_id: str, tags: Iterable[str]) -> dict[str, TagNote | None]:
        """The note of each tag, one MGET; None for a tag no gateway noted lately, or one unreadable."""
        wanted = sorted(set(tags))
        if not wanted:
            return {}
        try:
            async with self._redis.request_deadline():
                values = await self._redis.client.mget([TagNote.key(project_id, tag) for tag in wanted])
        except Exception as exc:
            logger.warning("Tag notes could not be read", error=error_text(exc))
            raise ExitReportsUnavailableError("tag notes could not be read") from exc
        return {tag: _note(value) for tag, value in zip(wanted, values, strict=True)}

    async def _append(self, messages: Sequence[ExitReportMessage]) -> None:
        """Append each message as an entry of the reports stream, one pipeline under the request deadline."""
        if not messages:
            return
        try:
            async with self._redis.request_deadline():
                pipe = self._redis.client.pipeline(transaction=False)
                for message in messages:
                    pipe.xadd(
                        EXIT_REPORT_STREAM_KEY,
                        {"item": message.model_dump_json()},
                        maxlen=EXIT_REPORT_STREAM_MAXLEN,
                        approximate=True,
                    )
                await pipe.execute()
        except Exception as exc:
            logger.warning("Exit reports could not be appended", count=len(messages), error=error_text(exc))
            raise ExitReportsUnavailableError("exit reports could not be appended") from exc


def _given(value: str | None) -> str | None:
    """A field as given, None when left out or blank."""
    if value is None:
        return None
    return value.strip() or None


def _note(raw: str | bytes | None) -> TagNote | None:
    """A stored note, None when missing or unreadable (written by another release)."""
    if not raw:
        return None
    try:
        return TagNote.model_validate_json(raw)
    except ValidationError:
        return None
