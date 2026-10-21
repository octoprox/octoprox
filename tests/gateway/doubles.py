# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Test doubles of the gateway's classes."""

from __future__ import annotations

from typing import NamedTuple
from unittest.mock import MagicMock

from api.core.rate_limiter import RateLimiter
from api.core.traffic_limiter import TrafficMeter
from api.db.redis import RedisClient
from api.gateway.metering import RequestMetering
from api.models.connector import DEFAULT_TRAFFIC_LIMIT_STATUS
from api.models.proxy import Proxy
from api.providers.registry import ProviderRegistry
from api.routing.inflight import InFlight
from api.routing.outcomes import LocalOutcomes
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot

# A routing snapshot without connectors: what a meter counts lands on its project alone.
NO_CONNECTORS = RoutingSnapshot.from_sections({}, registry=ProviderRegistry())


class Progress(NamedTuple):
    """One report of a running transfer's bytes, as its meter makes it."""

    proxy_id: str
    project_id: str
    connector_id: str
    peer_id: str | None
    sent: int
    received: int
    target_host: str | None


class MeterRequest(NamedTuple):
    """One meter asked of metering, with what the transfer names."""

    proxy: Proxy
    project_id: str
    peer_id: str | None
    target_host: str | None


class QuietMetering(RequestMetering):
    """Request metering as a gateway runs it, minus Redis and connectors: it hands out meters and counts them in flight."""

    def __init__(self, inflight: InFlight | None = None) -> None:
        super().__init__(
            MagicMock(spec=RedisClient),
            inflight if inflight is not None else InFlight(),
            MagicMock(spec=RateLimiter),
            outcomes=LocalOutcomes(),
            router=Router(NO_CONNECTORS),
        )


class RecordingMetering(QuietMetering):
    """Quiet request metering that records the progress reported to it and the meters asked of it.

    The test sets its interrupt answer (``interrupted``) and its limit status (``status``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False
        self.status = DEFAULT_TRAFFIC_LIMIT_STATUS
        self.reports: list[Progress] = []
        self.meters: list[MeterRequest] = []

    def meter(
        self, proxy: Proxy, project_id: str, *, peer_id: str | None = None, target_host: str | None = None
    ) -> TrafficMeter:
        self.meters.append(MeterRequest(proxy, project_id, peer_id, target_host))
        return super().meter(proxy, project_id, peer_id=peer_id, target_host=target_host)

    def progress(
        self,
        proxy_id: str,
        project_id: str,
        connector_id: str,
        peer_id: str | None,
        sent: int,
        received: int,
        target_host: str | None = None,
    ) -> None:
        self.reports.append(Progress(proxy_id, project_id, connector_id, peer_id, sent, received, target_host))

    def is_interrupted(self, connector_id: str) -> bool:
        return self.interrupted

    def limit_status_for(self, connector_id: str) -> int:
        return self.status

    def reported(self) -> tuple[int, int]:
        """The bytes reported so far: ``(sent, received)``."""
        return sum(report.sent for report in self.reports), sum(report.received for report in self.reports)
