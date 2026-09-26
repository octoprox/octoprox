# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy pool manager for Octoprox.

Subscribes to signals from HealthChecker and ProxyServer.
Emits proxy lifecycle signals (proxy_added, proxy_removed, proxy_status_changed).
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.auto_scaler import AutoScaler
from api.core.config import Settings
from api.core.demand_tracker import DemandTracker
from api.core.domain_filter import is_domain_allowed
from api.core.entity_index import ConnectorIndex, ProxyIndex
from api.core.event_bus import EVENT_CHANNEL, RedisPubSubTransport, event_bus
from api.core.health_checker import HealthChecker, IpExtractionRules
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.metrics_compactor import MetricsCompactor
from api.core.metrics_flusher import MetricsFlusher
from api.core.provider_syncer import ProxyProviderSyncer
from api.core.rate_limiter import RateLimiter
from api.core.signals import (
    connector_changed,
    connector_error_updated,
    connector_remove_requested,
    credential_changed,
    health_check_completed,
    project_changed,
    proxy_add_requested,
    proxy_added,
    proxy_changed,
    proxy_draining_requested,
    proxy_draining_started,
    proxy_marked_terminating,
    proxy_remove_requested,
    proxy_removed,
    proxy_status_changed,
    proxy_terminating_requested,
    proxy_update_requested,
    request_completed,
)
from api.core.stats import MetricDelta
from api.core.system_snapshotter import SystemSnapshotter
from api.core.traffic_limiter import TrafficLimiter, TrafficMeter
from api.core.workers import WorkerName
from api.db.redis import (
    INSTANCE_HEARTBEAT_INTERVAL,
    INSTANCE_REGISTRY_KEY,
    INSTANCE_STATS_KEY,
    INSTANCE_TTL_SECONDS,
    METRIC_DELTAS_CHANNEL,
    RedisClient,
)
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.geo.models import META_LOCATION_CONFLICT, LocationPolicy
from api.models.connector import (
    DEFAULT_ROUTING_WEIGHT,
    DEFAULT_TRAFFIC_LIMIT_STATUS,
    Connector,
    ProxyTarget,
    TrafficUsage,
)
from api.models.credential import Credential
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry, ProviderType, get_provider_registry
from api.providers.sdk.provider import DescriptorProvider
from api.providers.sdk.strategies import META_GEO, is_dynamic_gateway
from api.providers.store import ProviderStore
from api.strategies import ProxyGroup, get_strategy

if TYPE_CHECKING:
    from api.models.system import InstanceSnapshot
    from api.strategies.base import RoutingStrategy

logger = structlog.get_logger()


