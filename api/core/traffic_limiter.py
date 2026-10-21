# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-connector traffic limits: each connector's usage now, the rules it is judged by, and the meter.

Usage is the bytes a connector's proxies carried both ways this period; the traffic job decides blocks from it, and the
api shows it beside the block the job stored.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from api.db.redis import CONNECTOR_WINDOWS
from api.db.repository import MetricsRepository
from api.models.connector import (
    Connector,
    TrafficConfig,
    TrafficLimitAction,
    TrafficUsage,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.stats import MetricDelta
    from api.feed.state import RuntimeStateReader
    from api.gateway.metering import RequestMetering

# A running transfer reports its bytes once this much has accumulated or this
# long has passed since the last report, whichever comes first. A megabyte
# keeps the limit accurate to well under a vendor's billing resolution; the
# time bound keeps slow trickles visible.
PROGRESS_REPORT_BYTES = 1 << 20
PROGRESS_REPORT_SECONDS = 5.0


@dataclass(frozen=True)
class ConnectorTraffic:
    """A connector's traffic in its current usage window, both directions: its flushed history plus its live window."""

    connector: Connector
    config: TrafficConfig
    # The usage window: the period, cut at a manual reset inside it.
    start: datetime
    end: datetime
    sent: int = 0
    received: int = 0

    @classmethod
    def of(cls, connector: Connector, now: datetime) -> ConnectorTraffic:
        """The connector's usage window containing ``now``, with nothing counted in it yet."""
        config = connector.parsed_traffic_config
        start, end = config.usage_window(now, connector.traffic_reset_at)
        return cls(connector, config, start, end)

    @property
    def total(self) -> int:
        """The bytes both ways."""
        return self.sent + self.received

    @property
    def status(self) -> Literal["ok", "warning", "exceeded"]:
        """Where the total stands: under the warning line, past it, or at the limit; ``ok`` without a limit."""
        limit = self.config.limit_bytes
        if limit is None:
            return "ok"
        if self.total >= limit:
            return "exceeded"
        if self.total >= (self.config.warn_bytes or 0):
            return "warning"
        return "ok"

    def block_until(self) -> datetime | None:
        """The traffic job's rule: until when this total blocks the connector, its window's end; None while it does not.

        None with no limit, under the limit, or when the limit only alerts; only a rollover, a raised limit or a manual
        reset, which moves the window, lifts a block sooner.
        """
        limit = self.config.limit_bytes
        if limit is None or self.total < limit or self.config.action == TrafficLimitAction.ALERT:
            return None
        return self.end

    def usage(self, *, blocked: bool, pending: bool) -> TrafficUsage:
        """The report the api shows for this window; ``blocked`` and ``pending`` are the caller's, as Redis holds them.

        Args:
            blocked: The block the traffic job stored, which gateways enforce.
            pending: Whether a reset or a limit edit asked for a traffic decision no worker has stored yet.
        """
        config, total = self.config, self.total
        limit = config.limit_bytes
        reset_at = self.connector.traffic_reset_at
        return TrafficUsage(
            period=config.period,
            period_start=self.start,
            period_end=self.end,
            bytes_sent=self.sent,
            bytes_received=self.received,
            total_bytes=total,
            limit_bytes=limit,
            percent=round(total / limit * 100, 2) if limit else None,
            action=config.action,
            status=self.status,
            blocked=blocked,
            pending=pending,
            price_per_gb=config.price_per_gb,
            currency=config.currency,
            cost=config.cost_of(total),
            reset_at=reset_at if reset_at is not None and self.start <= reset_at < self.end else None,
        )


class TrafficHistory:
    """Connectors' usage windows now and the traffic flushed to Postgres in each, read in one query (:meth:`load`).

    :meth:`with_live` adds the windows Redis holds after: a flush in progress may be missed for a moment, never counted twice.
    """

    def __init__(self, flushed: dict[str, ConnectorTraffic]) -> None:
        # Each connector's traffic as Postgres holds it, by connector ID.
        self._flushed = flushed

    @classmethod
    async def load(cls, session: AsyncSession, connectors: Iterable[Connector], now: datetime) -> TrafficHistory:
        """Each connector's usage window at ``now`` and its history in that window, in one query of the caller's session."""
        windows = {connector.id: ConnectorTraffic.of(connector, now) for connector in connectors}
        if not windows:
            return cls({})
        rows = await MetricsRepository(session).get_connector_totals_since(
            {connector_id: window.start for connector_id, window in windows.items()}
        )
        return cls({
            connector_id: replace(window, sent=row["bytes_sent"], received=row["bytes_received"])
            if (row := rows.get(connector_id)) is not None
            else window
            for connector_id, window in windows.items()
        })

    async def with_live(self, state: RuntimeStateReader) -> dict[str, ConnectorTraffic]:
        """Each connector's traffic now: the history plus the window Redis holds, one pipelined read; raises for Redis."""
        if not self._flushed:
            return {}
        return self.plus(await state.windows(CONNECTOR_WINDOWS, list(self._flushed)))

    def plus(self, live: Mapping[str, MetricDelta]) -> dict[str, ConnectorTraffic]:
        """Each connector's traffic: the history plus ``live``, the windows Redis holds by connector ID (none: the history)."""
        return {
            connector_id: replace(
                flushed, sent=flushed.sent + window.bytes_sent, received=flushed.received + window.bytes_received
            )
            if (window := live.get(connector_id)) is not None
            else flushed
            for connector_id, flushed in self._flushed.items()
        }


class TrafficMeter:
    """Byte counter for one transfer through one proxy, reporting to the gateway's request metering as bytes flow.

    The transfer loops call ``add_sent`` and ``add_received`` per chunk and stop when either returns False.
    """

    __slots__ = (
        "_owner", "proxy_id", "project_id", "connector_id", "peer_id", "target_host",
        "sent", "received", "_reported_sent", "_reported_received", "_last_report", "_on_finish",
    )

    def __init__(
        self,
        owner: RequestMetering,
        proxy_id: str,
        project_id: str,
        connector_id: str,
        peer_id: str | None = None,
        target_host: str | None = None,
        *,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self._owner = owner
        self.proxy_id = proxy_id
        self.project_id = project_id
        self.connector_id = connector_id
        # The tunnel device the transfer came from, so its progress is
        # counted on the device as well; None for a proxy-port client.
        self.peer_id = peer_id
        # The destination the transfer is for, so its progress is counted
        # on the host as well (the Hosts page); None when not known.
        self.target_host = target_host
        self.sent = 0
        self.received = 0
        self._reported_sent = 0
        self._reported_received = 0
        self._last_report = time.monotonic()
        self._on_finish = on_finish

    @property
    def allowed(self) -> bool:
        """False once the connector is blocked with the interrupt action."""
        return not self._owner.is_interrupted(self.connector_id)

    @property
    def limit_status(self) -> int:
        """The HTTP status the connector's config reserves for the limit."""
        return self._owner.limit_status_for(self.connector_id)

    def add_sent(self, n: int) -> bool:
        """Count ``n`` bytes towards upstream; False when the transfer must stop."""
        self.sent += n
        return self._after_add()

    def add_received(self, n: int) -> bool:
        """Count ``n`` bytes from upstream; False when the transfer must stop."""
        self.received += n
        return self._after_add()

    def _after_add(self) -> bool:
        pending = (self.sent - self._reported_sent) + (self.received - self._reported_received)
        if pending >= PROGRESS_REPORT_BYTES or (
            pending > 0 and time.monotonic() - self._last_report >= PROGRESS_REPORT_SECONDS
        ):
            self.report()
        return self.allowed

    def report(self) -> None:
        """Hand everything not yet reported to the owner now."""
        sent = self.sent - self._reported_sent
        received = self.received - self._reported_received
        self._reported_sent = self.sent
        self._reported_received = self.received
        self._last_report = time.monotonic()
        if sent or received:
            self._owner.progress(
                self.proxy_id, self.project_id, self.connector_id, self.peer_id, sent, received,
                self.target_host,
            )

    def finish(self) -> tuple[int, int]:
        """Bytes not yet reported, for the completion event, so nothing is counted twice; ``on_finish`` runs once.

        Every meter is finished once its transfer is over, which ends its in-flight count; a second call ends nothing again.
        """
        sent = self.sent - self._reported_sent
        received = self.received - self._reported_received
        self._reported_sent = self.sent
        self._reported_received = self.received
        on_finish, self._on_finish = self._on_finish, None
        if on_finish is not None:
            on_finish()
        return sent, received

