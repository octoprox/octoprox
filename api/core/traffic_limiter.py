# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-connector traffic limits: usage this period, thresholds, and block state.

A connector's usage is the bytes its proxies carried in the current period
(see ``TrafficConfig``): both directions, summed. Every instance keeps that
number in memory so the selection path can ask "is this connector over its
limit" without I/O, the same way quarantine is mirrored from Redis.

Where the number comes from:

* ``refresh`` loads the period total from the connector_metrics history plus
  the Redis window the leader has not flushed yet (the ``loader`` callback,
  provided by ``ProxyManager``). It runs at startup and on every periodic
  full reload.
* Between refreshes the total moves with the metric deltas: this instance's
  own transfers land in ``unflushed`` as they happen (``record``) and move to
  ``known`` when the 5s flush puts them in Redis (``mark_flushed``); peers'
  deltas arrive on the metric pub/sub channel (``apply_peer``). Peers'
  unflushed bytes are invisible for at most one flush interval, which is the
  accepted error.

Bytes are metered while a transfer runs, not only when it ends: the
``TrafficMeter`` handed to each request reports progress every
``PROGRESS_REPORT_BYTES`` or ``PROGRESS_REPORT_SECONDS``, so a keep-alive
tunnel open for an hour counts against the limit as it goes and the
dashboard sees it move. The completion event carries only the remainder.

Reaching the limit applies the connector's action. ``alert`` only flags it.
``block`` raises a block: a Redis key (expiring with the period) plus the
``connector_traffic_changed`` signal so peers re-read it, and the connector
drops out of selection until the period rolls over, the limit is raised or
usage is reset. ``interrupt`` also makes every meter on the connector
report ``allowed == False``, and the transfer loops close their tunnel.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from api.core import utc_now
from api.core.event_bus import event_bus
from api.core.signals import connector_traffic_changed
from api.core.stats import MetricDelta
from api.db.redis import RedisClient
from api.models.connector import (
    Connector,
    TrafficConfig,
    TrafficLimitAction,
    TrafficPeriod,
    TrafficUsage,
)

logger = structlog.get_logger()

# A running transfer reports its bytes once this much has accumulated or this
# long has passed since the last report, whichever comes first. A megabyte
# keeps the limit accurate to well under a vendor's billing resolution; the
# time bound keeps slow trickles visible.
PROGRESS_REPORT_BYTES = 1 << 20
PROGRESS_REPORT_SECONDS = 5.0

# What ProxyManager gives the meter to fold progress into its pending deltas:
# (proxy_id, project_id, connector_id, bytes_sent, bytes_received).
ProgressSink = Callable[[str, str, str, int, int], None]
# Loads the flushed period totals: {connector_id: since} -> {connector_id: (sent, received)}.
TotalsLoader = Callable[[dict[str, datetime]], Awaitable[dict[str, tuple[int, int]]]]


def period_bounds(config: TrafficConfig, now: datetime) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the period containing ``now``, UTC."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if config.period == TrafficPeriod.DAY:
        return day, day + timedelta(days=1)
    if config.period == TrafficPeriod.WEEK:
        offset = (day.isoweekday() - config.reset_day) % 7
        start = day - timedelta(days=offset)
        return start, start + timedelta(days=7)
    reset_day = config.reset_day
    if day.day >= reset_day:
        start = day.replace(day=reset_day)
    else:
        last_of_previous = day.replace(day=1) - timedelta(days=1)
        start = last_of_previous.replace(day=reset_day)
    following = (start.replace(day=1) + timedelta(days=32)).replace(day=reset_day)
    return start, following


def usage_window(
    config: TrafficConfig, now: datetime, reset_at: datetime | None
) -> tuple[datetime, datetime]:
    """The span usage is summed over: the period, cut at a manual reset inside it."""
    start, end = period_bounds(config, now)
    if reset_at is not None and start < reset_at < end:
        start = reset_at
    return start, end


