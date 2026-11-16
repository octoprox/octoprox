# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports: what a client tells Octoprox about a request it sent, and what became of each report.

A report names the request's exit by the exit headers its response carried, or by the ``-tag-`` in the proxy username
it used, which the gateway noted the exit under; see docs/exit-reputation.md (Reporting exits).
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


class ExitReportIn(BaseModel):
    """One exit report: the request, named by the proxy username it used, its exit, the site and the verdict.

    The exit is named by the exit headers when they are complete (``connector_id`` alone for a dynamic connector, with
    ``ip`` for a connector with IPs), else by the username's ``-tag-``.
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
        description="The X-Octoprox-Connector-Id exit header of the request's response: alone, it names the exit of a "
        "dynamic connector; with ip, that of a connector with IPs",
    )
    ip: str | None = Field(
        default=None,
        description="The X-Octoprox-Exit-Ip exit header of the request's response, for a connector with IPs: the exit "
        "IP one of its rows was last seen exiting from",
    )


class ExitReportsRequest(BaseModel):
    """A batch of exit reports, each judged on its own."""

    reports: list[ExitReportIn] = Field(min_length=1, max_length=MAX_EXIT_REPORTS)


class ExitReportResult(BaseModel):
    """What became of one report: taken with what it counts for (the exit IP, the connector and the place level its
    evidence starts from), or refused and why."""

    index: int = Field(description="The report's position in the request, from 0")
    status: Literal["accepted", "rejected"]
    ip: str | None = None
    site: str | None = None
    connector_id: str | None = None
    place: str | None = Field(
        default=None,
        description="The requested place the connector's evidence is kept at, and at each coarser level; empty for none "
        "and for a connector with IPs",
    )
    error: str | None = None


class ExitReportsResponse(BaseModel):
    """How many reports were taken and refused, and each one's result in the order they came."""

    accepted: int
    rejected: int
    results: list[ExitReportResult]
