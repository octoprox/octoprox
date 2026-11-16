# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Messages that cross Redis work streams from one role to another: a gateway's sightings, for the workers' attribution,
and the exit reports the api took, for the workers' exit reputation.

Preflight may not wait for Postgres, so it appends one :class:`SightingMessage` per sighting to the sightings stream; the
api takes a report without Postgres, so it appends one :class:`ExitReportMessage` per report it took, for the workers to
check and apply.
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


class NotedExit(BaseModel):
    """The exit a gateway noted for a report's tag, carried with a report its exit headers named: what the workers count
    it for when the headers name no exit of the project's rows."""

    connector_id: str
    proxy_id: str
    # The exit IP of a row with IPs, as its canonical text; None for a dynamic gateway's exit.
    ip: str | None = None


class ExitReportMessage(BaseModel):
    """One exit report the api took, appended for the workers to apply to the project's records.

    It counts for the exit IP's record when it names an IP of a row with IPs (``ip``), and for the connector's at every
    level of ``place`` when it names a connector (``connector_id``). The tag's note names what it counts for; the exit
    headers name it as sent, which the workers check against the project's rows first (:attr:`by_headers`), falling
    back on the tag's note when one came with it (:attr:`noted`).
    """

    project_id: str
    # The site the report counts for: the exact host or an IP literal.
    site: str
    verdict: ExitVerdict
    # The exit IP as its canonical text: a row's with IPs, from the tag's note, or the exit IP header as sent; None for
    # neither (a dynamic gateway's exit).
    ip: str | None = None
    # The connector that served the exit, as the exit headers or the tag's note name it.
    connector_id: str | None = None
    # The requested place the connector's evidence is kept at (a LocationTarget key), "" for none and for a connector
    # with IPs, whose rows already depend on the place asked for; the username's place when the exit headers named it.
    place: str = ""
    # The proxy row the tag's note names; None for a report named by its exit headers.
    proxy_id: str | None = None
    # The exit the tag's note names, for a report its exit headers named: what it counts for when they name none.
    noted: NotedExit | None = None
    reason: str | None = None
    # When the api took it (naive UTC), and who sent it with which API token (None for a signed-in session).
    reported_at: datetime
    reporter_id: str | None = None
    token_id: str | None = None

    @property
    def by_headers(self) -> bool:
        """Whether the exit headers named its exit, as sent: no tag's note named a proxy row for it."""
        return self.proxy_id is None

    def by_note(self) -> ExitReportMessage | None:
        """The report named by its tag's note instead, as the api names a report without exit headers; None without a
        note. A row with IPs counts it at no place, since its rows already depend on the place asked for."""
        noted = self.noted
        if noted is None:
            return None
        return self.model_copy(
            update={
                "connector_id": noted.connector_id,
                "proxy_id": noted.proxy_id,
                "ip": noted.ip,
                "place": "" if noted.ip is not None else self.place,
                "noted": None,
            }
        )

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
