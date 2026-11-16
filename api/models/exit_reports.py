# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports: what a client tells Octoprox about a request it sent, and what became of each report.

A report names the request's exit by the exit headers its response carried, which the workers check against the
project's rows, or by the ``-tag-`` in the proxy username it used, which the gateway noted the exit under; see
docs/exit-reputation.md (Reporting exits).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from api.reputation.record import MAX_REASON_LENGTH

# The most reports one request may carry.
MAX_EXIT_REPORTS = 1000


class ExitVerdict(StrEnum):
    """What a client says an exit did on a site."""

    GOOD = "good"
    BAD = "bad"


class DropReason(StrEnum):
    """Why the workers dropped a report the api took without applying it to any record: the ``reason`` of
    ``octoprox_worker_exit_reports_dropped_total``."""

    # Its project is gone, or its exit reputation is off.
    PROJECT = "project"
    # The connector it counts for is gone, or not the project's.
    CONNECTOR = "connector"
    # Its exit headers name a connector with IPs without the exit IP.
    IP_NEEDED = "ip_needed"
    # Its exit headers name an exit IP no row of the connector was last seen exiting from.
    IP_NOT_SEEN = "ip_not_seen"
    # Delivered too many times without being applied: the reports stream's consumer gives it up.
    GIVEN_UP = "given_up"


class ExitReportIn(BaseModel):
    """One exit report: the request, named by the proxy username it used, its exit, the site and the verdict.

    The exit is named by the exit headers when ``connector_id`` is sent (alone for a dynamic connector, with ``ip`` for a
    connector with IPs), which the workers check, falling back on the username's ``-tag-`` when they name no exit; else
    by the ``-tag-`` alone.
    """

    username: str = Field(
        description="The proxy username the request used: the project it is for, the place its -cc-, -st- and -city- "
        "name, and its -tag-, which names the exit when the exit headers do not"
    )
    site: str = Field(
        description="The host the request went to, or its URL; the report counts for that exact host alone"
    )
    verdict: ExitVerdict
    reason: str | None = Field(
        default=None,
        max_length=MAX_REASON_LENGTH,
        # No control character: Postgres refuses a NUL in text, which would fail the workers' whole batch of reports.
        pattern=r"^[^\x00-\x1f\x7f]*$",
        description="Why, in a few words, such as captcha or 403",
    )
    connector_id: str | None = Field(
        default=None,
        # A connector id's length: anything longer names no connector, and would reach the reports stream as sent.
        max_length=36,
        description="The X-Octoprox-Connector-Id exit header of the request's response: alone, it names the exit of a "
        "dynamic connector; with ip, that of a connector with IPs. Sent, it names the exit; when the workers find it "
        "names no exit of the project, the username's -tag- does if a gateway noted it, else the report is dropped",
    )
    ip: str | None = Field(
        default=None,
        # The longest text of an IP address.
        max_length=45,
        description="The X-Octoprox-Exit-Ip exit header of the request's response, sent with connector_id: for a "
        "connector with IPs, the exit IP one of its rows was last seen exiting from",
    )


class ExitReportsRequest(BaseModel):
    """A batch of exit reports, each judged on its own."""

    reports: list[ExitReportIn] = Field(min_length=1, max_length=MAX_EXIT_REPORTS)


class ExitReportResult(BaseModel):
    """What became of one report: taken with what it names (the exit IP, the connector and the place level its evidence
    starts from), or refused and why. The workers check a report named by its exit headers, and may yet drop it."""

    index: int = Field(description="The report's position in the request, from 0")
    status: Literal["accepted", "rejected"]
    ip: str | None = None
    site: str | None = None
    connector_id: str | None = None
    place: str | None = Field(
        default=None,
        description="The requested place the connector's evidence is kept at, and at each coarser level; empty for none "
        "and for a connector with IPs, which the workers keep at none when its exit headers named it",
    )
    error: str | None = None


class ExitReportsResponse(BaseModel):
    """How many reports were taken and refused, and each one's result in the order they came."""

    accepted: int
    rejected: int
    results: list[ExitReportResult]
