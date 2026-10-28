# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Request metering: what a gateway counts per request, in memory off the request path, and the publisher that adds it to Redis.

Each window also asks the workers for a traffic decision on every connector with a traffic limit whose bytes it carries.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import structlog

from api.core.demand_tracker import (
    DEMAND_REJECTIONS_FIELD,
    DEMAND_REQUESTS_FIELD,
    DEMAND_TTL_SECONDS,
    DEMAND_WINDOW_SECONDS,
    demand_minute,
)
from api.core.publishers import BatchPublisher
from api.core.signals import (
    request_completed,
    request_rejected,
    tunnel_encrypted_dns_blocked,
    tunnel_name_unresolved,
)
from api.core.stats import (
    MetricDelta,
    StepMetricDelta,
    TunnelPeerMetricDelta,
    fold_host_tail,
    normalize_host,
)
from api.core.tasks import HeldTasks
from api.core.traffic_limiter import TrafficMeter
from api.core.workers import WorkerName
from api.db.redis import MetricWindows
from api.gateway.latency import RequestLatencies
from api.models.connector import DEFAULT_TRAFFIC_LIMIT_STATUS, TrafficLimitAction

if TYPE_CHECKING:
    from api.core.rate_limiter import RateLimiter
    from api.core.stats import DemandCountKey
    from api.core.timing import Step
    from api.db.redis import BatchPart, BatchReply, RedisClient
    from api.models.proxy import Proxy
    from api.routing.inflight import InFlight
    from api.routing.outcomes import LocalOutcomes
    from api.routing.router import Router
    from api.routing.snapshot import ConnectorView

logger = structlog.get_logger()

# How often a gateway writes what it counted to Redis, in seconds. Fast enough
# that the dashboard feels live, slow enough that one pipeline carries many
# requests: a busy gateway turns ten thousand requests a second into one write
# every five seconds. It is also most of how far a connector can overshoot its
# traffic limit before the block reaches the gateways (docs/traffic-limits.md).
METERING_INTERVAL_SECONDS = 5.0
# A project's rejected requests count as demand at most once this often, in
# seconds, per gateway. Clients retry a rejected request in a tight loop, and a
# retry storm is one request the pool did not meet, not thousands.
REJECTION_COUNT_INTERVAL_SECONDS = 1.0


