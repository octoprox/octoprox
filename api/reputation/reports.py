# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports as the api takes them, without Postgres: each named on its own by what it carries, and the ones named
appended to the reports stream, where the workers check them and decide what they do to the records.

A report names its request by the proxy username it used, and the request's exit in one of two ways: the exit headers
the response carried (a connector's id, with the exit IP for a connector with IPs), which the workers check against the
project's rows, else the ``-tag-`` in the username, by the note a gateway made of the tagged request. A report its exit
headers named carries its tag's note too, which the workers fall back on when the headers name no exit of the rows. A
row with IPs counts the report for its exit IP and its connector; a dynamic connector, whose exits are per request,
alone, at the place the username names; see docs/exit-reputation.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.logging import error_text
from api.core.username_params import parse_username_params
from api.db.redis import EXIT_REPORT_STREAM_KEY, EXIT_REPORT_STREAM_MAXLEN, TAG_NOTE_TTL_SECONDS
from api.jobs.messages import ExitReportMessage, NotedExit
from api.models.exit_reports import ExitReportIn, ExitReportResult, ExitReportsResponse
from api.reputation.site import canonical_ip, report_site
from api.reputation.tag_note import TagNote

if TYPE_CHECKING:
    from datetime import datetime

    from api.db.redis import RedisClient
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
# Why exit headers cannot name an exit, whatever the project's rows say: the report falls back on its tag's note, and
# is refused with this when it has none. A connector id is printable ASCII; anything else would reach the reports stream
# and the workers' Postgres statements as sent.
IP_NEEDS_CONNECTOR = "ip needs connector_id"
NOT_A_CONNECTOR = "connector_id is not a connector id"
NOT_AN_IP = "ip is not an IP"


class ExitReportsUnavailableError(RuntimeError):
    """Redis did not answer within the request deadline, so the reports could not be named or appended (503); the client
    sends them again."""


@dataclass(slots=True)
class _Report:
    """One report on its way through the intake: what it names, or why it was refused."""

    index: int
    report: ExitReportIn
    site: str | None = None
    # The tag the username carries, and the place it names (a LocationTarget key, "" for none).
    tag: str | None = None
    place: str = ""
    ip: str | None = None
    connector_id: str | None = None
    # The proxy row the tag's note names; None for a report named by its exit headers.
    proxy_id: str | None = None
    # Why the exit headers sent could not name the exit; the tag's note names it then, if there is one.
    header_error: str | None = None
    # The exit the tag's note names for a report its exit headers named: what the workers fall back on.
    noted: NotedExit | None = None
    error: str | None = None

    @property
    def refused(self) -> bool:
        return self.error is not None

    @property
    def wants_note(self) -> bool:
        """Whether its tag's note is to be read: it is not refused and carries a tag."""
        return not self.refused and self.tag is not None

    @property
    def needs_note(self) -> bool:
        """Whether its tag's note is all that can name its exit: no exit header named it."""
        return self.wants_note and self.connector_id is None

    def name(self, project: Project) -> None:
        """Read the site, the username's tag and place, and the exit headers, refusing what the report shows wrong."""
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
            self._name_by_headers(_given(self.report.connector_id), _given(self.report.ip))

    def _name_by_headers(self, connector_id: str | None, ip: str | None) -> None:
        """Take the exit the headers name, as sent, when ``connector_id`` is: the workers check it against the project's
        rows. An ``ip`` without a connector or that is no IP, or a connector id that is not printable ASCII, names none,
        and the tag's note is left to."""
        if connector_id is None:
            if ip is not None:
                self.header_error = IP_NEEDS_CONNECTOR
            return
        if not _printable_ascii(connector_id):
            self.header_error = NOT_A_CONNECTOR
            return
        canonical = canonical_ip(ip) if ip is not None else None
        if ip is not None and canonical is None:
            self.header_error = f"not an IP: {ip}" if _printable_ascii(ip) else NOT_AN_IP
            return
        self.connector_id, self.ip = connector_id, canonical

    def resolve(self, notes: Mapping[str, TagNote | None]) -> None:
        """Name the exit its exit headers did not from its tag's note, and give one they did the note to fall back on. A
        note of a row with IPs gives its exit IP and its connector, at no place, since its rows already depend on the
        place asked for; one of a dynamic gateway its connector, at the place."""
        if self.refused:
            return
        note = notes.get(self.tag) if self.tag is not None else None
        if self.connector_id is not None:
            if note is not None:
                self.noted = NotedExit(
                    connector_id=note.connector_id,
                    proxy_id=note.proxy_id,
                    ip=canonical_ip(note.ip) if note.ip else None,
                )
            return
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
        """What the workers are handed for this report, once it is named."""
        assert self.site is not None
        return ExitReportMessage(
            project_id=project_id,
            site=self.site,
            verdict=self.report.verdict,
            ip=self.ip,
            connector_id=self.connector_id,
            place=self.place,
            proxy_id=self.proxy_id,
            noted=self.noted,
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
    """Takes a project's exit reports without Postgres: names each by its exit headers as sent, with its tag's note to
    fall back on, or by that note alone, and appends the ones named to the reports stream in one pipeline.

    It decides nothing about the exits or connectors: the workers check the exit headers against the project's rows,
    and apply each report against the project's settings.
    """

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    async def take(
        self,
        project: Project,
        reports: Sequence[ExitReportIn],
        *,
        reporter_id: str | None,
        token_id: str | None,
    ) -> ExitReportsResponse:
        """Name each report on its own and append the ones named; what became of each, in order.

        Raises:
            ExitReportsUnavailableError: Redis did not answer within the request deadline.
        """
        pending = [_Report(index, report) for index, report in enumerate(reports)]
        for report in pending:
            report.name(project)
        notes = await self._notes(
            project.id,
            (report.tag for report in pending if report.wants_note and report.tag),
            needed=any(report.needs_note for report in pending),
        )
        for report in pending:
            report.resolve(notes)
        reported_at = utc_now()
        messages = [
            report.message(project.id, reported_at, reporter_id, token_id) for report in pending if not report.refused
        ]
        await self._append(messages)
        logger.debug(
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

    async def _notes(self, project_id: str, tags: Iterable[str], *, needed: bool) -> dict[str, TagNote | None]:
        """The note of each tag, one MGET; None for a tag no gateway noted lately, or one unreadable.

        Notes that cannot be read fail the call only when a report has nothing else to name its exit (``needed``);
        reports its exit headers named go without the note to fall back on.
        """
        wanted = sorted(set(tags))
        if not wanted:
            return {}
        try:
            async with self._redis.request_deadline():
                values = await self._redis.client.mget([TagNote.key(project_id, tag) for tag in wanted])
        except Exception as exc:
            logger.warning("Tag notes could not be read", error=error_text(exc))
            if needed:
                raise ExitReportsUnavailableError("tag notes could not be read") from exc
            return {}
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


def _printable_ascii(value: str) -> bool:
    """Whether ``value`` is printable ASCII: what a connector id and an IP's text are, and what is safe to echo back."""
    return value.isascii() and value.isprintable()


def _note(raw: str | bytes | None) -> TagNote | None:
    """A stored note, None when missing or unreadable (written by another release)."""
    if not raw:
        return None
    try:
        return TagNote.model_validate_json(raw)
    except ValidationError:
        return None
