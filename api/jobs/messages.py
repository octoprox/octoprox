# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Messages that cross Redis work streams from one role to another: a gateway's sightings, for the workers' attribution.

Preflight may not wait for Postgres, so it appends one :class:`SightingMessage` per sighting to the sightings stream.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from api.geo.models import LocationCandidate, ObservationSource


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
    # The gateway that saw it, stamped on the observation.
    instance_id: str