def _epoch(dt: datetime) -> float:
    """Epoch seconds of a naive UTC datetime."""
    return dt.replace(tzinfo=UTC).timestamp()


@dataclass
class _UsageState:
    """One connector's usage in its current window, as this instance sees it."""

    period_start: datetime
    period_end: datetime
    # Flushed to Redis by someone (this instance or a peer) or read from history.
    known_sent: int = 0
    known_received: int = 0
    # This instance's own transfers, not yet in Redis.
    unflushed_sent: int = 0
    unflushed_received: int = 0
    # Set when the window was started without a history read (rollover seen on
    # the hot path); the next evaluate refreshes from history first.
    stale: bool = False
    warned: bool = False
    exceeded: bool = False

    @property
    def bytes_sent(self) -> int:
        return self.known_sent + self.unflushed_sent

    @property
    def bytes_received(self) -> int:
        return self.known_received + self.unflushed_received

    @property
    def total(self) -> int:
        return self.bytes_sent + self.bytes_received


class TrafficMeter:
    """Byte counter for one transfer through one proxy.

    The transfer loops call ``add_sent`` / ``add_received`` per chunk and stop
    when either returns False. ``finish`` returns what has not been reported
    yet, for the completion event, so nothing is counted twice.
    """

    __slots__ = (
        "_limiter", "proxy_id", "project_id", "connector_id",
        "sent", "received", "_reported_sent", "_reported_received", "_last_report",
    )

    def __init__(
        self, limiter: TrafficLimiter, proxy_id: str, project_id: str, connector_id: str
    ) -> None:
        self._limiter = limiter
        self.proxy_id = proxy_id
        self.project_id = project_id
        self.connector_id = connector_id
        self.sent = 0
        self.received = 0
        self._reported_sent = 0
        self._reported_received = 0
        self._last_report = time.monotonic()

    @property
    def allowed(self) -> bool:
        """False once the connector is blocked with the interrupt action."""
        return not self._limiter.is_interrupted(self.connector_id)

    @property
    def limit_status(self) -> int:
        """The HTTP status the connector's config reserves for the limit."""
        return self._limiter.limit_status_for(self.connector_id)

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
        """Hand everything not yet reported to the limiter now."""
        sent = self.sent - self._reported_sent
        received = self.received - self._reported_received
        self._reported_sent = self.sent
        self._reported_received = self.received
        self._last_report = time.monotonic()
        if sent or received:
            self._limiter.progress(self.proxy_id, self.project_id, self.connector_id, sent, received)

    def finish(self) -> tuple[int, int]:
        """Bytes not yet reported, for the completion event; the meter is spent."""
        sent = self.sent - self._reported_sent
        received = self.received - self._reported_received
        self._reported_sent = self.sent
        self._reported_received = self.received
        return sent, received


