# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy pool manager for Octoprox.

Subscribes to signals from HealthChecker and ProxyServer.
Emits proxy lifecycle signals (proxy_added, proxy_removed, proxy_status_changed).
"""

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.auto_scaler import AutoScaler
from api.core.config import Settings
from api.core.demand_tracker import DemandTracker
from api.core.domain_filter import is_domain_allowed
from api.core.event_bus import EVENT_CHANNEL, RedisPubSubTransport, event_bus
from api.core.health_checker import HealthChecker
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
from api.core.stats import (
    MetricDelta,
    accumulate_delta,
    apply_delta,
    apply_metrics,
    combine_metrics,
    empty_delta,
    merge_delta_into,
)
from api.db.redis import (
    INSTANCE_REGISTRY_KEY,
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
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.strategies import get_strategy

if TYPE_CHECKING:
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
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._settings = settings

        # In-memory cache (loaded from Postgres on start)
        self._projects: dict[str, Project] = {}
        self._proxies: dict[str, Proxy] = {}
        self._credentials: dict[str, Credential] = {}
        self._connectors: dict[str, Connector] = {}
        # Per-project strategies (project_id -> strategy)
        self._project_strategies: dict[str, RoutingStrategy] = {}
        # Default strategy for backward compatibility
        self._strategy: RoutingStrategy = get_strategy(settings.default_strategy)
        self._health_checker = HealthChecker(self, redis_client, settings.instance_id)
        self._metrics_flusher = MetricsFlusher(session_factory, redis_client, settings)
        self._metrics_compactor = MetricsCompactor(
            session_factory, redis_client, settings.instance_id
        )
        self._demand_tracker = DemandTracker(redis_client)
        self._auto_scaler = AutoScaler(self, redis_client, settings.instance_id)
        self._provider_syncer = ProxyProviderSyncer(
            self, redis_client, settings.instance_id
        )
        self._rate_limiter = RateLimiter(redis_client)
        # Pending metric deltas accumulated since the last flush. The
        # per-request handler bumps local in-memory counters AND
        # appends here; ``_periodic_metric_flush_loop`` drains both
        # dicts in a single Redis pipeline and announces the same
        # deltas on Pub/Sub so peers can update without polling.
        self._pending_proxy_deltas: dict[str, MetricDelta] = {}
        self._pending_project_deltas: dict[str, MetricDelta] = {}
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

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
            credential_changed,
            project_changed,
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
            ],
        )

        # Load data from Postgres into memory cache
        await self._load_from_database()

        # Hydrate with Redis operational data
        await self._hydrate_from_redis()

        # Start health checker
        task = asyncio.create_task(self._health_checker.run())
        self._tasks.append(task)

        # Start metrics flusher
        task = asyncio.create_task(self._metrics_flusher.run())
        self._tasks.append(task)

        # Start metrics compactor (compaction + retention)
        task = asyncio.create_task(self._metrics_compactor.run())
        self._tasks.append(task)

        # Start auto-scaler
        task = asyncio.create_task(self._auto_scaler.run())
        self._tasks.append(task)

        # Start provider syncer (handles all proxy provider types)
        task = asyncio.create_task(self._provider_syncer.run())
        self._tasks.append(task)

        # Advertise this instance's presence so future phases can discover
        # peers (cross-instance event fanout, sharded health checks, leases).
        task = asyncio.create_task(self._heartbeat_loop())
        self._tasks.append(task)

        # Safety-net reload from Postgres in case cross-instance invalidation
        # events get dropped (Redis Pub/Sub is best-effort).
        task = asyncio.create_task(self._periodic_full_reload_loop())
        self._tasks.append(task)

        # Drain accumulated request-metric deltas to Redis (batched) and
        # announce them on Pub/Sub so peers update their in-memory view.
        # Keeps the hot path free of per-request Redis writes.
        task = asyncio.create_task(self._periodic_metric_flush_loop())
        self._tasks.append(task)

        # Receive peer instances' metric deltas and fold them into local
        # in-memory counters.
        task = asyncio.create_task(self._metric_delta_subscriber_loop())
        self._tasks.append(task)

        # Subscribe to cross-instance cache-invalidation events.
        task = asyncio.create_task(self._cross_instance_subscriber_loop())
        self._tasks.append(task)

    async def _cross_instance_subscriber_loop(self) -> None:
        """Listen on the EventBus distributed channel and reload entities.

        Drops self-echoes by ``instance_id``, then dispatches by the
        signal's own name (no string duplication — the signal objects are
        the source of truth). The handler receives ``(entity_id, op)`` so
        it can short-circuit on "removed" without a wasted DB read.
        Reconnects on failure so a brief Redis hiccup does not silently
        mute cross-instance updates.
        """
        from api.core.signals import (
            connector_changed,
            credential_changed,
            project_changed,
            proxy_changed,
            proxy_quarantine_changed,
        )

        dispatch: dict[str, Any] = {
            project_changed.name: self._apply_project_change,
            credential_changed.name: self._apply_credential_change,
            connector_changed.name: self._apply_connector_change,
            proxy_changed.name: self._apply_proxy_change,
            proxy_quarantine_changed.name: self._apply_proxy_quarantine_change,
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
            return
        await self.reload_connector(connector_id)

    async def _apply_proxy_change(self, proxy_id: str, op: str | None) -> None:
        if op == "removed":
            await self._evict_proxy_from_cache(proxy_id)
            return
        await self.reload_proxy(proxy_id)

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

    async def _evict_proxy_from_cache(self, proxy_id: str) -> None:
        if proxy_id in self._proxies:
            await self._redis_client.delete_proxy_status(proxy_id)
            await self._redis_client.reset_proxy_metrics(proxy_id)
            await self._rate_limiter.remove_proxy(proxy_id)
            del self._proxies[proxy_id]

    async def _heartbeat_loop(self) -> None:
        """Write a TTL'd Redis key advertising this instance.

        Used as the live-membership source for:

        * The cross-instance subscriber to drop self-echoes by ``instance_id``.
        * The HealthChecker's HRW shard ownership.
        * Lease holder identification (the lease value is the instance_id).

        Cleanup is double-belt:

        * Redis TTL (10s) expires the key automatically if the process
          dies hard (SIGKILL, OOM, network partition).
        * The ``finally`` block deletes the key on graceful shutdown so
          peers see the departure immediately rather than waiting 10s.

        Refresh interval (5s) is deliberately half the TTL so a single
        missed write does not declare us dead.
        """
        key = INSTANCE_REGISTRY_KEY.format(instance_id=self._settings.instance_id)
        payload = self._settings.role
        ttl_seconds = 10
        interval_seconds = 5
        try:
            while self._running:
                try:
                    await self._redis_client.client.set(key, payload, ex=ttl_seconds)
                except Exception:
                    logger.warning("Instance heartbeat write failed", exc_info=True)
                await asyncio.sleep(interval_seconds)
        finally:
            with contextlib.suppress(Exception):
                await self._redis_client.client.delete(key)

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

        Accumulates the request's contribution into a pending delta —
        nothing else. The hot path makes zero Redis calls (except the
        rate-limiter check below, which is correctness-critical and
        only opt-in).

        In-memory counters on ``Proxy`` / ``Project`` are intentionally
        NOT bumped here. The local instance would otherwise be ahead of
        its peers between the request and the next flush, which the UI
        would see as flapping when round-robin polls hit different
        instances. Instead, the flush loop applies the same delta to
        in-memory at the same moment it announces it to peers — see
        ``_flush_pending_metrics``.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy:
            accumulate_delta(
                self._pending_proxy_deltas.setdefault(proxy_id, empty_delta()),
                success, latency_ms, bytes_sent, bytes_received,
            )

            connector = self._connectors.get(proxy.connector_id)
            if connector:
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
            accumulate_delta(
                self._pending_project_deltas.setdefault(project_id, empty_delta()),
                success, latency_ms, bytes_sent, bytes_received,
            )

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

        # Release the EventBus distributed transport — it holds a reference
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
                status_data = statuses[proxy_id]
                proxy.status = status_data["status"]
                proxy.last_check_latency_ms = status_data["latency_ms"]
                proxy.consecutive_failures = status_data["consecutive_failures"]

            # Combine Postgres (historical) + Redis (current window)
            pg = postgres_proxy_metrics.get(proxy_id, {})
            rd = redis_proxy_metrics.get(proxy_id, {})
            apply_metrics(proxy, combine_metrics(pg, rd))

        # Hydrate project metrics directly on Project objects
        # Combines Postgres (historical) + Redis (current window)
        redis_project_metrics = await self._redis_client.get_all_project_metrics()
        for project_id, project in self._projects.items():
            pg = postgres_project_metrics.get(project_id, {})
            rd = redis_project_metrics.get(project_id, {})
            apply_metrics(project, combine_metrics(pg, rd))

        # Restore quarantine state from Redis
        await self._rate_limiter.hydrate_from_redis(list(self._proxies.keys()))

        logger.info(
            "Hydrated operational data",
            proxy_count=len(self._proxies),
            project_count=len(self._projects),
            from_redis=len(redis_proxy_metrics),
            from_postgres=len(postgres_proxy_metrics),
        )

    async def full_reload(self) -> None:
        """Re-read all definitions from Postgres, merging into the cache.

        Entries no longer present in Postgres are removed (with Redis +
        rate-limiter cleanup for proxies); new entries are added; existing
        entries have their *definition* fields patched in place so runtime
        state (live status, per-request counters) is preserved. After the
        merge, runtime fields are re-hydrated from Redis as a safety net so
        the cache converges with the cross-instance source of truth.
        """
        async with self._session_factory() as session:
            project_repo = ProjectRepository(session)
            credential_repo = CredentialRepository(session)
            connector_repo = ConnectorRepository(session)
            proxy_repo = ProxyRepository(session)
            projects = {p.id: p for p in await project_repo.get_all()}
            credentials = {c.id: c for c in await credential_repo.get_all()}
            connectors = {c.id: c for c in await connector_repo.get_all()}
            proxies = {p.id: p for p in await proxy_repo.get_all()}

        # Projects — merge in place, preserving aggregate counters.
        for pid in list(self._projects.keys()):
            if pid not in projects:
                self._projects.pop(pid, None)
                self._project_strategies.pop(pid, None)
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
        # their own — every field is DB-backed — so an outright replace
        # is fine, but only for entries that actually changed.
        for cid in list(self._credentials.keys()):
            if cid not in credentials:
                self._credentials.pop(cid, None)
        self._credentials.update(credentials)

        for cid in list(self._connectors.keys()):
            if cid not in connectors:
                self._connectors.pop(cid, None)
        self._connectors.update(connectors)

        # Proxies — patch in place to keep request counters, status, and
        # last_check_latency_ms from being clobbered by Pydantic defaults.
        removed_proxy_ids = [pid for pid in self._proxies if pid not in proxies]
        for pid in removed_proxy_ids:
            await self._redis_client.delete_proxy_status(pid)
            await self._redis_client.reset_proxy_metrics(pid)
            self._proxies.pop(pid, None)
        if removed_proxy_ids:
            await self._rate_limiter.remove_proxies(removed_proxy_ids)
        for pid, fresh_proxy in proxies.items():
            existing_proxy = self._proxies.get(pid)
            if existing_proxy is None:
                self._proxies[pid] = fresh_proxy
            else:
                existing_proxy.merge_definition_from(fresh_proxy)

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
        self, old_project_ids: list[str], old_proxy_ids: list[str]
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

        # Discard pending deltas keyed by now-deleted ids so the next flush
        # does not resurrect stale metrics in Redis.
        self._pending_proxy_deltas.clear()
        self._pending_project_deltas.clear()

        await self.full_reload()
        logger.info(
            "Applied imported state",
            old_projects=len(old_project_ids),
            old_proxies=len(old_proxy_ids),
        )

    async def _periodic_full_reload_loop(self, interval_seconds: int = 60) -> None:
        """Background safety-net: periodically re-sync the cache from Postgres."""
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    break
                await self.full_reload()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Periodic full reload failed", exc_info=True)

    async def _flush_pending_metrics(self) -> None:
        """Drain accumulated metric deltas to Redis and announce to peers.

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
        if not self._pending_proxy_deltas and not self._pending_project_deltas:
            return

        proxy_deltas = self._pending_proxy_deltas
        project_deltas = self._pending_project_deltas
        self._pending_proxy_deltas = {}
        self._pending_project_deltas = {}

        try:
            await self._redis_client.flush_metric_deltas(proxy_deltas, project_deltas)
        except Exception:
            logger.warning(
                "Failed to flush metric deltas; merging back into pending",
                exc_info=True,
            )
            for pid, d in proxy_deltas.items():
                merge_delta_into(
                    self._pending_proxy_deltas.setdefault(pid, empty_delta()), d
                )
            for pid, d in project_deltas.items():
                merge_delta_into(
                    self._pending_project_deltas.setdefault(pid, empty_delta()), d
                )
            return

        # Apply locally now that Redis is consistent. We use the same
        # code path peers will run when they receive the Pub/Sub
        # message below, so every instance updates its in-memory view
        # at the same logical moment instead of the local one leading
        # peers between request handling and propagation.
        self._apply_peer_metric_deltas(proxy_deltas, project_deltas)

        try:
            payload = json.dumps(
                {
                    "instance_id": self._settings.instance_id,
                    "proxy_deltas": proxy_deltas,
                    "project_deltas": project_deltas,
                }
            )
            await self._redis_client.client.publish(METRIC_DELTAS_CHANNEL, payload)
        except Exception:
            logger.warning(
                "Failed to publish metric deltas to peers (Redis already updated; "
                "cluster will converge via the 60s safety reload)",
                exc_info=True,
            )

    async def _periodic_metric_flush_loop(self, interval_seconds: float = 5.0) -> None:
        """Periodically flush accumulated metric deltas.

        Default cadence is 5s — fast enough that the UI feels live,
        slow enough that the Redis pipeline batches many requests
        into a single round-trip. On a busy host this turns 10k
        per-request Redis writes per second into one batched write
        every 5s carrying 50k aggregated increments.
        """
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    break
                await self._flush_pending_metrics()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Periodic metric flush failed", exc_info=True)

    async def _metric_delta_subscriber_loop(self) -> None:
        """Listen for peer instances' metric deltas and apply them to in-memory.

        Drops self-echoes by ``instance_id``. The applied delta updates
        in-memory ``Proxy`` / ``Project`` counters using the same
        weighted-average math as ``increment_stats`` — see
        ``stats.apply_delta``. Reconnects on transient Redis failures
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
                        self._apply_peer_metric_deltas(
                            payload.get("proxy_deltas") or {},
                            payload.get("project_deltas") or {},
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
        proxy_deltas: dict[str, dict[str, Any]],
        project_deltas: dict[str, dict[str, Any]],
    ) -> None:
        """Fold peer deltas into local in-memory counters."""
        for proxy_id, delta in proxy_deltas.items():
            proxy = self._proxies.get(proxy_id)
            if proxy is not None:
                apply_delta(proxy, delta)
        for project_id, delta in project_deltas.items():
            project = self._projects.get(project_id)
            if project is not None:
                apply_delta(project, delta)

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
            logger.info("Reload removed connector from cache", connector_id=connector_id)
            return
        self._connectors[connector_id] = connector
        logger.debug("Reloaded connector", connector_id=connector_id)

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
        peer publishes ``proxy_changed`` — causing UI stats to flap.
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
            # First time we see this proxy — take the fresh entity and
            # apply Redis status. Counters start at zero (the model
            # default), and converge upward via ``_hydrate_from_redis``
            # on the next periodic full reload.
            if status_data:
                fresh.status = status_data["status"]
                fresh.last_check_latency_ms = status_data["latency_ms"]
                fresh.consecutive_failures = status_data["consecutive_failures"]
            self._proxies[proxy_id] = fresh
        else:
            existing.merge_definition_from(fresh)
            if status_data:
                existing.status = status_data["status"]
                existing.last_check_latency_ms = status_data["latency_ms"]
                existing.consecutive_failures = status_data["consecutive_failures"]
        logger.debug("Reloaded proxy", proxy_id=proxy_id)

    @property
    def rate_limiter(self) -> RateLimiter:
        """Get the rate limiter instance."""
        return self._rate_limiter

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

        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.connector_id not in connector_ids_to_remove
        }

        logger.info("Removed project", project_id=project_id)
        await event_bus.publish(project_changed, self, entity_id=project_id, op="removed")
        return True

    # Credential methods
    def get_credentials_for_project(self, project_id: str) -> list[Credential]:
        """Get all credentials for a project, ordered by creation time."""
        credentials = [c for c in self._credentials.values() if c.project_id == project_id]
        return sorted(credentials, key=lambda c: c.created_at)

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
        connectors = [c for c in self._connectors.values() if c.project_id == project_id]
        return sorted(connectors, key=lambda c: c.created_at)

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
            proxy_ids = [p.id for p in self._proxies.values() if p.connector_id == connector.id]
            self._rate_limiter.clear_connector_proxies(proxy_ids)

        self._connectors[connector.id] = connector
        logger.info("Updated connector", connector_id=connector.id, name=connector.name)
        await event_bus.publish(connector_changed, self, entity_id=connector.id, op="updated")

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

        # Remove from cache
        del self._connectors[connector_id]
        # Also remove associated proxies from cache
        self._proxies = {
            pid: p for pid, p in self._proxies.items()
            if p.connector_id != connector_id
        }
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
        if credential and credential.type in (
            CredentialType.AWS,
            CredentialType.GCP,
            CredentialType.AZURE,
        ):
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

    def _get_enabled_connector_ids(
        self, project_id: str, target_host: str | None = None
    ) -> set[str]:
        """Get IDs of enabled connectors for a project.

        Args:
            project_id: The project to get connectors for.
            target_host: If provided, only return connectors whose domain
                routing config allows this host.
        """
        connector_ids: set[str] = set()
        for c in self._connectors.values():
            if c.project_id != project_id or not c.enabled:
                continue
            if target_host:
                routing = c.parsed_routing_config
                if not is_domain_allowed(target_host, routing):
                    continue
            connector_ids.add(c.id)
        return connector_ids

    def get_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project (via enabled connectors only)."""
        connector_ids = self._get_enabled_connector_ids(project_id)
        return [p for p in self._proxies.values() if p.connector_id in connector_ids]

    def get_all_proxies_for_project(self, project_id: str) -> list[Proxy]:
        """Get all proxies for a project, including those from disabled connectors."""
        connector_ids = {c.id for c in self._connectors.values() if c.project_id == project_id}
        return [p for p in self._proxies.values() if p.connector_id in connector_ids]

    def get_healthy_proxies_for_project(
        self, project_id: str, target_host: str | None = None
    ) -> list[Proxy]:
        """Get healthy proxies for a project (from enabled connectors only).

        Excludes quarantined proxies (rate-limited).

        Args:
            project_id: The project to get proxies for.
            target_host: If provided, only return proxies from connectors whose
                domain routing config allows this host.
        """
        connector_ids = self._get_enabled_connector_ids(project_id, target_host)
        return [
            p for p in self._proxies.values()
            if p.connector_id in connector_ids
            and p.status == ProxyStatus.HEALTHY
            and not self._rate_limiter.is_quarantined(p.id)
        ]

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
    ) -> bool:
        """Check if proxy selection failed due to quarantine.

        Returns True when either all healthy proxies are quarantined, or
        when sticky_quarantine blocked a specific session's quarantined proxy.
        Used to distinguish 'no proxies exist' (502) from quarantine (429).
        """
        if self._is_sticky_quarantine_blocked(project_id, session_id):
            return True

        connector_ids = self._get_enabled_connector_ids(project_id, target_host)
        healthy = [
            p for p in self._proxies.values()
            if p.connector_id in connector_ids and p.status == ProxyStatus.HEALTHY
        ]
        if not healthy:
            return False
        return all(self._rate_limiter.is_quarantined(p.id) for p in healthy)

    def get_quarantined_count_for_project(self, project_id: str) -> int:
        """Get the number of quarantined proxies for a project."""
        connector_ids = {c.id for c in self._connectors.values() if c.project_id == project_id and c.enabled}
        return sum(
            1 for p in self._proxies.values()
            if p.connector_id in connector_ids and self._rate_limiter.is_quarantined(p.id)
        )

    async def select_proxy_for_project(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
    ) -> Proxy | None:
        """Select a proxy for a specific project using the project's routing strategy.

        Returns a proxy with resolved credentials (placeholders replaced with
        actual values from the credential/connector chain).

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
        """
        if self._is_sticky_quarantine_blocked(project_id, session_id):
            return None

        healthy_proxies = self.get_healthy_proxies_for_project(project_id, target_host)
        strategy = self._project_strategies.get(project_id, self._strategy)

        # The strategy handles whatever cross-instance state it needs
        # (sticky reads/writes its Redis binding inside select; others
        # ignore the redis_client/project_id kwargs).
        selected = await strategy.select(
            healthy_proxies,
            session_id,
            redis_client=self._redis_client,
            project_id=project_id,
        )
        if selected is None:
            return None
        return self.resolve_proxy_credentials(selected)

    def set_project_strategy(self, project_id: str, strategy_name: str) -> None:
        """Change the routing strategy for a project."""
        self._project_strategies[project_id] = get_strategy(strategy_name)
        logger.info("Changed project routing strategy", project_id=project_id, strategy=strategy_name)

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
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            await repo.update(proxy)
            await session.commit()

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
        logger.info("Removed proxy", proxy_id=proxy_id)

        await event_bus.publish(proxy_removed,
            self,
            proxy_id=proxy_id,
            connector_id=connector_id,
        )
        await event_bus.publish(proxy_changed, self, entity_id=proxy_id, op="removed")

        return True

    async def select_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select a proxy using the current (global) routing strategy.

        Project-scoped traffic uses select_proxy_for_project instead; this is
        the unscoped fallback. Kept async for API consistency with the
        project-scoped variant.
        """
        proxy = await self._strategy.select(
            self.healthy_proxies,
            session_id,
            redis_client=self._redis_client,
        )
        if proxy:
            return self.resolve_proxy_credentials(proxy)
        return None

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
                # Cross-instance: tell peers to re-hydrate this proxy.
                await event_bus.publish(
                    proxy_changed, self, entity_id=proxy_id, op="updated"
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
            if credential and credential.type in (
                CredentialType.AWS,
                CredentialType.GCP,
                CredentialType.AZURE,
            ):
                # Mark as terminating - auto-scaler will handle the actual termination
                logger.info(
                    "Marking cloud proxy for termination",
                    proxy_id=proxy_id,
                    credential_type=credential.type.value,
                )
                return await self.mark_proxy_terminating(proxy_id)

        # Non-cloud proxy: remove directly
        return await self.remove_proxy(proxy_id)

    def get_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a specific connector."""
        return [p for p in self._proxies.values() if p.connector_id == connector_id]

    def get_active_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get active (non-draining, non-terminating) proxies for a connector."""
        return [
            p for p in self._proxies.values()
            if p.connector_id == connector_id
            and p.status not in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING)
        ]

