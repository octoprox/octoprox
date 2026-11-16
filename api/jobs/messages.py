# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Messages that cross Redis work streams from one role to another: a gateway's sightings, for the workers' attribution,
and the exit reports the api took, for the workers' exit reputation.

Preflight may not wait for Postgres, so it appends one :class:`SightingMessage` per sighting to the sightings stream; the
api decides nothing about a report, so it appends one :class:`ExitReportMessage` per report it took.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from api.geo.models import LocationCandidate, ObservationSource
from api.models.exit_reports import ExitVerdict
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts


class SightingMessage(BaseModel):
    """One sighting of a proxy's exit by a gateway, appended for the workers' attribution."""

    proxy_id: str
    ip: str
    source: ObservationSource
    # The exit's place as the echo endpoint reported it, if it reported one.
    endpoint_country: str | None = None
    endpoint_state: str | None = None
    endpoint_city: str | None = None
    # The client session the sighting came from.
    session_id: str | None = None
    # What the databases said about ``ip`` when the gateway looked it up: the
    # worker judges with the answers the request was judged with, and does not
    # look the IP up again. Empty when no database knew the IP.
    candidates: list[LocationCandidate]
    # When the gateway saw the exit (naive UTC). Stamped on the observation and
    # on the row, which is never written back to an older sighting.
    observed_at: datetime


class ExitReportMessage(BaseModel):
    """One exit report the api took, as it resolved it, appended for the workers to apply to the project's records.

    It counts for the exit IP's record when it names an IP of a row with IPs (``ip``), and for the connector's at every
    level of ``place`` when it names a connector (``connector_id``); at least one of the two is set.
    """

    project_id: str
    # The site the report counts for: the exact host or an IP literal.
    site: str
    verdict: ExitVerdict
    # The exit IP as its canonical text; None when the report named no IP of a row with IPs (a dynamic gateway's exit).
    ip: str | None = None
    # The connector that served the exit, as the exit headers or the tag's note name it.
    connector_id: str | None = None
    # The requested place the connector's evidence is kept at (a LocationTarget key), "" for none and for a connector
    # with IPs, whose rows already depend on the place asked for.
    place: str = ""
    # The proxy row the tag's note names; None for a report named by its exit headers. Kept for tracing.
    proxy_id: str | None = None
    reason: str | None = None
    # When the api took it (naive UTC), and who sent it with which API token (None for a signed-in session).
    reported_at: datetime
    reporter_id: str | None = None
    token_id: str | None = None

    @property
    def counts(self) -> ReportCounts:
        """What it adds to each record it counts for: one good or one bad report, with its reason."""
        good = self.verdict is ExitVerdict.GOOD
        return ReportCounts(good=int(good), bad=int(not good), reason=self.reason)

    @property
    def ip_key(self) -> ExitIpKey | None:
        """The exit IP record it counts for; None when it names no IP of a row with IPs."""
        return ExitIpKey(self.project_id, self.ip, self.site) if self.ip else None

    @property
    def connector_key(self) -> ConnectorSiteKey | None:
        """The connector record it counts for at the finest level of its place, whose site window takes it; None without
        a connector. The metrics flush counts it at every coarser level too."""
        if not self.connector_id:
            return None
        return ConnectorSiteKey(self.project_id, self.connector_id, self.site, self.place)