class TrafficLimiter:
    """Tracks every connector's traffic against its period and limit.

    Args:
        redis_client: Holds the block keys peers read.
        get_connector: Resolves a connector by id from the manager's cache.
        sink: Receives transfer progress to fold into the pending metric deltas.
        loader: Reads the flushed period totals (history plus Redis window).
    """

    def __init__(
        self,
        redis_client: RedisClient,
        get_connector: Callable[[str], Connector | None],
        sink: ProgressSink | None = None,
        loader: TotalsLoader | None = None,
    ) -> None:
        self._redis_client = redis_client
        self._get_connector = get_connector
        self.sink = sink
        self.loader = loader
        self._states: dict[str, _UsageState] = {}
        # connector_id -> epoch second the block ends (the period end).
        self._blocked: dict[str, float] = {}
        # Parsed config per connector, valid while the connector still carries
        # the very same traffic_config dict (a write replaces the dict, or the
        # whole object; nothing edits the dict in place).
        self._config_cache: dict[str, tuple[dict[str, Any], TrafficConfig]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Config and state ---------------------------------------------------

    def config_for(self, connector: Connector) -> TrafficConfig:
        """The connector's parsed traffic config, cached per traffic_config dict."""
        cached = self._config_cache.get(connector.id)
        if cached is not None and cached[0] is connector.traffic_config:
            return cached[1]
        config = connector.parsed_traffic_config
        self._config_cache[connector.id] = (connector.traffic_config, config)
        return config

    async def sync_config(self, connector: Connector) -> None:
        """After a connector write: recount and re-apply if its traffic settings moved.

        The API edits the cached object in place, so the manager cannot tell
        what changed; comparing against what this limiter last parsed can.
        """
        cached = self._config_cache.get(connector.id)
        old_config = cached[1] if cached is not None else None
        new_config = connector.parsed_traffic_config
        state = self._states.get(connector.id)
        window = usage_window(new_config, utc_now(), connector.traffic_reset_at)
        window_moved = state is not None and (state.period_start, state.period_end) != window
        self._config_cache[connector.id] = (connector.traffic_config, new_config)
        if old_config != new_config or window_moved:
            await self.reconfigure(connector.id)

    def limit_status_for(self, connector_id: str) -> int:
        connector = self._get_connector(connector_id)
        if connector is None:
            return TrafficConfig().limit_status
        return self.config_for(connector).limit_status

    def _state_for(self, connector: Connector, now: datetime) -> tuple[_UsageState, bool]:
        """The connector's state for the window containing ``now``; True when it just changed.

        A new window (first sight, period rollover, config edit or manual
        reset) starts from zero known bytes. Unflushed bytes are seconds old
        and stay counted; the state is marked stale so the next evaluate
        reads the exact total from history.
        """
        config = self.config_for(connector)
        start, end = usage_window(config, now, connector.traffic_reset_at)
        state = self._states.get(connector.id)
        if state is not None and state.period_start == start and state.period_end == end:
            return state, False
        fresh = _UsageState(period_start=start, period_end=end, stale=True)
        if state is not None:
            fresh.unflushed_sent = state.unflushed_sent
            fresh.unflushed_received = state.unflushed_received
        self._states[connector.id] = fresh
        return fresh, True

    @staticmethod
    def _crossed(config: TrafficConfig, before: int, after: int) -> bool:
        """Whether the total passed the warning or the limit threshold."""
        if config.limit_bytes is None:
            return False
        warn = config.warn_bytes or 0
        return before < warn <= after or before < config.limit_bytes <= after

    # --- Hot path -------------------------------------------------------------

    def is_blocked(self, connector_id: str) -> bool:
        """Whether the connector takes no new requests right now (sync, no I/O)."""
        until = self._blocked.get(connector_id)
        if until is None:
            return False
        if time.time() >= until:
            # The period the block was raised in is over.
            del self._blocked[connector_id]
            return False
        return True

    def is_interrupted(self, connector_id: str) -> bool:
        """Whether running transfers on the connector must stop."""
        if not self.is_blocked(connector_id):
            return False
        connector = self._get_connector(connector_id)
        return (
            connector is not None
            and self.config_for(connector).action == TrafficLimitAction.INTERRUPT
        )

    @property
    def blocked_count(self) -> int:
        """How many connectors this instance currently holds blocked."""
        now = time.time()
        return sum(1 for until in self._blocked.values() if until > now)

    def meter(self, proxy_id: str, project_id: str, connector_id: str) -> TrafficMeter:
        """A fresh meter for one transfer."""
        return TrafficMeter(self, proxy_id, project_id, connector_id)

    def progress(
        self, proxy_id: str, project_id: str, connector_id: str, sent: int, received: int
    ) -> None:
        """A running transfer reports bytes: into the pending deltas, then against the limit."""
        if self.sink is not None:
            self.sink(proxy_id, project_id, connector_id, sent, received)
        if self.record(connector_id, sent, received):
            self._schedule(self.evaluate(connector_id))

    def record(self, connector_id: str, sent: int, received: int) -> bool:
        """Count this instance's own bytes; True when the caller should ``evaluate``."""
        connector = self._get_connector(connector_id)
        if connector is None:
            return False
        state, changed = self._state_for(connector, utc_now())
        before = state.total
        state.unflushed_sent += sent
        state.unflushed_received += received
        return changed or self._crossed(self.config_for(connector), before, state.total)

    def mark_flushed(self, connector_deltas: dict[str, MetricDelta]) -> None:
        """This instance's deltas reached Redis: they are known now, not unflushed."""
        for connector_id, delta in connector_deltas.items():
            state = self._states.get(connector_id)
            if state is None:
                continue
            state.known_sent += delta.bytes_sent
            state.known_received += delta.bytes_received
            state.unflushed_sent = max(0, state.unflushed_sent - delta.bytes_sent)
            state.unflushed_received = max(0, state.unflushed_received - delta.bytes_received)

    async def apply_peer(self, connector_deltas: dict[str, MetricDelta]) -> None:
        """Fold a peer instance's flushed deltas into the known totals."""
        for connector_id, delta in connector_deltas.items():
            connector = self._get_connector(connector_id)
            if connector is None:
                continue
            state, changed = self._state_for(connector, utc_now())
            before = state.total
            state.known_sent += delta.bytes_sent
            state.known_received += delta.bytes_received
            if changed or self._crossed(self.config_for(connector), before, state.total):
                await self.evaluate(connector_id)

    # --- Reads ----------------------------------------------------------------

    def usage(self, connector: Connector, now: datetime | None = None) -> TrafficUsage:
        """The connector's usage this period, for API responses and exports."""
        now = now or utc_now()
        config = self.config_for(connector)
        state, _ = self._state_for(connector, now)
        total = state.total
        limit = config.limit_bytes
        if limit is None:
            status = "ok"
        elif total >= limit:
            status = "exceeded"
        elif total >= (config.warn_bytes or 0):
            status = "warning"
        else:
            status = "ok"
        reset_at = connector.traffic_reset_at
        if reset_at is not None and not (state.period_start <= reset_at < state.period_end):
            reset_at = None
        return TrafficUsage(
            period=config.period,
            period_start=state.period_start,
            period_end=state.period_end,
            bytes_sent=state.bytes_sent,
            bytes_received=state.bytes_received,
            total_bytes=total,
            limit_bytes=limit,
            percent=round(total / limit * 100, 2) if limit else None,
            action=config.action,
            status=status,  # type: ignore[arg-type]
            blocked=self.is_blocked(connector.id),
            price_per_gb=config.price_per_gb,
            currency=config.currency,
            cost=config.cost_of(total),
            reset_at=reset_at,
        )

    # --- Evaluation and state transitions -------------------------------------

    async def evaluate(self, connector_id: str) -> None:
        """Apply the connector's limit to its current usage. Idempotent."""
        connector = self._get_connector(connector_id)
        if connector is None:
            await self.forget(connector_id)
            return
        config = self.config_for(connector)
        state, _ = self._state_for(connector, utc_now())
        if state.stale:
            await self._refresh_known([connector_id])
            state = self._states[connector_id]
        total = state.total
        limit = config.limit_bytes
        blocked = self.is_blocked(connector_id)

        if limit is None:
            if blocked:
                await self._release(connector, "no_limit")
            return

        if total >= limit:
            if not state.exceeded:
                state.exceeded = True
                state.warned = True
                logger.warning(
                    "Connector traffic limit reached",
                    connector_id=connector_id,
                    connector=connector.name,
                    project_id=connector.project_id,
                    total_bytes=total,
                    limit_bytes=limit,
                    action=config.action.value,
                    period_end=state.period_end.isoformat(),
                )
            if config.action == TrafficLimitAction.ALERT:
                if blocked:
                    await self._release(connector, "alert_only")
            elif not blocked:
                await self._block(connector, state)
            return

        if not state.warned and total >= (config.warn_bytes or 0):
            state.warned = True
            logger.warning(
                "Connector approaching its traffic limit",
                connector_id=connector_id,
                connector=connector.name,
                project_id=connector.project_id,
                total_bytes=total,
                limit_bytes=limit,
                percent=round(total / limit * 100, 1),
            )
        if blocked:
            await self._release(connector, "under_limit")

    async def _block(self, connector: Connector, state: _UsageState) -> None:
        until = _epoch(state.period_end)
        self._blocked[connector.id] = until
        await self._redis_client.set_connector_traffic_blocked(connector.id, until)
        logger.warning(
            "Connector blocked by its traffic limit",
            connector_id=connector.id,
            connector=connector.name,
            project_id=connector.project_id,
            action=self.config_for(connector).action.value,
            until=state.period_end.isoformat(),
        )
        await event_bus.publish(
            connector_traffic_changed, self, entity_id=connector.id, op="blocked"
        )

    async def _release(self, connector: Connector, reason: str) -> None:
        self._blocked.pop(connector.id, None)
        await self._redis_client.clear_connector_traffic_blocked(connector.id)
        logger.info(
            "Connector released from its traffic limit",
            connector_id=connector.id,
            connector=connector.name,
            reason=reason,
        )
        await event_bus.publish(
            connector_traffic_changed, self, entity_id=connector.id, op="released"
        )

    async def refresh(self, connector_ids: list[str]) -> None:
        """Reload the period totals from history and re-apply every limit.

        Runs at startup and on the periodic full reload; also after a config
        edit or a manual reset, when the window itself changed.
        """
        await self._refresh_known(connector_ids)
        for connector_id in connector_ids:
            connector = self._get_connector(connector_id)
            if connector is None:
                continue
            if self.config_for(connector).limit_enabled or self.is_blocked(connector_id):
                await self.evaluate(connector_id)

    async def _refresh_known(self, connector_ids: list[str]) -> None:
        now = utc_now()
        windows: dict[str, datetime] = {}
        for connector_id in connector_ids:
            connector = self._get_connector(connector_id)
            if connector is None:
                continue
            state, _ = self._state_for(connector, now)
            windows[connector_id] = state.period_start
        if not windows:
            return
        if self.loader is None:
            # No history to read (standalone use): what has been applied so far is all there is.
            for connector_id in windows:
                self._states[connector_id].stale = False
            return
        totals = await self.loader(windows)
        for connector_id in windows:
            state = self._states[connector_id]
            state.known_sent, state.known_received = totals.get(connector_id, (0, 0))
            state.stale = False

    async def hydrate_blocked_from_redis(self, connector_ids: list[str]) -> None:
        """Restore the block state peers (or this instance, before a restart) raised."""
        for connector_id in connector_ids:
            await self.refresh_blocked_for(connector_id)

    async def refresh_blocked_for(self, connector_id: str) -> None:
        """Re-read one connector's block key; the cross-instance handler."""
        until = await self._redis_client.get_connector_traffic_blocked(connector_id)
        if until is None:
            self._blocked.pop(connector_id, None)
        else:
            self._blocked[connector_id] = until

    async def reconfigure(self, connector_id: str) -> None:
        """The connector's traffic config or reset point changed: start over from history."""
        self._config_cache.pop(connector_id, None)
        self._states.pop(connector_id, None)
        connector = self._get_connector(connector_id)
        if connector is None:
            await self.forget(connector_id)
            return
        await self._refresh_known([connector_id])
        await self.evaluate(connector_id)

    async def forget(self, connector_id: str) -> None:
        """Drop every trace of a removed connector."""
        self._states.pop(connector_id, None)
        self._config_cache.pop(connector_id, None)
        self._blocked.pop(connector_id, None)
        await self._redis_client.clear_connector_traffic_blocked(connector_id)

    def forget_local(self, connector_id: str) -> None:
        """Drop in-memory state only (a peer removed the connector and owns the Redis key)."""
        self._states.pop(connector_id, None)
        self._config_cache.pop(connector_id, None)
        self._blocked.pop(connector_id, None)

    # --- Background evaluation ------------------------------------------------

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run an evaluation off the transfer loop that triggered it."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Traffic limit evaluation failed", error=str(exc))