class ProxyManager:
    """Manages the proxy pool and routing.

    Uses Postgres for persistent storage of proxies, credentials, and connectors.
    Uses Redis for operational data (health status, metrics, sessions).

    Args:
        session_factory: Async session factory for database operations.
        redis_client: Redis client for operational data.
        settings: Application settings.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        settings: Settings,
        provider_registry: ProviderRegistry | None = None,
        health_check_extraction_rules: IpExtractionRules | None = None,
        cross_instance_handlers: Mapping[Any, Callable[[str, str | None], Awaitable[None]]] | None = None,
        reload_hooks: Sequence[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        """
        Args:
            cross_instance_handlers: Extra signals to forward over Redis, each
                with the ``handler(entity_id, op)`` this instance runs when a
                peer publishes it. Other subsystems join the change feed here
                without this class naming them.
            reload_hooks: Run on every periodic full reload, after the
                provider store re-syncs, for the same reason.
        """
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._settings = settings
        self._provider_registry = provider_registry or get_provider_registry()
        self._cross_instance_extra_signals: list[Any] = list((cross_instance_handlers or {}).keys())
        self._cross_instance_handlers: dict[str, Callable[[str, str | None], Awaitable[None]]] = {
            signal.name: handler for signal, handler in (cross_instance_handlers or {}).items()
        }
        self._reload_hooks: list[Callable[[], Awaitable[None]]] = list(reload_hooks or [])
        self._provider_store = ProviderStore(self._provider_registry, session_factory)

        # In-memory cache (loaded from Postgres on start)
        self._projects: dict[str, Project] = {}
        self._proxies: ProxyIndex = ProxyIndex()
        self._credentials: dict[str, Credential] = {}
        self._connectors: ConnectorIndex = ConnectorIndex()
        # One lock per (connector, country) so a burst of -cc- requests provisions a group once
        self._geo_provision_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Descriptor providers by connector id, keyed on object identity (see _descriptor_provider).
        self._provider_cache: dict[str, tuple[Connector, Credential, ProviderType, DescriptorProvider]] = {}
        # Per-project strategies (project_id -> strategy)
        self._project_strategies: dict[str, RoutingStrategy] = {}
        # Default strategy for backward compatibility
        self._strategy: RoutingStrategy = get_strategy(settings.default_strategy)
        self._health_checker = HealthChecker(
            self, redis_client, settings.instance_id, extraction_rules=health_check_extraction_rules
        )
        self._metrics_flusher = MetricsFlusher(session_factory, redis_client, settings)
        self._metrics_compactor = MetricsCompactor(
            session_factory, redis_client, settings.instance_id
        )
        self._demand_tracker = DemandTracker(redis_client)
        self._auto_scaler = AutoScaler(self, redis_client, settings.instance_id)
        self._provider_syncer = ProxyProviderSyncer(
            self, redis_client, settings.instance_id, self._provider_registry
        )
        self._rate_limiter = RateLimiter(redis_client)
        # Per-connector traffic usage and limits. Reads connectors from this
        # cache, folds transfer progress into the pending deltas below, and
        # loads period totals from history through the manager's session.
        self._traffic_limiter = TrafficLimiter(
            redis_client,
            get_connector=lambda connector_id: self._connectors.get(connector_id),
            sink=self._record_traffic_progress,
            loader=self._load_connector_traffic_totals,
        )
        self._system_snapshotter = SystemSnapshotter(
            session_factory, redis_client, self, settings
        )
        # Pending metric deltas accumulated since the last flush. The
        # per-request handler bumps local in-memory counters AND
        # appends here; ``_metric_delta_publisher_loop`` drains the
        # dicts in a single Redis pipeline and announces the same
        # deltas on Pub/Sub so peers can update without polling.
        self._pending_proxy_deltas: dict[str, MetricDelta] = {}
        self._pending_project_deltas: dict[str, MetricDelta] = {}
        self._pending_connector_deltas: dict[str, MetricDelta] = {}
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        # Builds what this instance publishes about itself on the heartbeat.
        # Set by the lifespan, because the snapshot spans components this
        # manager does not own. Left None in tests and anywhere else the
        # manager runs standalone, where the heartbeat then advertises
        # membership alone - exactly as it did before snapshots existed.
        self.snapshot_provider: Callable[[], InstanceSnapshot] | None = None

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        """Start a named background loop and keep its handle.

        The name is what the admin system view lists as a running worker, so
        it doubles as the loop's public identity - keep it stable.
        """
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def start(self) -> None:
        """Start the proxy manager and background tasks."""
        self._running = True
        logger.info("Starting proxy manager")

        # Subscribe to signals
        self._subscribe_to_signals()

        # Wire the cross-instance transport for the event bus. Imports kept
        # local so signals.py is not pulled in for tests that don't need it.
        from api.core.signals import (
            connector_changed,
            connector_traffic_changed,
            credential_changed,
            project_changed,
            provider_changed,
            proxy_changed,
            proxy_quarantine_changed,
        )
        event_bus.configure_distributed(
            RedisPubSubTransport(self._redis_client, self._settings.instance_id),
            [
                project_changed,
                credential_changed,
                connector_changed,
                proxy_changed,
                proxy_quarantine_changed,
                connector_traffic_changed,
                provider_changed,
                *self._cross_instance_extra_signals,
            ],
        )

        # Load admin-authored provider descriptors before any connector is
        # synced, so credentials of custom types resolve to a provider.
        await self._provider_store.sync_all()

        # Load data from Postgres into memory cache
        await self._load_from_database()

        # Hydrate with Redis operational data
        await self._hydrate_from_redis()

        # Start health checker
        self._spawn(WorkerName.HEALTH_CHECKER, self._health_checker.run())

        # Start metrics flusher
        self._spawn(WorkerName.METRICS_FLUSHER, self._metrics_flusher.run())

        # Start metrics compactor (compaction + retention)
        self._spawn(WorkerName.METRICS_COMPACTOR, self._metrics_compactor.run())

        # Start auto-scaler
        self._spawn(WorkerName.AUTO_SCALER, self._auto_scaler.run())

        # Start provider syncer (handles all proxy provider types)
        self._spawn(WorkerName.PROVIDER_SYNCER, self._provider_syncer.run())

        # Snapshot install-wide gauges for the admin trend charts. A disabled
        # snapshotter is not spawned at all, so it never shows up in the
        # worker list as a loop that has stopped.
        if self._system_snapshotter.enabled:
            self._spawn(WorkerName.SYSTEM_SNAPSHOTTER, self._system_snapshotter.run())

        # Advertise this instance's presence so future phases can discover
        # peers (cross-instance event fanout, sharded health checks, leases).
        self._spawn(WorkerName.HEARTBEAT, self._heartbeat_loop())

        # Safety-net reload from Postgres in case cross-instance invalidation
        # events get dropped (Redis Pub/Sub is best-effort).
        self._spawn(WorkerName.FULL_RELOAD, self._periodic_full_reload_loop())

        # Drain accumulated request-metric deltas to Redis (batched) and
        # announce them on Pub/Sub so peers update their in-memory view.
        # Keeps the hot path free of per-request Redis writes. Named for what
        # it publishes, not "flush", so it cannot be read as the leader-elected
        # ``metrics_flusher`` that writes Redis counters on to Postgres.
        self._spawn(WorkerName.METRIC_DELTA_PUBLISHER, self._metric_delta_publisher_loop())

        # Receive peer instances' metric deltas and fold them into local
        # in-memory counters.
        self._spawn(WorkerName.METRIC_DELTA_SUBSCRIBER, self._metric_delta_subscriber_loop())

        # Subscribe to cross-instance cache-invalidation events.
        self._spawn(WorkerName.CROSS_INSTANCE_SUBSCRIBER, self._cross_instance_subscriber_loop())

    async def _cross_instance_subscriber_loop(self) -> None:
        """Listen on the EventBus distributed channel and reload entities.

        Drops self-echoes by ``instance_id``, then dispatches by the
        signal's own name (no string duplication - the signal objects are
        the source of truth). The handler receives ``(entity_id, op)`` so
        it can short-circuit on "removed" without a wasted DB read.
        Reconnects on failure so a brief Redis hiccup does not silently
        mute cross-instance updates.
        """
        from api.core.signals import (
            connector_changed,
            connector_traffic_changed,
            credential_changed,
            project_changed,
            provider_changed,
            proxy_changed,
            proxy_quarantine_changed,
        )

        dispatch: dict[str, Any] = {
            project_changed.name: self._apply_project_change,
            credential_changed.name: self._apply_credential_change,
            connector_changed.name: self._apply_connector_change,
            proxy_changed.name: self._apply_proxy_change,
            proxy_quarantine_changed.name: self._apply_proxy_quarantine_change,
            connector_traffic_changed.name: self._apply_connector_traffic_change,
            provider_changed.name: self._apply_provider_change,
            **self._cross_instance_handlers,
        }
        my_id = self._settings.instance_id
        while self._running:
            try:
                pubsub = self._redis_client.client.pubsub()
                await pubsub.subscribe(EVENT_CHANNEL)
                try:
                    async for message in pubsub.listen():
                        if not self._running:
                            break
                        if message.get("type") != "message":
                            continue
                        try:
                            data = message.get("data")
                            if isinstance(data, bytes):
                                data = data.decode("utf-8")
                            payload = json.loads(data)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            logger.debug("Skipping malformed cross-instance message")
                            continue
                        if payload.get("instance_id") == my_id:
                            continue
                        handler = dispatch.get(payload.get("signal"))
                        entity_id = payload.get("entity_id")
                        if handler is None or not entity_id:
                            continue
                        op = payload.get("op")
                        try:
                            with job_stats.track(WorkerName.CROSS_INSTANCE_SUBSCRIBER):
                                await handler(entity_id, op)
                        except Exception:
                            logger.warning(
                                "Cross-instance reload handler failed",
                                signal=payload.get("signal"),
                                entity_id=entity_id,
                                op=op,
                                exc_info=True,
                            )
                finally:
                    with contextlib.suppress(Exception):
                        await pubsub.unsubscribe(EVENT_CHANNEL)
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Cross-instance subscriber failed, reconnecting", exc_info=True)
                await asyncio.sleep(1.0)

    # Op-aware dispatchers for the cross-instance subscriber. Each routes
    # "removed" to a synchronous cache eviction (no DB read needed), and
    # any other op (typically "added" / "updated") to a DB-reload path.
    async def _apply_project_change(self, project_id: str, op: str | None) -> None:
        if op == "removed":
            self._evict_project_from_cache(project_id)
            return
        await self.reload_project(project_id)

    async def _apply_credential_change(self, credential_id: str, op: str | None) -> None:
        if op == "removed":
            self._credentials.pop(credential_id, None)
            return
        await self.reload_credential(credential_id)

    async def _apply_connector_change(self, connector_id: str, op: str | None) -> None:
        if op == "removed":
            self._connectors.pop(connector_id, None)
            self._forget_connector_routing_state(connector_id)
            self._traffic_limiter.forget_local(connector_id)
            self._pending_connector_deltas.pop(connector_id, None)
            return
        await self.reload_connector(connector_id)

    async def _apply_connector_traffic_change(self, connector_id: str, op: str | None) -> None:
        # A peer blocked or released the connector; the Redis key it wrote is
        # authoritative, this instance just mirrors it so selection (and any
        # running transfer under the interrupt action) reacts now.
        await self._traffic_limiter.refresh_blocked_for(connector_id)

    async def _apply_proxy_change(self, proxy_id: str, op: str | None) -> None:
        if op == "removed":
            await self._evict_proxy_from_cache(proxy_id)
            return
        # A health flip we already have the proxy for needs Redis, not
        # Postgres. One we have never seen still needs its definition, so it
        # falls through to the full reload.
        if op == "status" and proxy_id in self._proxies:
            await self.refresh_proxy_status(proxy_id)
            return
        await self.reload_proxy(proxy_id)

    async def _apply_provider_change(self, provider_id: str, op: str | None) -> None:
        await self._provider_store.reload_one(provider_id, op)

    async def _apply_proxy_quarantine_change(
        self, proxy_id: str, op: str | None
    ) -> None:
        # Re-hydrate this proxy's quarantine TTL from Redis. The Redis key
        # set/cleared by the peer is authoritative; we just refresh our
        # local cache so selection sees the change immediately.
        await self._rate_limiter.refresh_quarantine_for(proxy_id)

    def _evict_project_from_cache(self, project_id: str) -> None:
        self._projects.pop(project_id, None)
        self._project_strategies.pop(project_id, None)
        # Never flush a delta for a row that is gone: it would recreate the
        # Redis hash the owner just cleared.
        self._pending_project_deltas.pop(project_id, None)

    async def _evict_proxy_from_cache(self, proxy_id: str) -> None:
        if proxy_id in self._proxies:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)
            await self._rate_limiter.remove_proxy(proxy_id)
            del self._proxies[proxy_id]
            self._pending_proxy_deltas.pop(proxy_id, None)

    def _instance_snapshot_json(self) -> str | None:
        """Serialise this instance's self-report, or None if it cannot be built.

        Publishing is best-effort: a snapshot that fails to build must not cost
        this instance its membership key, because peers shard health checks off
        that key. So the failure is logged and the heartbeat carries on with
        the registry write alone.
        """
        if self.snapshot_provider is None:
            return None
        try:
            return self.snapshot_provider().model_dump_json()
        except Exception:
            logger.warning("Instance snapshot could not be built", exc_info=True)
            return None

    async def _heartbeat_loop(self) -> None:
        """Write TTL'd Redis keys advertising this instance and what it sees.

        The registry key is the live-membership source for:

        * The cross-instance subscriber to drop self-echoes by ``instance_id``.
        * The HealthChecker's HRW shard ownership.
        * Lease holder identification (the lease value is the instance_id).

        Alongside it goes the snapshot key: the runtime, caches and worker
        counters that only this process can see, so the admin system view can
        show them for every instance rather than only for whichever one the
        load balancer routed the request to. Both are written in one pipeline,
        so a peer never reads a snapshot from an instance it thinks is gone.
        Building the snapshot touches memory only - see
        :func:`api.core.system_stats.build_instance_snapshot` - which is what
        keeps it affordable at this cadence.

        Cleanup is double-belt:

        * Redis TTL (10s) expires the keys automatically if the process
          dies hard (SIGKILL, OOM, network partition).
        * The ``finally`` block deletes them on graceful shutdown so
          peers see the departure immediately rather than waiting 10s.

        Refresh interval (5s) is deliberately half the TTL so a single
        missed write does not declare us dead.
        """
        key = INSTANCE_REGISTRY_KEY.format(instance_id=self._settings.instance_id)
        stats_key = INSTANCE_STATS_KEY.format(instance_id=self._settings.instance_id)
        payload = self._settings.role
        job_stats.declare_interval(WorkerName.HEARTBEAT, INSTANCE_HEARTBEAT_INTERVAL)
        try:
            while self._running:
                try:
                    with job_stats.track(WorkerName.HEARTBEAT):
                        snapshot = self._instance_snapshot_json()
                        pipe = self._redis_client.client.pipeline()
                        pipe.set(key, payload, ex=INSTANCE_TTL_SECONDS)
                        if snapshot is not None:
                            pipe.set(stats_key, snapshot, ex=INSTANCE_TTL_SECONDS)
                        await pipe.execute()
                except Exception:
                    logger.warning("Instance heartbeat write failed", exc_info=True)
                await asyncio.sleep(INSTANCE_HEARTBEAT_INTERVAL)
        finally:
            with contextlib.suppress(Exception):
                await self._redis_client.client.delete(key, stats_key)

    def _subscribe_to_signals(self) -> None:
        """Subscribe to signals from other components."""
        # Health check and request signals
        health_check_completed.connect(self._on_health_check_completed)
        request_completed.connect(self._on_request_completed)

        # AutoScaler request signals
        proxy_add_requested.connect(self._on_proxy_add_requested)
        proxy_remove_requested.connect(self._on_proxy_remove_requested)
        proxy_draining_requested.connect(self._on_proxy_draining_requested)
        proxy_terminating_requested.connect(self._on_proxy_terminating_requested)
        connector_remove_requested.connect(self._on_connector_remove_requested)
        connector_error_updated.connect(self._on_connector_error_updated)

        # Provider syncer signals
        proxy_update_requested.connect(self._on_proxy_update_requested)

        # Also subscribe DemandTracker to request_completed signal
        self._demand_tracker.subscribe_to_signals()
        logger.debug("ProxyManager subscribed to signals")

    async def _on_health_check_completed(
        self,
        sender: object,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float,
        consecutive_failures: int,
    ) -> None:
        """Handle health check completed signal from HealthChecker."""
        await self.update_proxy_status(
            proxy_id, status, latency_ms, consecutive_failures
        )

    async def _on_request_completed(
        self,
        sender: object,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
    ) -> None:
        """Handle request completed signal from ProxyServer."""
        await self._handle_request_stats(
            proxy_id, project_id, success, latency_ms, bytes_sent, bytes_received
        )

    async def _on_proxy_add_requested(
        self,
        sender: object,
        proxy: Proxy,
    ) -> None:
        """Handle proxy add request signal from AutoScaler."""
        await self.add_proxy(proxy)

    async def _on_proxy_remove_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy remove request signal from AutoScaler."""
        await self.remove_proxy(proxy_id)

    async def _on_proxy_draining_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy draining request signal from AutoScaler."""
        await self.start_proxy_draining(proxy_id)

    async def _on_proxy_terminating_requested(
        self,
        sender: object,
        proxy_id: str,
    ) -> None:
        """Handle proxy terminating request signal from AutoScaler."""
        await self.mark_proxy_terminating(proxy_id)

    async def _on_connector_remove_requested(
        self,
        sender: object,
        connector_id: str,
    ) -> None:
        """Handle connector remove request signal from AutoScaler."""
        await self.remove_connector(connector_id)

    async def _on_connector_error_updated(
        self,
        sender: object,
        connector_id: str,
        error: str | None,
        consecutive_errors: int,
    ) -> None:
        """Handle connector error updated signal from AutoScaler."""
        await self.update_connector_error(connector_id, error, consecutive_errors)

    async def _on_proxy_update_requested(
        self,
        sender: object,
        proxy: Proxy,
    ) -> None:
        """Handle proxy update request signal from ProxyProviderSyncer."""
        await self.update_proxy(proxy)

    async def _handle_request_stats(
        self,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
    ) -> None:
        """Handle request statistics update (internal implementation).

        Accumulates the request's contribution into a pending delta -
        nothing else. The hot path makes zero Redis calls (except the
        rate-limiter check below, which is correctness-critical and
        only opt-in).

        In-memory counters on ``Proxy`` / ``Project`` are intentionally
        NOT bumped here. The local instance would otherwise be ahead of
        its peers between the request and the next flush, which the UI
        would see as flapping when round-robin polls hit different
        instances. Instead, the flush loop applies the same delta to
        in-memory at the same moment it announces it to peers - see
        ``_flush_pending_metrics``.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy:
            self._pending_proxy_deltas.setdefault(proxy_id, MetricDelta()).add_request(
                success, latency_ms, bytes_sent, bytes_received,
            )

            connector = self._connectors.get(proxy.connector_id)
            if connector:
                self._pending_connector_deltas.setdefault(connector.id, MetricDelta()).add_request(
                    success, latency_ms, bytes_sent, bytes_received,
                )
                # Bytes reported while the transfer ran are already counted;
                # these are the remainder (see TrafficMeter.finish).
                if self._traffic_limiter.record(connector.id, bytes_sent, bytes_received):
                    await self._traffic_limiter.evaluate(connector.id)

                rl_config = connector.parsed_rate_limit_config
                if rl_config:
                    await self._rate_limiter.record_request(
                        proxy_id=proxy_id,
                        connector_id=proxy.connector_id,
                        max_requests=rl_config.max_requests,
                        window_seconds=rl_config.window_seconds,
                        quarantine_seconds_min=rl_config.quarantine_seconds_min,
                        quarantine_seconds_max=rl_config.quarantine_seconds_max,
                    )

        project = self._projects.get(project_id)
        if project:
            self._pending_project_deltas.setdefault(project_id, MetricDelta()).add_request(
                success, latency_ms, bytes_sent, bytes_received,
            )

    def _record_traffic_progress(
        self, proxy_id: str, project_id: str, connector_id: str, bytes_sent: int, bytes_received: int
    ) -> None:
        """A running transfer reports bytes so far: bytes only, the request is counted at its end."""
        if proxy_id in self._proxies:
            self._pending_proxy_deltas.setdefault(proxy_id, MetricDelta()).add_bytes(bytes_sent, bytes_received)
        if connector_id in self._connectors:
            self._pending_connector_deltas.setdefault(connector_id, MetricDelta()).add_bytes(bytes_sent, bytes_received)
        if project_id in self._projects:
            self._pending_project_deltas.setdefault(project_id, MetricDelta()).add_bytes(bytes_sent, bytes_received)

    def traffic_meter(self, proxy: Proxy, project_id: str) -> TrafficMeter:
        """A meter for one transfer through ``proxy``; the server feeds it as bytes flow."""
        return self._traffic_limiter.meter(proxy.id, project_id, proxy.connector_id)

    async def _load_connector_traffic_totals(
        self, since_by_connector: dict[str, datetime]
    ) -> dict[str, tuple[int, int]]:
        """Bytes per connector since its window start: flushed history plus the Redis window."""
        async with self._session_factory() as session:
            rows = await MetricsRepository(session).get_connector_totals_since(since_by_connector)
        window = await self._redis_client.get_all_connector_metrics()
        totals: dict[str, tuple[int, int]] = {}
        for connector_id in since_by_connector:
            history = rows.get(connector_id, {})
            current = window.get(connector_id) or MetricDelta()
            totals[connector_id] = (
                int(history.get("bytes_sent", 0)) + current.bytes_sent,
                int(history.get("bytes_received", 0)) + current.bytes_received,
            )
        return totals

    async def stop(self) -> None:
        """Stop the proxy manager and cleanup."""
        self._running = False
        logger.info("Stopping proxy manager")

        self._metrics_flusher.stop()
        self._auto_scaler.stop()

        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        # Release the EventBus distributed transport - it holds a reference
        # to the redis client, which the lifespan is about to close.
        event_bus.reset_distributed()

    async def _load_from_database(self) -> None:
        """Load projects, credentials, connectors and proxies from Postgres."""
        logger.info("Loading data from database")
        async with self._session_factory() as session:
            project_repo = ProjectRepository(session)
            credential_repo = CredentialRepository(session)
            connector_repo = ConnectorRepository(session)
            proxy_repo = ProxyRepository(session)

            # Load projects and initialize their strategies
            projects = await project_repo.get_all()
            for project in projects:
                self._projects[project.id] = project
                self._project_strategies[project.id] = get_strategy(project.routing_strategy)

            credentials = await credential_repo.get_all()
            for credential in credentials:
                self._credentials[credential.id] = credential

            connectors = await connector_repo.get_all()
            for connector in connectors:
                self._connectors[connector.id] = connector

            proxies = await proxy_repo.get_all()
            for proxy in proxies:
                self._proxies[proxy.id] = proxy

        logger.info(
            "Loaded from database",
            project_count=len(self._projects),
            credential_count=len(self._credentials),
            connector_count=len(self._connectors),
            proxy_count=len(self._proxies),
        )

    async def _hydrate_from_redis(self) -> None:
        """Hydrate proxy and project objects with operational data from Redis and Postgres.

        Combines cumulative totals from Postgres (historical snapshots) with
        the current window from Redis to get accurate total counts.
        """
        logger.info("Hydrating operational data")
        statuses = await self._redis_client.get_all_proxy_statuses()
        redis_proxy_metrics = await self._redis_client.get_all_proxy_metrics()

        # Always load cumulative totals from Postgres
        async with self._session_factory() as session:
            repo = MetricsRepository(session)
            postgres_proxy_metrics = await repo.get_cumulative_metrics_for_all_proxies()
            postgres_project_metrics = await repo.get_cumulative_project_metrics()

        # Hydrate proxy metrics
        for proxy_id, proxy in self._proxies.items():
            if proxy_id in statuses:
                proxy.apply_status_snapshot(statuses[proxy_id])

            # Postgres (historical) + Redis (current window)
            MetricDelta.summed(
                postgres_proxy_metrics.get(proxy_id), redis_proxy_metrics.get(proxy_id)
            ).set_on(proxy)

        # Hydrate project metrics directly on Project objects
        # Combines Postgres (historical) + Redis (current window)
        redis_project_metrics = await self._redis_client.get_all_project_metrics()
        for project_id, project in self._projects.items():
            MetricDelta.summed(
                postgres_project_metrics.get(project_id), redis_project_metrics.get(project_id)
            ).set_on(project)

        # Restore quarantine state from Redis
        await self._rate_limiter.hydrate_from_redis(list(self._proxies.keys()))

        # Traffic usage this period per connector, and the blocks in force.
        connector_ids = list(self._connectors.keys())
        await self._traffic_limiter.hydrate_blocked_from_redis(connector_ids)
        await self._traffic_limiter.refresh(connector_ids)

        logger.info(
            "Hydrated operational data",
            proxy_count=len(self._proxies),
            project_count=len(self._projects),
            from_redis=len(redis_proxy_metrics),
            from_postgres=len(postgres_proxy_metrics),
        )

    async def full_reload(self) -> None:
        """Re-read all definitions from Postgres, merging into the cache.

        Custom provider descriptors are re-synced first so credentials whose
        type was just restored (backup import) or announced on a dropped
        Pub/Sub event resolve to a provider before connectors are reconciled.

        Entries no longer present in Postgres are removed (with Redis +
        rate-limiter cleanup for proxies); new entries are added; existing
        entries have their *definition* fields patched in place so runtime
        state (live status, per-request counters) is preserved. After the
        merge, runtime fields are re-hydrated from Redis as a safety net so
        the cache converges with the cross-instance source of truth.
        """
        await self._provider_store.sync_all()
        for hook in self._reload_hooks:
            try:
                await hook()
            except Exception:
                logger.warning("Reload hook failed", exc_info=True)
        async with self._session_factory() as session:
            project_repo = ProjectRepository(session)
            credential_repo = CredentialRepository(session)
            connector_repo = ConnectorRepository(session)
            proxy_repo = ProxyRepository(session)
            projects = {p.id: p for p in await project_repo.get_all()}
            credentials = {c.id: c for c in await credential_repo.get_all()}
            connectors = {c.id: c for c in await connector_repo.get_all()}
            proxies = {p.id: p for p in await proxy_repo.get_all()}

        # Projects - merge in place, preserving aggregate counters.
        for pid in list(self._projects.keys()):
            if pid not in projects:
                self._projects.pop(pid, None)
                self._project_strategies.pop(pid, None)
                self._pending_project_deltas.pop(pid, None)
        for pid, fresh in projects.items():
            existing = self._projects.get(pid)
            if existing is None:
                self._projects[pid] = fresh
                self._project_strategies[pid] = get_strategy(fresh.routing_strategy)
            else:
                old_strategy = existing.routing_strategy
                existing.merge_definition_from(fresh)
                if old_strategy != fresh.routing_strategy:
                    self._project_strategies[pid] = get_strategy(fresh.routing_strategy)

        # Credentials and connectors have no in-memory runtime state of
        # their own - every field is DB-backed - so an outright replace
        # is fine, but only for entries that actually changed.
        for cid in list(self._credentials.keys()):
            if cid not in credentials:
                self._credentials.pop(cid, None)
        self._credentials.update(credentials)

        for cid in list(self._connectors.keys()):
            if cid not in connectors:
                self._connectors.pop(cid, None)
                self._forget_connector_routing_state(cid)
                self._traffic_limiter.forget_local(cid)
                self._pending_connector_deltas.pop(cid, None)
        self._connectors.update(connectors)

        # Proxies - patch in place to keep request counters, status, and
        # last_check_latency_ms from being clobbered by Pydantic defaults.
        removed_proxy_ids = [pid for pid in self._proxies if pid not in proxies]
        for pid in removed_proxy_ids:
            await self._redis_client.delete_proxy_status(pid)
            await self._redis_client.reset_proxy_metrics(pid)
            self._proxies.pop(pid, None)
            self._pending_proxy_deltas.pop(pid, None)
        if removed_proxy_ids:
            await self._rate_limiter.remove_proxies(removed_proxy_ids)
        for pid, fresh_proxy in proxies.items():
            existing_proxy = self._proxies.get(pid)
            if existing_proxy is None:
                self._proxies[pid] = fresh_proxy
            else:
                existing_proxy.merge_definition_from(fresh_proxy)
                self._proxies.reindex(pid)

        # Flush our own pending deltas first so Redis has them before
        # we read it back. Otherwise the upcoming ``_hydrate_from_redis``
        # would overwrite local in-memory counters with stale
        # cluster-wide totals that don't yet include the requests this
        # instance has accumulated since the last flush.
        await self._flush_pending_metrics()

        # Re-hydrate runtime state (status, metrics) from Redis so this
        # instance's view converges with the cross-instance source of truth.
        await self._hydrate_from_redis()

        logger.debug(
            "Full reload complete",
            projects=len(self._projects),
            credentials=len(self._credentials),
            connectors=len(self._connectors),
            proxies=len(self._proxies),
        )

    async def apply_imported_state(
        self,
        old_project_ids: list[str],
        old_proxy_ids: list[str],
        old_connector_ids: list[str] | None = None,
    ) -> None:
        """Reconcile in-memory + Redis state after a backup import replaced the DB.

        A replace-import wipes every row and restores new ones, so the Redis
        operational keys for the pre-import entities are now stale and would
        cause the metrics flusher to insert against deleted ids. Purge them
        (mirroring ``remove_project``), drop our own un-flushed metric deltas
        for the old ids, then rebuild the cache from the freshly imported DB.
        Other instances converge via the 60s periodic full reload.
        """
        for proxy_id in old_proxy_ids:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)
        for project_id in old_project_ids:
            await self._redis_client.reset_project_metrics(project_id)
            await self._redis_client.clear_mitm_requests(project_id)
        await self._rate_limiter.remove_proxies(old_proxy_ids)

        for connector_id in old_connector_ids or []:
            await self._redis_client.reset_connector_metrics(connector_id)
            await self._traffic_limiter.forget(connector_id)

        # Discard pending deltas keyed by now-deleted ids so the next flush
        # does not resurrect stale metrics in Redis.
        self._pending_proxy_deltas.clear()
        self._pending_project_deltas.clear()
        self._pending_connector_deltas.clear()

        await self.full_reload()
        logger.info(
            "Applied imported state",
            old_projects=len(old_project_ids),
            old_proxies=len(old_proxy_ids),
        )

    async def _periodic_full_reload_loop(self, interval_seconds: int = 60) -> None:
        """Background safety-net: periodically re-sync the cache from Postgres."""
        job_stats.declare_interval(WorkerName.FULL_RELOAD, interval_seconds)
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    break
                with job_stats.track(WorkerName.FULL_RELOAD):
                    await self.full_reload()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Periodic full reload failed", exc_info=True)

    async def _flush_pending_metrics(self) -> bool:
        """Drain accumulated metric deltas to Redis and announce to peers.

        Returns True if there was a batch to flush, False if the buffers were
        empty - which is every cycle on an instance taking no traffic, and what
        the publisher loop reports as an idle run.

        Operation:

        1. Swap the pending dicts out atomically (Python assignment is
           atomic between awaits) so new requests accumulate into a
           fresh dict and we own the snapshot.
        2. Apply the snapshot to Redis in a single pipelined batch.
           If that fails, merge the snapshot back into the pending
           dicts so the next iteration retries.
        3. Publish the same snapshot on the ``METRIC_DELTAS_CHANNEL``
           Pub/Sub channel so peer instances can update their own
           in-memory counters without reading Redis. Pub/Sub is
           best-effort; the 60s ``full_reload`` is the safety net for
           dropped messages.
        """
        if (
            not self._pending_proxy_deltas
            and not self._pending_project_deltas
            and not self._pending_connector_deltas
        ):
            return False

        proxy_deltas = self._pending_proxy_deltas
        project_deltas = self._pending_project_deltas
        connector_deltas = self._pending_connector_deltas
        self._pending_proxy_deltas = {}
        self._pending_project_deltas = {}
        self._pending_connector_deltas = {}

        try:
            await self._redis_client.flush_metric_deltas(
                proxy_deltas, project_deltas, connector_deltas
            )
        except Exception:
            logger.warning(
                "Failed to flush metric deltas; merging back into pending",
                exc_info=True,
            )
            for pid, d in proxy_deltas.items():
                self._pending_proxy_deltas.setdefault(pid, MetricDelta()).merge(d)
            for pid, d in project_deltas.items():
                self._pending_project_deltas.setdefault(pid, MetricDelta()).merge(d)
            for cid, d in connector_deltas.items():
                self._pending_connector_deltas.setdefault(cid, MetricDelta()).merge(d)
            # Still a working cycle: there was a batch, and the retry carries
            # it. Only an empty buffer counts as idle.
            return True

        # Apply locally now that Redis is consistent. We use the same
        # code path peers will run when they receive the Pub/Sub
        # message below, so every instance updates its in-memory view
        # at the same logical moment instead of the local one leading
        # peers between request handling and propagation.
        self._apply_peer_metric_deltas(proxy_deltas, project_deltas)
        # Our own connector bytes were counted as they happened; they only
        # change column, from unflushed to known.
        self._traffic_limiter.mark_flushed(connector_deltas)

        try:
            payload = json.dumps(
                {
                    "instance_id": self._settings.instance_id,
                    "proxy_deltas": MetricDelta.dump_many(proxy_deltas),
                    "project_deltas": MetricDelta.dump_many(project_deltas),
                    "connector_deltas": MetricDelta.dump_many(connector_deltas),
                }
            )
            await self._redis_client.client.publish(METRIC_DELTAS_CHANNEL, payload)
        except Exception:
            logger.warning(
                "Failed to publish metric deltas to peers (Redis already updated; "
                "cluster will converge via the 60s safety reload)",
                exc_info=True,
            )
        return True

    async def _metric_delta_publisher_loop(self, interval_seconds: float = 5.0) -> None:
        """Periodically move accumulated metric deltas into Redis.

        This is the in-process-to-Redis half of the metrics pipeline and runs
        on every instance. The Redis-to-Postgres half is ``MetricsFlusher``,
        which is leader-elected and writes the history rows.

        Default cadence is 5s - fast enough that the UI feels live,
        slow enough that the Redis pipeline batches many requests
        into a single round-trip. On a busy host this turns 10k
        per-request Redis writes per second into one batched write
        every 5s carrying 50k aggregated increments.
        """
        job_stats.declare_interval(WorkerName.METRIC_DELTA_PUBLISHER, interval_seconds)
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    break
                with job_stats.track(WorkerName.METRIC_DELTA_PUBLISHER) as run:
                    if not await self._flush_pending_metrics():
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Periodic metric flush failed", exc_info=True)

    async def _metric_delta_subscriber_loop(self) -> None:
        """Listen for peer instances' metric deltas and apply them to in-memory.

        Drops self-echoes by ``instance_id``. The applied delta updates
        in-memory ``Proxy`` / ``Project`` counters with count-weighted
        latency, see ``MetricDelta.apply_to``. Reconnects on transient Redis failures
        so a brief blip doesn't silently mute cross-instance updates.
        """
        my_id = self._settings.instance_id
        while self._running:
            try:
                pubsub = self._redis_client.client.pubsub()
                await pubsub.subscribe(METRIC_DELTAS_CHANNEL)
                try:
                    async for message in pubsub.listen():
                        if not self._running:
                            break
                        if message.get("type") != "message":
                            continue
                        try:
                            data = message.get("data")
                            if isinstance(data, bytes):
                                data = data.decode("utf-8")
                            payload = json.loads(data)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            logger.debug("Skipping malformed metric-delta message")
                            continue
                        if payload.get("instance_id") == my_id:
                            continue
                        # A "run" for a subscriber is one peer message applied;
                        # the loop itself just waits on the socket.
                        with job_stats.track(WorkerName.METRIC_DELTA_SUBSCRIBER):
                            self._apply_peer_metric_deltas(
                                MetricDelta.parse_many(payload.get("proxy_deltas")),
                                MetricDelta.parse_many(payload.get("project_deltas")),
                            )
                            await self._traffic_limiter.apply_peer(
                                MetricDelta.parse_many(payload.get("connector_deltas"))
                            )
                finally:
                    with contextlib.suppress(Exception):
                        await pubsub.unsubscribe(METRIC_DELTAS_CHANNEL)
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Metric-delta subscriber failed, reconnecting", exc_info=True)
                await asyncio.sleep(1.0)

    def _apply_peer_metric_deltas(
        self,
        proxy_deltas: dict[str, MetricDelta],
        project_deltas: dict[str, MetricDelta],
    ) -> None:
        """Fold peer deltas into local in-memory counters."""
        for proxy_id, delta in proxy_deltas.items():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                delta.apply_to(proxy)
        for project_id, delta in project_deltas.items():
            project = self._projects.get(project_id)
            if project is not None:
                delta.apply_to(project)

    async def reload_project(self, project_id: str) -> None:
        """Re-read a project from Postgres into the cache.

        If the project no longer exists, it is removed. Otherwise its
        definition fields are patched in place onto the existing cached
        ``Project`` so accumulated per-request counters survive the reload.
        """
        async with self._session_factory() as session:
            fresh = await ProjectRepository(session).get_by_id(project_id)
        if fresh is None:
            self._projects.pop(project_id, None)
            self._project_strategies.pop(project_id, None)
            logger.info("Reload removed project from cache", project_id=project_id)
            return
        existing = self._projects.get(project_id)
        if existing is None:
            self._projects[project_id] = fresh
            self._project_strategies[project_id] = get_strategy(fresh.routing_strategy)
        else:
            old_strategy = existing.routing_strategy
            existing.merge_definition_from(fresh)
            if old_strategy != fresh.routing_strategy:
                self._project_strategies[project_id] = get_strategy(fresh.routing_strategy)
        logger.debug("Reloaded project", project_id=project_id)

    async def reload_credential(self, credential_id: str) -> None:
        """Re-read a credential from Postgres into the cache."""
        async with self._session_factory() as session:
            credential = await CredentialRepository(session).get_by_id(credential_id)
        if credential is None:
            self._credentials.pop(credential_id, None)
            logger.info("Reload removed credential from cache", credential_id=credential_id)
            return
        self._credentials[credential_id] = credential
        logger.debug("Reloaded credential", credential_id=credential_id)

    async def reload_connector(self, connector_id: str) -> None:
        """Re-read a connector from Postgres into the cache."""
        async with self._session_factory() as session:
            connector = await ConnectorRepository(session).get_by_id(connector_id)
        if connector is None:
            self._connectors.pop(connector_id, None)
            self._forget_connector_routing_state(connector_id)
            logger.info("Reload removed connector from cache", connector_id=connector_id)
            return
        self._connectors[connector_id] = connector
        logger.debug("Reloaded connector", connector_id=connector_id)

    async def refresh_proxy_status(self, proxy_id: str) -> None:
        """Re-read one proxy's health fields from Redis into the cache.

        The status-only half of :meth:`reload_proxy`, for the case where the
        proxies row is known not to have changed - a peer's health check
        flipping the proxy between healthy, degraded and unhealthy. Redis is
        authoritative for those fields (see ``Proxy.apply_status_snapshot``),
        so this costs one Redis read and no database round-trip.

        A proxy this instance has not cached is ignored: it has no definition
        to attach the status to, and the caller reloads it in full instead.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            return
        status_data = await self._redis_client.get_proxy_status(proxy_id)
        if status_data:
            proxy.apply_status_snapshot(status_data)
        logger.debug("Refreshed proxy status", proxy_id=proxy_id, status=proxy.status)

    async def reload_proxy(self, proxy_id: str) -> None:
        """Re-read a proxy from Postgres + Redis status into the cache.

        If the proxy no longer exists in Postgres, removes it and cleans
        up the matching Redis + rate-limiter state. Otherwise the proxy's
        *definition* fields are patched in place onto the existing cached
        ``Proxy`` and ``status`` / ``last_check_latency_ms`` /
        ``consecutive_failures`` are refreshed from Redis. Per-request
        counters (``request_count`` and friends) are deliberately left
        untouched: they are updated by ``_handle_request_stats`` per
        request, and replacing them here would zero them out every time a
        peer publishes ``proxy_changed`` - causing UI stats to flap.
        """
        async with self._session_factory() as session:
            fresh = await ProxyRepository(session).get_by_id(proxy_id)
        if fresh is None:
            if proxy_id in self._proxies:
                await self._redis_client.delete_proxy_status(proxy_id)
                await self._redis_client.reset_proxy_metrics(proxy_id)
                await self._rate_limiter.remove_proxy(proxy_id)
                del self._proxies[proxy_id]
                logger.info("Reload removed proxy from cache", proxy_id=proxy_id)
            return

        status_data = await self._redis_client.get_proxy_status(proxy_id)
        existing = self._proxies.get(proxy_id)
        if existing is None:
            # First time we see this proxy - take the fresh entity and
            # apply Redis status. Counters start at zero (the model
            # default), and converge upward via ``_hydrate_from_redis``
            # on the next periodic full reload.
            if status_data:
                fresh.apply_status_snapshot(status_data)
            self._proxies[proxy_id] = fresh
        else:
            existing.merge_definition_from(fresh)
            self._proxies.reindex(proxy_id)
            if status_data:
                existing.apply_status_snapshot(status_data)
        logger.debug("Reloaded proxy", proxy_id=proxy_id)

    @property
    def provider_registry(self) -> ProviderRegistry:
        """The provider registry this manager resolves credential types against."""
        return self._provider_registry

    @property
    def provider_store(self) -> ProviderStore:
        return self._provider_store

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        """Handles for the long-running loops started by :meth:`start`."""
        return list(self._tasks)

    def cache_sizes(self) -> dict[str, int]:
        """Entry counts of every in-memory cache this instance holds.

        Postgres remains authoritative; these are what *this* process has
        resident, so a gap against the database counts means a reload is
        overdue (or a peer has written something we have not seen yet).
        """
        return {
            "projects": len(self._projects),
            "credentials": len(self._credentials),
            "connectors": len(self._connectors),
            "proxies": len(self._proxies),
            "project_strategies": len(self._project_strategies),
            "geo_provision_locks": len(self._geo_provision_locks),
            "pending_proxy_deltas": len(self._pending_proxy_deltas),
            "pending_project_deltas": len(self._pending_project_deltas),
            "pending_connector_deltas": len(self._pending_connector_deltas),
            "quarantined_proxies": self._rate_limiter.active_quarantine_count,
            "traffic_blocked_connectors": self._traffic_limiter.blocked_count,
        }

    @property
    def rate_limiter(self) -> RateLimiter:
        """Get the rate limiter instance."""
        return self._rate_limiter

    @property
    def traffic_limiter(self) -> TrafficLimiter:
        """Per-connector traffic usage and limits."""
        return self._traffic_limiter

    def is_traffic_blocked(self, connector_id: str) -> bool:
        """Whether the connector takes no new requests because its traffic limit was reached."""
        return self._traffic_limiter.is_blocked(connector_id)

    def traffic_usage(self, connector: Connector) -> TrafficUsage:
        """The connector's traffic this period against its limit and price."""
        return self._traffic_limiter.usage(connector)

    @property
    def proxies(self) -> list[Proxy]:
        """Get all proxies."""
        return list(self._proxies.values())

    @property
    def healthy_proxies(self) -> list[Proxy]:
        """Get only healthy proxies."""
        return [p for p in self._proxies.values() if p.status == ProxyStatus.HEALTHY]

    @property
    def credentials(self) -> list[Credential]:
        """Get all credentials, ordered by creation time."""
        return sorted(self._credentials.values(), key=lambda c: c.created_at)

    @property
    def connectors(self) -> list[Connector]:
        """Get all connectors, ordered by creation time."""
        return sorted(self._connectors.values(), key=lambda c: c.created_at)

    @property
    def projects(self) -> list[Project]:
        """Get all projects, ordered by creation time."""
        return sorted(self._projects.values(), key=lambda p: p.created_at)

    def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        return self._projects.get(project_id)

    def get_project_by_username(self, username: str) -> Project | None:
        """Get a project by proxy username (for authentication)."""
        for project in self._projects.values():
            if project.username == username:
                return project
        return None

    async def add_project(self, project: Project) -> None:
        """Add a project (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.create(project)
            await session.commit()

        self._projects[project.id] = project
        self._project_strategies[project.id] = get_strategy(project.routing_strategy)
        logger.info("Added project", project_id=project.id, name=project.name)
        await event_bus.publish(project_changed, self, entity_id=project.id, op="added")

    async def update_project(self, project: Project) -> None:
        """Update a project (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.update(project)
            await session.commit()

        self._projects[project.id] = project
        self._project_strategies[project.id] = get_strategy(project.routing_strategy)
        logger.info("Updated project", project_id=project.id, name=project.name)
        await event_bus.publish(project_changed, self, entity_id=project.id, op="updated")

    async def remove_project(self, project_id: str) -> bool:
        """Remove a project (deletes from Postgres, cascades to credentials, connectors and proxies).

        Also cleans up Redis data (project metrics and all associated proxy data)
        to prevent foreign key violations in the metrics flusher.
        """
        if project_id not in self._projects:
            return False

        # Collect proxy IDs before deletion for Redis cleanup
        connector_ids_to_remove = [
            cid for cid, c in self._connectors.items() if c.project_id == project_id
        ]
        proxy_ids_to_remove = [
            pid for pid, p in self._proxies.items()
            if p.connector_id in connector_ids_to_remove
        ]

        async with self._session_factory() as session:
            repo = ProjectRepository(session)
            await repo.delete(project_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for deleted proxies/project (which would cause foreign key violations)
        for proxy_id in proxy_ids_to_remove:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)
        await self._redis_client.reset_project_metrics(project_id)
        await self._redis_client.clear_mitm_requests(project_id)

        # Clean up rate limiter state (in-memory + Redis quarantine keys)
        await self._rate_limiter.remove_proxies(proxy_ids_to_remove)

        # Remove from cache
        del self._projects[project_id]
        self._pending_project_deltas.pop(project_id, None)
        if project_id in self._project_strategies:
            del self._project_strategies[project_id]

        # Remove associated credentials from cache
        credential_ids_to_remove = [
            cid for cid, c in self._credentials.items() if c.project_id == project_id
        ]
        for cid in credential_ids_to_remove:
            del self._credentials[cid]

        # Remove associated connectors and proxies from cache
        for cid in connector_ids_to_remove:
            del self._connectors[cid]
            self._forget_connector_routing_state(cid)

        self._proxies.remove_groups(connector_ids_to_remove)

        logger.info("Removed project", project_id=project_id)
        await event_bus.publish(project_changed, self, entity_id=project_id, op="removed")
        return True

    # Credential methods
    def get_credentials_for_project(self, project_id: str) -> list[Credential]:
        """Get all credentials for a project, ordered by creation time."""
        credentials = [c for c in self._credentials.values() if c.project_id == project_id]
        return sorted(credentials, key=lambda c: (c.created_at, c.id))

    def get_credential(self, credential_id: str) -> Credential | None:
        """Get a credential by ID."""
        return self._credentials.get(credential_id)

    def _build_credential_context(self, proxy: Proxy) -> dict[str, str]:
        """Build a context dictionary for resolving credential placeholders.

        The context contains all values that can be substituted into
        proxy username/password placeholders. It merges all string values
        from both the credential config and connector config.

        Args:
            proxy: The proxy to build context for.

        Returns:
            Dictionary with all string config values from credential and
            connector that can be used to resolve placeholders.
        """
        context: dict[str, str] = {}

        # Get connector for this proxy
        connector = self._connectors.get(proxy.connector_id)
        if not connector:
            return context

        # Get credential for this connector
        credential = self._credentials.get(connector.credential_id)
        if not credential:
            return context

        # Add all string values from credential config
        for key, value in credential.config.items():
            if isinstance(value, str) and value:
                context[key] = value

        # Add all string values from connector config (may override credential values)
        if connector.config:
            for key, value in connector.config.items():
                if isinstance(value, str) and value:
                    context[key] = value

        return context

    def resolve_proxy_credentials(self, proxy: Proxy) -> Proxy:
        """Resolve credential placeholders in proxy username/password.

        Creates a copy of the proxy with placeholders like {username},
        {password}, {customer_id}, {zone_password} replaced with actual
        values from the credential/connector chain.

        Args:
            proxy: The proxy with potential placeholders in credentials.

        Returns:
            A copy of the proxy with resolved credentials.
        """
        # Build context for placeholder resolution
        context = self._build_credential_context(proxy)

        if not context:
            # No context available, return proxy as-is
            return proxy

        # Check if any placeholders need resolution
        username = proxy.username
        password = proxy.password
        needs_resolution = False

        if username and "{" in username:
            needs_resolution = True
        if password and "{" in password:
            needs_resolution = True

        if not needs_resolution:
            return proxy

        # Create a copy of the proxy with resolved credentials
        resolved_proxy = proxy.model_copy()

        if username:
            for key, value in context.items():
                username = username.replace(f"{{{key}}}", value)
            resolved_proxy.username = username

        if password:
            for key, value in context.items():
                password = password.replace(f"{{{key}}}", value)
            resolved_proxy.password = password

        return resolved_proxy

    async def add_credential(self, credential: Credential) -> None:
        """Add a credential (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.create(credential)
            await session.commit()

        self._credentials[credential.id] = credential
        logger.info("Added credential", credential_id=credential.id, name=credential.name)
        await event_bus.publish(credential_changed, self, entity_id=credential.id, op="added")

    async def update_credential(self, credential: Credential) -> None:
        """Update a credential (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.update(credential)
            await session.commit()

        self._credentials[credential.id] = credential
        logger.info("Updated credential", credential_id=credential.id, name=credential.name)
        await event_bus.publish(credential_changed, self, entity_id=credential.id, op="updated")

    async def remove_credential(self, credential_id: str) -> bool:
        """Remove a credential (deletes from Postgres)."""
        if credential_id not in self._credentials:
            return False

        async with self._session_factory() as session:
            repo = CredentialRepository(session)
            await repo.delete(credential_id)
            await session.commit()

        del self._credentials[credential_id]
        logger.info("Removed credential", credential_id=credential_id)
        await event_bus.publish(credential_changed, self, entity_id=credential_id, op="removed")
        return True

    def get_connectors_for_credential(self, credential_id: str) -> list[Connector]:
        """Get all connectors using a specific credential, ordered by creation time."""
        connectors = [c for c in self._connectors.values() if c.credential_id == credential_id]
        return sorted(connectors, key=lambda c: c.created_at)

    # Connector methods
    def get_connectors_for_project(self, project_id: str) -> list[Connector]:
        """Get all connectors for a project, ordered by creation time."""
        connectors = self._connectors.for_project(project_id)
        return sorted(connectors, key=lambda c: (c.created_at, c.id))

    def get_connector(self, connector_id: str) -> Connector | None:
        """Get a connector by ID."""
        return self._connectors.get(connector_id)

    def is_connector_enabled(self, connector_id: str) -> bool:
        """Check if a connector is enabled."""
        connector = self._connectors.get(connector_id)
        return connector.enabled if connector else False

    async def add_connector(self, connector: Connector) -> None:
        """Add a connector (persists to Postgres)."""
        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.create(connector)
            await session.commit()

        self._connectors[connector.id] = connector
        logger.info("Added connector", connector_id=connector.id, name=connector.name)
        await event_bus.publish(connector_changed, self, entity_id=connector.id, op="added")

    async def update_connector(self, connector: Connector) -> None:
        """Update a connector (persists to Postgres)."""
        # Check if rate limit config changed before persisting
        old_connector = self._connectors.get(connector.id)
        rate_limit_changed = (
            old_connector is not None
            and old_connector.rate_limit_config != connector.rate_limit_config
        )

        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.update(connector)
            await session.commit()

        # Only clear rate limiter state if the rate limit config actually changed
        if rate_limit_changed:
            proxy_ids = [p.id for p in self._proxies.for_connector(connector.id)]
            self._rate_limiter.clear_connector_proxies(proxy_ids)

        self._connectors[connector.id] = connector
        # If the window or the limit moved, recount from history and re-apply,
        # which lifts a block the new settings no longer justify. The limiter
        # decides, because the route may have edited the cached object in place.
        await self._traffic_limiter.sync_config(connector)
        logger.info("Updated connector", connector_id=connector.id, name=connector.name)
        await event_bus.publish(connector_changed, self, entity_id=connector.id, op="updated")

    async def reset_connector_traffic(self, connector_id: str) -> Connector | None:
        """Start the connector's usage over from now; history is kept, only the count restarts."""
        connector = self._connectors.get(connector_id)
        if connector is None:
            return None
        connector.traffic_reset_at = utc_now()
        await self.update_connector(connector)
        logger.info(
            "Connector traffic usage reset",
            connector_id=connector_id,
            name=connector.name,
        )
        return connector

    async def update_connector_error(
        self,
        connector_id: str,
        error: str | None,
        consecutive_errors: int,
    ) -> None:
        """Update a connector's error state (persists to Postgres).

        Args:
            connector_id: The connector ID to update.
            error: The error message, or None to clear the error.
            consecutive_errors: The count of consecutive errors.
        """
        from api.core import utc_now

        connector = self._connectors.get(connector_id)
        if not connector:
            logger.warning(
                "Cannot update error for unknown connector",
                connector_id=connector_id,
            )
            return

        # Update error fields
        connector.last_error = error
        connector.last_error_at = utc_now() if error else None
        connector.consecutive_errors = consecutive_errors

        # Persist to database
        async with self._session_factory() as session:
            repo = ConnectorRepository(session)
            await repo.update(connector)
            await session.commit()

        self._connectors[connector.id] = connector

        if error:
            logger.warning(
                "Connector error recorded",
                connector_id=connector_id,
                error=error,
                consecutive_errors=consecutive_errors,
            )
        else:
            logger.info(
                "Connector error cleared",
                connector_id=connector_id,
            )
        await event_bus.publish(connector_changed, self, entity_id=connector_id, op="updated")

    async def remove_connector(self, connector_id: str) -> bool:
        """Remove a connector (deletes from Postgres, cascades to proxies).

        Also cleans up Redis data for all associated proxies to prevent
        foreign key violations in the metrics flusher.
        """
        if connector_id not in self._connectors:
            return False

        # Collect proxy IDs before deletion for Redis cleanup
        proxy_ids_to_remove = [
            pid for pid, p in self._proxies.items()
            if p.connector_id == connector_id
        ]

        async with self._session_factory() as session:
            # Database has ON DELETE CASCADE on proxies.connector_id,
            # so deleting the connector will automatically delete its proxies
            repo = ConnectorRepository(session)
            await repo.delete(connector_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for deleted proxies (which would cause foreign key violations)
        for proxy_id in proxy_ids_to_remove:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)

        # Clean up rate limiter state (in-memory + Redis quarantine keys)
        await self._rate_limiter.remove_proxies(proxy_ids_to_remove)

        # The connector's own metrics hash would otherwise be flushed against
        # a row that no longer exists; its block key and usage go with it.
        await self._redis_client.reset_connector_metrics(connector_id)
        await self._traffic_limiter.forget(connector_id)

        # Remove from cache
        del self._connectors[connector_id]
        # Only now: while the awaits above yielded, a transfer still running
        # could report progress and re-create the delta for a connector whose
        # row is already gone.
        self._pending_connector_deltas.pop(connector_id, None)
        self._forget_connector_routing_state(connector_id)
        # Also remove associated proxies from cache
        self._proxies.remove_groups([connector_id])
        logger.info("Removed connector", connector_id=connector_id)
        await event_bus.publish(connector_changed, self, entity_id=connector_id, op="removed")
        return True

    async def delete_connector_async(self, connector_id: str) -> bool:
        """Delete a connector, handling cloud instances appropriately.

        For cloud connectors (AWS, GCP, Azure): marks all proxies as TERMINATING
        and disables the connector. The auto-scaler will handle instance
        termination and the connector will be cleaned up when all proxies are gone.

        For non-cloud connectors: directly removes the connector and its proxies.

        Args:
            connector_id: The connector ID to delete.

        Returns:
            True if the connector was found and deletion initiated,
            False if connector not found.
        """
        connector = self._connectors.get(connector_id)
        if not connector:
            return False

        credential = self.get_credential(connector.credential_id)

        # Check if this is a cloud provider connector
        if credential and self._provider_registry.is_cloud(credential.type):
            # Mark all proxies as terminating - auto-scaler will handle termination
            proxies = self.get_proxies_for_connector(connector_id)
            for proxy in proxies:
                await self.mark_proxy_terminating(proxy.id)

            # Disable the connector and mark for deletion so auto-scaler knows to clean it up
            # The connector will be removed once all proxies are terminated
            connector.enabled = False
            connector.pending_deletion = True
            await self.update_connector(connector)

            logger.info(
                "Marked cloud connector for deletion",
                connector_id=connector_id,
                proxy_count=len(proxies),
            )
            return True

        # Non-cloud connector: remove directly
        return await self.remove_connector(connector_id)

    def _enabled_connectors(
        self,
        project_id: str,
        target_host: str | None = None,
        *,
        include_traffic_blocked: bool = False,
    ) -> list[Connector]:
        """Enabled connectors of a project that may serve ``target_host``.

        A connector blocked by its traffic limit is left out unless
        ``include_traffic_blocked`` asks for it (to tell "over limit" from
        "nothing configured" when a request finds no proxy).

        Args:
            project_id: The project to get connectors for.
            target_host: If provided, only return connectors whose domain
                routing config allows this host.
        """
        return [
            c for c in self._connectors.for_project(project_id)
            if c.enabled
            and (not target_host or is_domain_allowed(target_host, c.parsed_routing_config))
            and (include_traffic_blocked or not self._traffic_limiter.is_blocked(c.id))
        ]

    def _get_enabled_connector_ids(
        self, project_id: str, target_host: str | None = None
    ) -> set[str]:
        """IDs of the connectors returned by _enabled_connectors."""
        return {c.id for c in self._enabled_connectors(project_id, target_host)}

    # --- Country routing (-cc-<code> username suffix) -------------------------
    #
    # A connector declares the countries its proxies exit from (``countries``
    # on static/cloud connectors, the provider's country field on descriptor
    # connectors). Given a requested country:
    #   - connectors listing countries serve it only if it is listed;
    #   - a proxy with a known exit country (vendor-reported ``country`` or
    #     the ``geo`` it was provisioned for) must match exactly;
    #   - a proxy with no known country is eligible only through its
    #     connector's list;
    #   - descriptor pools whose credentials carry a country and list none
    #     ("All countries") get a slot group for the country on demand;
    #   - a dynamic-sessions gateway row serves any country its connector
    #     allows (every country when it lists none): the code is rendered
    #     into the vendor credentials per request, so nothing is provisioned.
    # Without a requested country every proxy is eligible, except on-demand
    # geo groups of "All countries" pools, which keep serving only the
    # clients that asked for that country.

    def _descriptor_provider(self, connector: Connector) -> DescriptorProvider | None:
        """A DescriptorProvider for the connector, or None for code-implemented types.

        Cached per connector and reused while the connector, credential and
        provider type objects are the very ones the cache saw: every reload
        and update replaces those objects, so a config change misses the cache
        and rebuilds. This runs on the request path (country eligibility and
        dynamic-session rendering), where rebuilding a provider each time
        would be pure allocation.
        """
        credential = self._credentials.get(connector.credential_id)
        if credential is None:
            return None
        ptype = self._provider_registry.get(credential.type)
        if ptype is None or ptype.factory is None:
            return None
        cached = self._provider_cache.get(connector.id)
        if cached is not None and cached[0] is connector and cached[1] is credential and cached[2] is ptype:
            return cached[3]
        try:
            provider = ptype.factory(connector, credential)
        except ValueError:
            self._provider_cache.pop(connector.id, None)
            return None
        if not isinstance(provider, DescriptorProvider):
            return None
        self._provider_cache[connector.id] = (connector, credential, ptype, provider)
        return provider

    def get_connector_target(self, connector: Connector) -> ProxyTarget | None:
        """Intended pool size for a connector, or None when it has no target of its own.

        Cloud connectors scale up to ``max_proxies``; provider connectors
        derive it per country from their descriptor; static connectors hold
        whatever was added to them.
        """
        cloud = connector.cloud_config
        if cloud is not None:
            return ProxyTarget(total=cloud.max_proxies)
        provider = self._descriptor_provider(connector)
        if provider is None:
            return None
        return provider.proxy_target(self.get_proxies_for_connector(connector.id))

    def _accepts_request_country(self, connector: Connector) -> bool:
        """Whether the connector provisions slot groups for unlisted countries on demand."""
        if connector.countries:
            return False
        provider = self._descriptor_provider(connector)
        return provider is not None and provider.accepts_request_country()

    def _eligible_proxies(
        self,
        project_id: str,
        target_host: str | None,
        country: str | None,
        *,
        include_quarantined: bool,
        include_traffic_blocked: bool = False,
    ) -> list[Proxy]:
        """Healthy proxies a request may use, after domain and country filtering.

        Under a ``strict`` location policy the project also refuses proxies
        whose vendor-declared location is contradicted by attribution.
        """
        wanted = country.strip().upper() if country else None
        project = self._projects.get(project_id)
        strict = project is not None and project.location_policy == LocationPolicy.STRICT
        connectors: dict[str, tuple[list[str], bool]] = {}
        for c in self._enabled_connectors(
            project_id, target_host, include_traffic_blocked=include_traffic_blocked
        ):
            declared = c.countries
            if wanted and declared and wanted not in declared:
                continue
            connectors[c.id] = (declared, wanted is None and not declared and self._accepts_request_country(c))

        eligible: list[Proxy] = []
        for p in self._proxies.for_connectors(connectors):
            entry = connectors[p.connector_id]
            if p.status != ProxyStatus.HEALTHY:
                continue
            if not include_quarantined and self._rate_limiter.is_quarantined(p.id):
                continue
            if strict and p.metadata.get(META_LOCATION_CONFLICT) is True:
                continue
            declared, hide_geo_groups = entry
            if is_dynamic_gateway(p):
                # The connector-level allow-list was applied above; the row itself has no country.
                eligible.append(p)
                continue
            proxy_country = p.country
            if wanted:
                if proxy_country is not None:
                    if proxy_country != wanted:
                        continue
                elif wanted not in declared:
                    continue
            elif hide_geo_groups and p.metadata.get(META_GEO):
                continue
            eligible.append(p)
        return eligible

    async def _provision_country_slots(
        self, project_id: str, target_host: str | None, country: str
    ) -> bool:
        """Create slot groups for ``country`` on every eligible "All countries" pool.

        Runs at most once per (connector, country) per instance at a time; a
        Redis lease keeps cluster peers from provisioning the same group. A
        peer that loses the lease waits briefly for the winner's proxies to
        arrive through the cross-instance change feed.
        """
        provisioned = False
        for connector in self._enabled_connectors(project_id, target_host):
            if not self._accepts_request_country(connector):
                continue
            if self._has_geo_group(connector.id, country):
                continue
            lock = self._geo_provision_locks.setdefault((connector.id, country), asyncio.Lock())
            async with lock:
                if self._has_geo_group(connector.id, country):
                    continue
                lease = Lease(
                    self._redis_client,
                    name=f"geo_provision:{connector.id}:{country}",
                    owner_id=self._settings.instance_id,
                )
                if not await lease.try_acquire():
                    await self._wait_for_geo_group(connector.id, country)
                    continue
                try:
                    provider = self._descriptor_provider(connector)
                    if provider is None:
                        continue
                    # Postgres is the authority on what exists. A peer may have
                    # provisioned this group moments ago and released the lease
                    # before its proxy_changed events reached us; provisioning
                    # from the local cache would then create a second group.
                    existing = await self._fetch_connector_proxies(connector.id)
                    for proxy in existing:
                        if proxy.id not in self._proxies:
                            await self.reload_proxy(proxy.id)
                    if any(p.metadata.get(META_GEO) == country for p in existing):
                        logger.debug(
                            "Country slot group already provisioned by a peer",
                            connector_id=connector.id, country=country,
                        )
                        provisioned = True
                        continue
                    to_add = await provider.provision_country(existing, country)
                    for proxy in to_add:
                        await self.add_proxy(proxy)
                    if to_add:
                        provisioned = True
                        logger.info(
                            "Provisioned country slot group on demand",
                            connector_id=connector.id, country=country, slots=len(to_add),
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to provision country slot group",
                        connector_id=connector.id, country=country, error=str(exc),
                    )
                finally:
                    await lease.release()
        return provisioned

    async def _fetch_connector_proxies(self, connector_id: str) -> list[Proxy]:
        """The connector's proxies as persisted in Postgres (authoritative across the cluster)."""
        async with self._session_factory() as session:
            return await ProxyRepository(session).get_by_connector(connector_id)

    def _has_geo_group(self, connector_id: str, country: str) -> bool:
        return any(p.metadata.get(META_GEO) == country for p in self._proxies.for_connector(connector_id))

    async def _wait_for_geo_group(self, connector_id: str, country: str, timeout: float = 3.0) -> None:
        """Poll the local cache until a peer's slot group for ``country`` shows up (or timeout)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._has_geo_group(connector_id, country):
                return
            await asyncio.sleep(0.2)

    def get_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project (via enabled connectors only)."""
        return self._proxies.for_connectors(self._get_enabled_connector_ids(project_id))

    def get_all_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project, including those from disabled connectors."""
        return self._proxies.for_connectors(c.id for c in self._connectors.for_project(project_id))

    def get_healthy_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Every healthy, non-quarantined proxy of the project's enabled connectors.

        This is the pool-health view used by the API and the dashboard. It
        applies no routing rules: on-demand country groups count as healthy
        here even though untargeted requests do not use them. Routing goes
        through get_routable_proxies_for_project instead.
        """
        return [
            p for p in self._proxies.for_connectors(self._get_enabled_connector_ids(project_id))
            if p.status == ProxyStatus.HEALTHY and not self._rate_limiter.is_quarantined(p.id)
        ]

    def get_routable_proxies_for_project(
        self,
        project_id: str,
        target_host: str | None = None,
        country: str | None = None,
    ) -> list[Proxy]:
        """Healthy proxies a request may be routed to.

        Excludes quarantined proxies (rate-limited) and applies the request's
        constraints:

        Args:
            project_id: The project to get proxies for.
            target_host: If provided, only return proxies from connectors whose
                domain routing config allows this host.
            country: If provided (ISO 3166-1 alpha-2), only return proxies
                that serve this country: proxies whose known exit country
                matches, or unlabelled proxies of a connector that lists it.
                Without it, on-demand country groups of "all countries" pools
                are left out, so untargeted traffic keeps its default exits.
        """
        return self._eligible_proxies(project_id, target_host, country, include_quarantined=False)

    def _is_sticky_quarantine_blocked(
        self, project_id: str, session_id: str | None
    ) -> bool:
        """Check if a sticky session's cached proxy is quarantined and sticky_quarantine is on.

        Returns True when the session should be blocked (429) rather than
        falling back to another proxy.
        """
        if session_id is None:
            return False
        strategy = self._project_strategies.get(project_id, self._strategy)
        if strategy.name != "sticky":
            return False
        session_map: dict[str, str] = getattr(strategy, "_session_map", {})
        cached_proxy_id = session_map.get(session_id)
        if not cached_proxy_id or not self._rate_limiter.is_quarantined(cached_proxy_id):
            return False
        cached_proxy = self._proxies.get(cached_proxy_id)
        if not cached_proxy or cached_proxy.status != ProxyStatus.HEALTHY:
            return False
        connector = self._connectors.get(cached_proxy.connector_id)
        if not connector:
            return False
        rl_config = connector.parsed_rate_limit_config
        return rl_config is not None and rl_config.sticky_quarantine

    def are_all_proxies_quarantined(
        self,
        project_id: str,
        target_host: str | None = None,
        session_id: str | None = None,
        country: str | None = None,
    ) -> bool:
        """Check if proxy selection failed due to quarantine.

        Returns True when either all healthy proxies are quarantined, or
        when sticky_quarantine blocked a specific session's quarantined proxy.
        Used to distinguish 'no proxies exist' (502) from quarantine (429).
        """
        if self._is_sticky_quarantine_blocked(project_id, session_id):
            return True

        healthy = self._eligible_proxies(project_id, target_host, country, include_quarantined=True)
        if not healthy:
            return False
        return all(self._rate_limiter.is_quarantined(p.id) for p in healthy)

    def traffic_limit_status(
        self,
        project_id: str,
        target_host: str | None = None,
        country: str | None = None,
    ) -> int | None:
        """The status to answer with when only traffic-blocked connectors could serve the request.

        None when some connector is still eligible, or when none would be
        even without the blocks (that is a 502, not a limit). With several
        blocked connectors disagreeing on their status, 509 wins: it is the
        one that cannot be mistaken for anything else.
        """
        if self._eligible_proxies(project_id, target_host, country, include_quarantined=True):
            return None
        held_back = self._eligible_proxies(
            project_id, target_host, country, include_quarantined=True, include_traffic_blocked=True
        )
        statuses: set[int] = set()
        for proxy in held_back:
            connector = self._connectors.get(proxy.connector_id)
            if connector is not None and self._traffic_limiter.is_blocked(connector.id):
                statuses.add(self._traffic_limiter.config_for(connector).limit_status)
        if not statuses:
            return None
        return statuses.pop() if len(statuses) == 1 else DEFAULT_TRAFFIC_LIMIT_STATUS

    def get_quarantined_count_for_project(self, project_id: str) -> int:
        """Get the number of quarantined proxies for a project."""
        return sum(
            1 for p in self._proxies.for_connectors(self._get_enabled_connector_ids(project_id))
            if self._rate_limiter.is_quarantined(p.id)
        )

    async def select_proxy_for_project(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
        country: str | None = None,
        exclude: frozenset[str] | None = None,
        sessid: str | None = None,
    ) -> Proxy | None:
        """Select a proxy for a specific project using the project's routing strategy.

        Returns a proxy with resolved credentials (placeholders replaced with
        actual values from the credential/connector chain). ``exclude`` drops
        proxies by id before the strategy runs (preflight retry); a sticky
        session bound to an excluded proxy is re-bound to the pick.

        ``session_id`` is the routing key (the client's ``-sessid-`` or, for
        sticky routing, its address); ``sessid`` is only the explicit
        ``-sessid-`` value. A dynamic-sessions gateway row derives the vendor
        session from ``sessid`` and renders the requested country into its
        credentials before they are resolved; with no ``sessid`` every request
        gets a fresh vendor session.

        When sticky_quarantine is enabled on a connector's rate limit config
        and the project uses sticky routing, a session whose assigned proxy is
        quarantined will get None (triggering 429) instead of falling back to
        a different proxy.

        For sticky-strategy projects with a session_id, looks up the
        session→proxy binding in Redis (cross-instance) before falling back
        to the strategy's local selection. New bindings are persisted to
        Redis with a short TTL so other instances see them on the next
        request.

        Args:
            project_id: The project to select a proxy for.
            session_id: Session identifier for sticky routing.
            target_host: If provided, only consider proxies from connectors
                whose domain routing config allows this host.
            country: If provided, only consider proxies that serve this
                country (see get_routable_proxies_for_project). "All countries"
                provider pools get a slot group for it on first use.
        """
        if self._is_sticky_quarantine_blocked(project_id, session_id):
            return None

        healthy_proxies = self.get_routable_proxies_for_project(project_id, target_host, country)
        if country and not healthy_proxies:
            wanted = country.strip().upper()
            if await self._provision_country_slots(project_id, target_host, wanted):
                healthy_proxies = self.get_routable_proxies_for_project(project_id, target_host, wanted)
        if exclude:
            healthy_proxies = [p for p in healthy_proxies if p.id not in exclude]
        strategy = self._project_strategies.get(project_id, self._strategy)

        # Two steps: the connector by routing weight, then the proxy inside
        # it by the strategy. The strategy handles whatever cross-instance
        # state it needs (sticky reads/writes its Redis binding inside
        # select; others ignore the redis_client/project_id kwargs).
        selected = await strategy.select_weighted(
            self.group_by_connector(healthy_proxies),
            session_id,
            redis_client=self._redis_client,
            project_id=project_id,
        )
        if selected is None:
            return None
        if is_dynamic_gateway(selected):
            selected = self._render_dynamic_request(selected, project_id, sessid, country)
        return self.resolve_proxy_credentials(selected)

    def group_by_connector(self, proxies: list[Proxy]) -> list[ProxyGroup]:
        """Eligible proxies grouped per connector, each group carrying the connector's routing weight.

        Groups keep the order in which their connectors first appear; a
        connector no longer known (mid-removal) gets the default weight.
        """
        groups: dict[str, ProxyGroup] = {}
        for proxy in proxies:
            group = groups.get(proxy.connector_id)
            if group is None:
                connector = self._connectors.get(proxy.connector_id)
                weight = connector.weight if connector is not None else DEFAULT_ROUTING_WEIGHT
                load = sum(p.request_count for p in self._proxies.for_connector(proxy.connector_id))
                group = ProxyGroup(key=proxy.connector_id, weight=weight, load=load)
                groups[proxy.connector_id] = group
            group.proxies.append(proxy)
        return list(groups.values())

    def _forget_connector_routing_state(self, connector_id: str) -> None:
        """Drop per-connector strategy state (round robin cursors) for a removed connector."""
        self._strategy.forget_group(connector_id)
        for strategy in self._project_strategies.values():
            strategy.forget_group(connector_id)

    def _render_dynamic_request(
        self, proxy: Proxy, project_id: str, sessid: str | None, country: str | None
    ) -> Proxy:
        """Credentials for one request through a dynamic-sessions gateway row."""
        connector = self._connectors.get(proxy.connector_id)
        provider = self._descriptor_provider(connector) if connector is not None else None
        if provider is None or not provider.is_dynamic:
            # The row says dynamic but the connector no longer does (mid-sync): use it as stored.
            return proxy
        return provider.render_request(proxy, sessid=sessid, country=country, scope=project_id)

    def set_project_strategy(self, project_id: str, strategy_name: str) -> None:
        """Change the routing strategy for a project."""
        self._project_strategies[project_id] = get_strategy(strategy_name)
        logger.info("Changed project routing strategy", project_id=project_id, strategy=strategy_name)

    def strategy_for_project(self, project_id: str) -> "RoutingStrategy":
        """The routing strategy in force for a project: its own, else the install default."""
        return self._project_strategies.get(project_id, self._strategy)

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """Get a proxy by ID."""
        return self._proxies.get(proxy_id)

    async def add_proxy(self, proxy: Proxy) -> None:
        """Add a proxy to the pool (persists to Postgres).

        Emits proxy_added signal after successful addition.
        """
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.create(proxy)
            await session.commit()

        self._proxies[proxy.id] = proxy
        logger.info("Added proxy", proxy_id=proxy.id, host=proxy.host)

        # Local-only: in-process subscribers (e.g., ProviderSyncer for
        # re-creation logic) attach to proxy_added. Cross-instance receivers
        # use proxy_changed.
        await event_bus.publish(proxy_added,
            self,
            proxy_id=proxy.id,
            connector_id=proxy.connector_id,
        )
        await event_bus.publish(proxy_changed, self, entity_id=proxy.id, op="added")

    async def update_proxy(self, proxy: Proxy) -> None:
        """Update a proxy in the pool (persists to Postgres)."""
        await self.update_proxies([proxy])

    async def update_proxies(self, proxies: list[Proxy]) -> None:
        """Update several proxies in one transaction, then announce each to peers."""
        if not proxies:
            return
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            for proxy in proxies:
                await repo.update(proxy)
            await session.commit()

        for proxy in proxies:
            self._proxies[proxy.id] = proxy
            logger.info("Updated proxy", proxy_id=proxy.id, host=proxy.host)
            await event_bus.publish(proxy_changed, self, entity_id=proxy.id, op="updated")

    async def remove_proxy(self, proxy_id: str) -> bool:
        """Remove a proxy from the pool (deletes from Postgres).

        Emits proxy_removed signal after successful removal.
        Also cleans up Redis data (status and metrics) to prevent
        foreign key violations in the metrics flusher.
        """
        if proxy_id not in self._proxies:
            return False

        proxy = self._proxies[proxy_id]
        connector_id = proxy.connector_id

        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.delete(proxy_id)
            await session.commit()

        # Clean up Redis data to prevent metrics flusher from trying to insert
        # metrics for a deleted proxy (which would cause foreign key violations)
        await self._redis_client.delete_proxy_status(proxy_id)
        await self._redis_client.reset_proxy_metrics(proxy_id)

        # Clean up rate limiter state (in-memory + Redis quarantine key)
        await self._rate_limiter.remove_proxy(proxy_id)

        del self._proxies[proxy_id]
        self._pending_proxy_deltas.pop(proxy_id, None)
        logger.info("Removed proxy", proxy_id=proxy_id)

        await event_bus.publish(proxy_removed,
            self,
            proxy_id=proxy_id,
            connector_id=connector_id,
        )
        await event_bus.publish(proxy_changed, self, entity_id=proxy_id, op="removed")

        return True

    def set_strategy(self, strategy_name: str) -> None:
        """Change the routing strategy."""
        self._strategy = get_strategy(strategy_name)
        logger.info("Changed routing strategy", strategy=strategy_name)

    async def update_proxy_status(
        self,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float = 0.0,
        consecutive_failures: int = 0,
    ) -> None:
        """Update proxy health status (stores in Redis).

        Emits proxy_status_changed signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy:
            old_status = proxy.status
            proxy.status = status
            proxy.last_check_latency_ms = latency_ms
            proxy.consecutive_failures = consecutive_failures

            await self._redis_client.set_proxy_status(
                proxy_id, status, latency_ms, consecutive_failures
            )

            # Emit signal if status actually changed
            if old_status != status:
                await event_bus.publish(proxy_status_changed,
                    self,
                    proxy_id=proxy_id,
                    old_status=old_status,
                    new_status=status,
                )
                # Cross-instance: tell peers to re-hydrate this proxy. The
                # "status" op says the proxies *row* did not change, so peers
                # refresh the three health fields from Redis and skip the
                # Postgres read - this is by far the most frequent
                # cross-instance event, and on a flapping pool the reads it
                # used to cost were the bulk of the peers' database traffic.
                await event_bus.publish(
                    proxy_changed, self, entity_id=proxy_id, op="status"
                )

    # Demand tracking and scaling methods

    @property
    def demand_tracker(self) -> DemandTracker:
        """Get the demand tracker instance."""
        return self._demand_tracker

    async def get_demand_info(self, project_id: str) -> dict[str, Any]:
        """Get demand level and instance counts for a project.

        Returns:
            Dict with demand_level, requests_per_minute, current/min/max instances,
            and counts of draining/terminating instances.
        """
        project = self._projects.get(project_id)
        if not project:
            return {}

        # Get all proxies for the project
        proxies = self.get_proxies_for_project(project_id)
        healthy_proxies = self.get_healthy_proxies_for_project(project_id)

        # Count proxies by status
        draining_count = sum(1 for p in proxies if p.status == ProxyStatus.DRAINING)
        terminating_count = sum(1 for p in proxies if p.status == ProxyStatus.TERMINATING)

        # Get demand info from tracker
        demand_info = await self._demand_tracker.get_demand_info(
            project_id, len(healthy_proxies)
        )

        # Get min/max from connectors (aggregate across all cloud connectors)
        min_instances = 0
        max_instances = 0
        for connector in self._connectors.values():
            if connector.project_id == project_id and connector.enabled:
                cloud_config = connector.cloud_config
                if cloud_config:
                    min_instances += cloud_config.min_proxies
                    max_instances += cloud_config.max_proxies

        return {
            "demand_level": demand_info["demand_level"].value,
            "requests_per_minute": demand_info["requests_per_minute"],
            "rate_per_proxy": demand_info["rate_per_proxy"],
            "current_instances": len(proxies),
            "healthy_instances": len(healthy_proxies),
            "min_instances": min_instances,
            "max_instances": max_instances,
            "draining_instances": draining_count,
            "terminating_instances": terminating_count,
        }

    async def start_proxy_draining(self, proxy_id: str) -> bool:
        """Mark a proxy as draining - stop routing new requests to it.

        Args:
            proxy_id: The proxy ID to start draining.

        Returns:
            True if successful, False if proxy not found.

        Emits proxy_draining_started signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        proxy.status = ProxyStatus.DRAINING
        # Store draining start time in metadata
        from api.core import utc_now

        proxy.metadata["draining_started_at"] = utc_now().isoformat()

        # Update in Redis
        await self._redis_client.set_proxy_status(
            proxy_id, ProxyStatus.DRAINING, proxy.last_check_latency_ms, 0
        )

        # Persist to database
        await self.update_proxy(proxy)

        logger.info("Started draining proxy", proxy_id=proxy_id)

        # Emit signal for subscribers
        await event_bus.publish(proxy_draining_started,
            self,
            proxy_id=proxy_id,
            connector_id=proxy.connector_id,
        )

        return True

    async def mark_proxy_terminating(self, proxy_id: str) -> bool:
        """Mark a proxy as terminating.

        Args:
            proxy_id: The proxy ID to mark as terminating.

        Returns:
            True if successful, False if proxy not found.

        Emits proxy_marked_terminating signal after successful update.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        proxy.status = ProxyStatus.TERMINATING

        # Update in Redis
        await self._redis_client.set_proxy_status(
            proxy_id, ProxyStatus.TERMINATING, 0, 0
        )

        # Persist to database
        await self.update_proxy(proxy)

        logger.info("Marked proxy as terminating", proxy_id=proxy_id)

        # Emit signal for subscribers
        await event_bus.publish(proxy_marked_terminating,
            self,
            proxy_id=proxy_id,
            connector_id=proxy.connector_id,
        )

        return True

    async def delete_proxy_async(self, proxy_id: str) -> bool:
        """Delete a proxy, handling cloud instances appropriately.

        For cloud proxies (AWS, GCP, Azure): marks as TERMINATING so the
        auto-scaler will handle instance termination.

        For non-cloud proxies: directly removes the proxy.

        Args:
            proxy_id: The proxy ID to delete.

        Returns:
            True if the proxy was found and deletion initiated,
            False if proxy not found.
        """
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return False

        # Get connector and credential to check if this is a cloud provider
        connector = self.get_connector(proxy.connector_id)
        if connector:
            credential = self.get_credential(connector.credential_id)

            # Check if this is a cloud provider connector
            if credential and self._provider_registry.is_cloud(credential.type):
                # Mark as terminating - auto-scaler will handle the actual termination
                logger.info(
                    "Marking cloud proxy for termination",
                    proxy_id=proxy_id,
                    credential_type=credential.type,
                )
                return await self.mark_proxy_terminating(proxy_id)

        # Non-cloud proxy: remove directly
        return await self.remove_proxy(proxy_id)

    def get_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a specific connector."""
        return self._proxies.for_connector(connector_id)

    def get_active_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get active (non-draining, non-terminating) proxies for a connector."""
        return [
            p for p in self._proxies.for_connector(connector_id)
            if p.status not in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING)
        ]