class RequestMetering(BatchPublisher):
    """A gateway's request counters, and the publisher that adds them to Redis a window at a time; each gateway runs one.

    Every :class:`~api.core.traffic_limiter.TrafficMeter` reports to it, and each finished request's outcome goes to the
    gateway's :class:`~api.routing.outcomes.LocalOutcomes`, which its routing strategies read, and its latency to the
    histograms ``/metrics`` exports.

    Args:
        redis_client: takes each window on its ``metering`` batch channel, one writer per process.
        outcomes: takes the outcome of each finished request through a proxy the snapshot holds.
        router: the gateway's router, whose snapshot in force each lookup reads, so a swap applies at once.
        host_metrics_max_hosts: how many hosts each project keeps named per window; 0 keeps every host.
        clock: epoch seconds now, the minute demand counts under; tests pass their own.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        inflight: InFlight,
        rate_limiter: RateLimiter,
        *,
        outcomes: LocalOutcomes,
        router: Router,
        host_metrics_enabled: bool = True,
        host_metrics_max_hosts: int = 0,
        interval: float = METERING_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(redis_client, name=WorkerName.REQUEST_METERING, interval=interval)
        self._inflight = inflight
        self._rate_limiter = rate_limiter
        self._outcomes = outcomes
        self._router = router
        self._host_metrics = host_metrics_enabled
        self._max_hosts = host_metrics_max_hosts
        self._clock = clock
        # What the gateway counted since its last flush, keyed the way the Redis hashes are, and the demand beside it.
        self._windows = MetricWindows()
        self._demand: Counter[DemandCountKey] = Counter()
        # Per project, the monotonic time its last counted rejection was seen.
        self._last_rejection: dict[str, float] = {}
        # Rate-limit windows being counted, awaited on stop.
        self._held = HeldTasks()
        # Every request and byte this gateway carried since it started, for its snapshot.
        self.totals = MetricDelta()
        # Every request's latency and steps since it started, by connector, for its snapshot.
        self.latencies = RequestLatencies()
        # Sends that did not land whole since start: Redis did not answer or
        # refused a part (sent again), or a part's script failed (dropped).
        self.failed_flushes = 0

    # --- Lifecycle ------------------------------------------------------------------

    def start(self, senders: Iterable[object]) -> None:
        """Start counting the requests that end and are rejected, and the tunnel signals, as ``senders`` publish them.

        The senders are this gateway's own listeners, so gateways sharing one process count apart.
        """
        for sender in senders:
            request_completed.connect(self._on_request_completed, sender=sender)
            request_rejected.connect(self._on_request_rejected, sender=sender)
            tunnel_name_unresolved.connect(self._on_tunnel_name_unresolved, sender=sender)
            tunnel_encrypted_dns_blocked.connect(self._on_tunnel_encrypted_dns_blocked, sender=sender)

    async def stop(self) -> None:
        """Stop counting and wait for the rate-limit windows being counted, then end the loop and send what is left; never raises."""
        request_completed.disconnect(self._on_request_completed)
        request_rejected.disconnect(self._on_request_rejected)
        tunnel_name_unresolved.disconnect(self._on_tunnel_name_unresolved)
        tunnel_encrypted_dns_blocked.disconnect(self._on_tunnel_encrypted_dns_blocked)
        await self._held.settle()
        await super().stop()

    # --- Signal receivers: memory only, never raising ------------------------------------

    async def _on_request_completed(
        self,
        sender: object,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
        peer_id: str | None = None,
        target_host: str | None = None,
        steps: Mapping[Step, float] | None = None,
        **_: object,
    ) -> None:
        """``request_completed`` from a listener."""
        try:
            self.record_completed(
                proxy_id, project_id, success, latency_ms, bytes_sent, bytes_received, peer_id, target_host, steps
            )
        except Exception:
            logger.error("Counting a request failed", proxy_id=proxy_id, project_id=project_id, exc_info=True)

    async def _on_request_rejected(self, sender: object, project_id: str, **_: object) -> None:
        """``request_rejected`` from a listener."""
        try:
            self.record_rejection(project_id)
        except Exception:
            logger.error("Counting a rejected request failed", project_id=project_id, exc_info=True)

    async def _on_tunnel_name_unresolved(self, sender: object, peer_id: str, **_: object) -> None:
        """``tunnel_name_unresolved`` from the tunnel listener."""
        try:
            self.record_unresolved(peer_id)
        except Exception:
            logger.error("Counting a tunnel connection failed", peer_id=peer_id, exc_info=True)

    async def _on_tunnel_encrypted_dns_blocked(self, sender: object, peer_id: str, **_: object) -> None:
        """``tunnel_encrypted_dns_blocked`` from the tunnel listener."""
        try:
            self.record_encrypted_dns_blocked(peer_id)
        except Exception:
            logger.error("Counting a tunnel connection failed", peer_id=peer_id, exc_info=True)

    # --- Counting ---------------------------------------------------------------------

    def record_completed(
        self,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
        peer_id: str | None = None,
        target_host: str | None = None,
        steps: Mapping[Step, float] | None = None,
    ) -> None:
        """Count one finished request, its bytes being what its meter had not reported yet; never blocks.

        ``steps`` is its latency by step (``RequestTimer.steps``). A request through a connector with a rate limit is
        also counted in the proxy's shared window, in a held task.
        """
        view = self._router.snapshot.view_of(proxy_id)
        for delta in self._deltas(proxy_id, project_id, view, peer_id, target_host):
            delta.add_request(success, latency_ms, bytes_sent, bytes_received, steps)
        self.totals.add_request(success, latency_ms, bytes_sent, bytes_received)
        self._demand[(project_id, demand_minute(self._clock()), DEMAND_REQUESTS_FIELD)] += 1
        if view is None:
            return
        self._outcomes.record(proxy_id, success, latency_ms)
        self.latencies.record(view.connector.id, view.connector.name, success, latency_ms, steps or {})
        rate = view.rate_limit
        if rate is not None:
            self._held.spawn(
                self._rate_limiter.record_request(
                    proxy_id, view.connector.id, rate.max_requests, rate.window_seconds,
                    rate.quarantine_seconds_min, rate.quarantine_seconds_max,
                ),
                name="rate_limit_window",
                proxy_id=proxy_id,
            )

    def record_rejection(self, project_id: str) -> None:
        """Count a request of the project rejected before any proxy served it, as demand the pool did not meet.

        At most one per project every ``REJECTION_COUNT_INTERVAL_SECONDS``.
        """
        now = time.monotonic()
        last = self._last_rejection.get(project_id)
        if last is not None and now - last < REJECTION_COUNT_INTERVAL_SECONDS:
            return
        self._last_rejection[project_id] = now
        self._demand[(project_id, demand_minute(self._clock()), DEMAND_REJECTIONS_FIELD)] += 1

    def record_unresolved(self, peer_id: str) -> None:
        """A tunnel connection was relayed by address (its name could not be recovered): counted on the device."""
        self._windows.tunnel_peers.setdefault(peer_id, TunnelPeerMetricDelta()).by_address += 1

    def record_encrypted_dns_blocked(self, peer_id: str) -> None:
        """An encrypted-DNS connection of a tunnel device was closed: counted on the device."""
        self._windows.tunnel_peers.setdefault(peer_id, TunnelPeerMetricDelta()).encrypted_dns_blocked += 1

    def _deltas(
        self,
        proxy_id: str,
        project_id: str,
        view: ConnectorView | None,
        peer_id: str | None,
        target_host: str | None,
    ) -> list[MetricDelta]:
        """The pending deltas one request or transfer counts on: its project, and its tunnel device when it came through one.

        Its proxy, connector and destination host only while the proxy is in the routing snapshot, since a proxy
        deleted meanwhile has nothing to count on. The project's and connector's keep the latency by step.
        """
        window = self._windows
        deltas: list[MetricDelta] = [window.projects.setdefault(project_id, StepMetricDelta())]
        if view is not None:
            connector_id = view.connector.id
            deltas.append(window.proxies.setdefault(proxy_id, MetricDelta()))
            deltas.append(window.connectors.setdefault(connector_id, StepMetricDelta()))
            host = normalize_host(target_host) if self._host_metrics else None
            if host is not None:
                deltas.append(window.hosts.setdefault((project_id, connector_id, host), MetricDelta()))
        if peer_id is not None:
            deltas.append(window.tunnel_peers.setdefault(peer_id, TunnelPeerMetricDelta()))
        return deltas

    # --- Meters -----------------------------------------------------------------------

    def meter(
        self, proxy: Proxy, project_id: str, *, peer_id: str | None = None, target_host: str | None = None
    ) -> TrafficMeter:
        """A meter for one transfer through ``proxy``: fed as bytes flow, finished when the transfer is over.

        The transfer counts as in flight on the proxy and its connector until the meter is finished.

        Args:
            peer_id: the tunnel device the transfer came from, whose counts take its bytes too.
            target_host: the transfer's destination, whose counts take its bytes too.
        """
        self._inflight.begin(proxy.id, proxy.connector_id)
        return TrafficMeter(
            self, proxy.id, project_id, proxy.connector_id, peer_id, target_host,
            on_finish=partial(self._inflight.end, proxy.id, proxy.connector_id),
        )

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
        """A running transfer reports the bytes it carried since its last report; the request counts when it ends.

        Runs on the transfer's own loop, so it never raises.
        """
        try:
            view = self._router.snapshot.view_of(proxy_id)
            for delta in self._deltas(proxy_id, project_id, view, peer_id, target_host):
                delta.add_bytes(sent, received)
            self.totals.add_bytes(sent, received)
        except Exception:
            logger.error(
                "Counting transfer progress failed", proxy_id=proxy_id, connector_id=connector_id, exc_info=True
            )

    def is_interrupted(self, connector_id: str) -> bool:
        """Whether transfers through the connector must stop now: its traffic is blocked, with the interrupt action.

        Both as the snapshot in force says, so a running transfer follows a
        change of the connector's traffic config at its next chunk.
        """
        view = self._router.snapshot.connector_view(connector_id)
        return (
            view is not None
            and view.traffic.action == TrafficLimitAction.INTERRUPT
            and self._router.is_traffic_blocked(connector_id)
        )

    def limit_status_for(self, connector_id: str) -> int:
        """The HTTP status the connector's traffic config reserves for the limit."""
        view = self._router.snapshot.connector_view(connector_id)
        return view.traffic.limit_status if view is not None else DEFAULT_TRAFFIC_LIMIT_STATUS

    # --- Publishing ---------------------------------------------------------------------

    def _windowed(self) -> int:
        """Entries the window holds: one per entity, host row and demand count."""
        return len(self._windows) + len(self._demand)

    def _cut(self, redis_client: RedisClient) -> Sequence[BatchPart]:
        """The window as metering parts, and a new window begun; demand minutes no reader looks back to are left out."""
        # Read before the window is cut, so a snapshot that cannot be read keeps the window for the next flush.
        limited = self._limited(self._windows.connectors)
        windows, self._windows = self._windows, MetricWindows()
        demand, self._demand = self._demand, Counter()
        # The readers' window moved on while a window that was owed waited.
        oldest_read = demand_minute(self._clock()) - DEMAND_WINDOW_SECONDS // 60
        return redis_client.metering_batch(
            replace(windows, hosts=fold_host_tail(windows.hosts, self._max_hosts)),
            demand={key: count for key, count in demand.items() if key[1] >= oldest_read},
            demand_ttl_seconds=DEMAND_TTL_SECONDS,
            metered=limited,
        )

    async def _send(self) -> list[tuple[BatchPart, BatchReply]]:
        """One send through the batch sender, counted in ``failed_flushes`` unless all of it landed."""
        try:
            answered = await super()._send()
        except BaseException:
            self.failed_flushes += 1
            raise
        if not all(reply.landed for _part, reply in answered):
            self.failed_flushes += 1
        return answered

    def _limited(self, connectors: Mapping[str, MetricDelta]) -> list[str]:
        """The connectors of a window that the snapshot in force gives a traffic limit."""
        snapshot = self._router.snapshot
        return [
            connector_id
            for connector_id in connectors
            if (view := snapshot.connector_view(connector_id)) is not None and view.traffic.limit_enabled
        ]
