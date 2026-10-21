# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The gateway role: proxy and tunnel traffic, routed on the routing feed, with no Postgres and no database credentials.

Each verified feed update is applied in one order (:meth:`GatewayRuntime.apply`), and everything the gateway counts or
sees goes to Redis only, batched off the request path.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.config import Settings
from api.core.failure_log import FailureLog, RequestFailure
from api.core.mitm import MitmHandler
from api.core.proxy_server import ProxyServer
from api.core.rate_limiter import RateLimiter
from api.core.roles import Role
from api.core.signals import request_failed
from api.core.tasks import quietly
from api.core.tls_cert_manager import TLSCertManager
from api.core.workers import WorkerName
from api.feed.codec import FeedCodec
from api.feed.follower import FeedFollower, FeedUpdate
from api.feed.models import GLOBAL_SECTION, GlobalSection
from api.gateway.metering import RequestMetering
from api.gateway.sightings import SightingPublisher
from api.geo.preflight import PreflightChecker
from api.geo.store import DatabaseSync
from api.geo.verifier import ExitVerifier
from api.jobs.queue import JobQueue
from api.models.system import GatewaySnapshot
from api.openvpn.endpoint import OpenVpnEndpoint
from api.roles.runtime import RoleRuntime
from api.routing.exclusions import LocalExclusions
from api.routing.inflight import InFlight
from api.routing.ondemand import FeedOnDemand
from api.routing.outcomes import LocalOutcomes
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.tunnel.dataplane import TunnelDataPlane
from api.wireguard.endpoint import TunnelReconciler, WireGuardEndpoint

if TYPE_CHECKING:
    from api.core.loops import Loop
    from api.db.redis import RedisClient
    from api.geo.runtime import GeoStack
    from api.providers.store import ProviderStore

logger = structlog.get_logger()


class GatewayRuntime(RoleRuntime):
    """One gateway: the routing feed in, proxied and tunnelled requests out.

    Args:
        redis_client: the process's one Redis client, connected before :meth:`start`, which every call goes through.
        geo: the process's attribution stack, fetching files from the api (``HttpBlobSource``); the node closes it.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: RedisClient,
        provider_store: ProviderStore,
        geo: GeoStack,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._redis = redis_client
        self._provider_store = provider_store
        self._geo = geo
        # Set once every IP database in the feed has loaded (see ``readiness``).
        self._geo_loaded_once = False
        self.database_sync = DatabaseSync(geo.database_store)
        # Set when stop() begins, so a feed applied meanwhile binds no listener.
        self._stopping = False
        self.exclusions = LocalExclusions()
        self.inflight = InFlight()
        self.outcomes = LocalOutcomes()
        # The calls a request makes or sets off (an on-demand ask here; rate
        # windows, sticky bindings, preflight verdicts, MITM captures and fake
        # IPs below) each run under the client's request deadline and fail
        # open when it passes, whatever the reply bound of the batched writes.
        self.on_demand = FeedOnDemand(JobQueue(redis_client), redis_client)
        self.rate_limiter = RateLimiter(redis_client, on_trip=self.exclusions.quarantine)
        self.router = Router(
            RoutingSnapshot.from_sections({}, registry=provider_store.registry),
            exclusions=self.exclusions,
            inflight=self.inflight,
            outcomes=self.outcomes,
            on_demand=self.on_demand,
            redis_client=redis_client,
            default_strategy=settings.default_strategy,
        )
        # Metering counts into the transfers in flight and the outcomes the
        # router's strategies read, and reads the connectors and traffic blocks
        # of the snapshot the router holds in force.
        self.metering = RequestMetering(
            redis_client,
            self.inflight,
            self.rate_limiter,
            outcomes=self.outcomes,
            router=self.router,
            host_metrics_enabled=settings.host_metrics_enabled,
            host_metrics_max_hosts=settings.host_metrics_max_hosts,
        )
        self.follower = FeedFollower(
            redis_client,
            FeedCodec(settings.internal_secret),
            self.apply,
            reconcile_interval=settings.reconcile_interval,
        )
        self.failure_log = FailureLog(redis_client, settings, connector_name=self._connector_name)
        # Preflight sightings of pooled slots travel like the dynamic rows'
        # observations do, on the same cadence and within the same bound.
        self.sightings = SightingPublisher(
            redis_client,
            interval=settings.geo_observation_publish_interval,
            max_pending=settings.geo_observation_max_buffer,
        )
        self.preflight = PreflightChecker(
            geo.geo_service, redis_client, sightings=self.sightings, exclusions=self.exclusions
        )
        self._exit_verifier = ExitVerifier(self.preflight, self.router)
        self.cert_manager = TLSCertManager(Path(settings.tls_mitm_ca_cert_path), Path(settings.tls_mitm_ca_key_path))
        self.mitm_handler = MitmHandler(self.cert_manager, redis_client=redis_client)
        self.proxy_server = ProxyServer(
            self.router,
            self.metering,
            host=settings.host,
            port=settings.proxy_port,
            timeout=settings.connection_timeout,
            mitm_handler=self.mitm_handler,
            exit_verifier=self._exit_verifier,
        )
        # The tunnels this gateway terminates: a data plane they share and an
        # endpoint per enabled protocol, each following the feed.
        self.tunnel: TunnelDataPlane | None = None
        self.wireguard: WireGuardEndpoint | None = None
        self.openvpn: OpenVpnEndpoint | None = None
        if settings.wireguard_enabled or settings.openvpn_enabled:
            self.tunnel = TunnelDataPlane(settings, redis_client)
            if settings.wireguard_enabled:
                self.wireguard = WireGuardEndpoint(settings, self.tunnel, redis_client)
            if settings.openvpn_enabled:
                self.openvpn = OpenVpnEndpoint(settings, self.tunnel, redis_client)
        # Failed and refused requests since start, by failure kind.
        self._failures: Counter[str] = Counter()
        # The projects of the global section applied last.
        self._project_ids: set[str] = set()
        failure_log = (self.failure_log,) if self.failure_log.enabled else ()
        self._loops: tuple[Loop, ...] = (
            self.database_sync, self.follower, self.metering, *failure_log, self.sightings
        )

    @property
    def endpoints(self) -> list[WireGuardEndpoint | OpenVpnEndpoint]:
        """The tunnel endpoints this gateway runs."""
        return [endpoint for endpoint in (self.wireguard, self.openvpn) if endpoint is not None]

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        """Every loop of this gateway, the tunnel endpoints' included, for the System page."""
        return self.tasks.tasks + [task for endpoint in self.endpoints for task in endpoint.tasks.tasks]

    # --- Lifecycle ----------------------------------------------------------------------

    def loops(self) -> Sequence[Loop]:
        """The IP database sync, the feed follower and the publishers: request metering, the failure log while it is enabled, the sightings."""
        return self._loops

    async def start(self) -> None:
        """Load the MITM CA, start the tunnels, listen to the request signals and start the loops, raising when the CA cannot be had."""
        self._warn_on_default_api_url()
        await self.cert_manager.bootstrap(self._redis, self._settings.instance_id)
        if self.tunnel is not None:
            # Before the follower: the first feed applied brings the interfaces up on the plane.
            await self.tunnel.start(
                self.router, self.metering, mitm_handler=self.mitm_handler, exit_verifier=self._exit_verifier
            )
            for endpoint in self.endpoints:
                await endpoint.start()
            self.tasks.spawn(WorkerName.TUNNEL_RECONCILER, TunnelReconciler(self.endpoints).run())
        # Once the tunnel listener exists, and before the follower starts: no listener serves a request until the
        # first feed is applied.
        senders = self._senders()
        self.metering.start(senders)
        for sender in senders:
            request_failed.connect(self._on_request_failed, sender=sender)
        await super().start()

    async def stop(self) -> None:
        """Close the listeners, stop the loops, each publisher sending what it holds as it stops, then take the tunnels down.

        The writes still under way are awaited before the tunnels go; never raises.
        """
        self._stopping = True
        await quietly("proxy listener", self.proxy_server.stop())
        if self.tunnel is not None and self.tunnel.transparent_server is not None:
            await quietly("tunnel listener", self.tunnel.transparent_server.stop())
        request_failed.disconnect(self._on_request_failed)
        await super().stop()
        await quietly("MITM captures", self.mitm_handler.settle())
        await quietly("sticky bindings", self.router.settle())
        for endpoint in self.endpoints:
            await quietly("tunnel endpoint", endpoint.stop())
        if self.tunnel is not None:
            await quietly("tunnel data plane", self.tunnel.stop())

    def readiness(self) -> str | None:
        """None when this gateway can serve, else why not."""
        reason = self.follower.readiness()
        if reason is not None:
            return reason
        if not self.proxy_server.is_listening:
            return "proxy listener not bound"
        if not self.cert_manager.loaded:
            return "MITM CA not loaded"
        # Missing IP databases hold a gateway back only until its first full
        # load: preflight on a fresh gateway with no databases would refuse
        # traffic that projects refusing unknown exits expect to pass. After
        # that, a database added later that cannot be fetched yet must not
        # turn every gateway unready at once and leave the load balancer with
        # no endpoint; it is retried, and the snapshot reports it.
        if not self._geo_loaded_once:
            missing = self.database_sync.missing()
            if missing:
                return f"IP databases not loaded yet: {', '.join(missing)}"
            self._geo_loaded_once = True
        return None

    def snapshot(self) -> GatewaySnapshot:
        """What this gateway is doing now, for its heartbeat and the metrics endpoint."""
        head = self.follower.applied_head
        applied_at = self.follower.applied_at
        totals = self.metering.totals
        databases = self._geo.database_store
        recorder = self._geo.observation_recorder
        return GatewaySnapshot(
            listener_port=self.proxy_server.port,
            listening=self.proxy_server.is_listening,
            active_connections=self.proxy_server.active_connections,
            feed_digest=head.digest if head is not None else None,
            feed_changes=head.changes if head is not None else None,
            feed_applied_at=applied_at,
            feed_age_seconds=(utc_now() - applied_at).total_seconds() if applied_at is not None else None,
            retired_slots=self.exclusions.retired_count,
            local_quarantines=self.exclusions.quarantined_count,
            in_flight=self.inflight.total,
            requests=totals.request_count,
            successes=totals.success_count,
            failures=totals.failure_count,
            bytes_sent=totals.bytes_sent,
            bytes_received=totals.bytes_received,
            failures_by_kind=dict(self._failures),
            pending_metric_deltas=self.metering.pending,
            metric_flush_failures=self.metering.failed_flushes,
            rate_limit_errors=self.rate_limiter.window_errors,
            preflight_checks=self.preflight.checks,
            preflight_rejections=self.preflight.rejections,
            geo_databases_loaded=[database.id for database in databases.loaded],
            geo_database_errors=databases.load_errors,
            geo_databases_missing=databases.missing(),
            observations_pending=recorder.pending,
            observations_dropped=recorder.dropped,
            sightings_pending=self.sightings.pending,
            sightings_dropped=self.sightings.dropped,
            failure_log_dropped=self.failure_log.dropped,
            mitm_captures_dropped=self.mitm_handler.dropped_captures,
            tunnels=[endpoint.state() for endpoint in self.endpoints],
        )

    # --- Following the feed -------------------------------------------------------------

    async def apply(self, update: FeedUpdate) -> None:
        """Apply one verified feed update: the global section's settings, the snapshot, local state, then the first bind.

        Every step is idempotent, so the follower's retry of an update that raised (a proxy port that cannot be bound)
        converges.
        """
        global_section = update.changed.get(GLOBAL_SECTION)
        if isinstance(global_section, GlobalSection):
            self._provider_store.apply_specs(global_section.descriptors)
            self._geo.settings_store.adopt(global_section.geo_settings, stored=global_section.geo_settings_stored)
            if self.wireguard is not None:
                self.wireguard.apply(global_section.wireguard)
            if self.openvpn is not None:
                self.openvpn.apply(global_section.openvpn)
            self.database_sync.offer(global_section.geo_databases)
            self._forget_projects({project.id for project in global_section.projects})
        previous = self.router.snapshot
        snapshot = RoutingSnapshot.from_sections(
            update.sections, registry=self._provider_store.registry, previous=previous
        )
        self.router.swap(snapshot)
        self.outcomes.retain(snapshot.proxy_ids)
        self.exclusions.reconcile(snapshot.quarantined)
        for connector_id in previous.connector_ids - snapshot.connector_ids:
            self.failure_log.forget_connector(connector_id)
        await self.on_demand.applied(snapshot)
        if not self._stopping and not self.proxy_server.is_listening:
            await self.proxy_server.start()
            if self._stopping:
                # stop() ran while the port was being bound, and found no listener to close.
                await self.proxy_server.stop()

    def _forget_projects(self, project_ids: set[str]) -> None:
        """Drop the failure log's window for projects the feed no longer has, so its next write leaves them gone."""
        for project_id in self._project_ids - project_ids:
            self.failure_log.forget(project_id)
        self._project_ids = project_ids

    # --- Collaborators' callbacks and loops ----------------------------------------------

    def _senders(self) -> list[object]:
        """What publishes this gateway's request signals: its proxy listener, its MITM handler and its tunnel listener.

        Its receivers hear these alone, so gateways sharing one process each count only their own requests.
        """
        senders: list[object] = [self.proxy_server, self.mitm_handler]
        if self.tunnel is not None and self.tunnel.transparent_server is not None:
            senders.append(self.tunnel.transparent_server)
        return senders

    async def _on_request_failed(self, sender: object, failure: RequestFailure, **_: object) -> None:
        """``request_failed`` from a listener or the MITM handler: counted by kind and kept by the failure log."""
        try:
            self._failures[failure.kind.value] += 1
            if self.failure_log.enabled:
                self.failure_log.record(failure)
        except Exception:
            logger.error("Recording a failed request failed", project_id=failure.project_id, exc_info=True)

    def _connector_name(self, connector_id: str) -> str | None:
        """A connector's name in the snapshot in force, for the failure log's examples."""
        view = self.router.snapshot.connector_view(connector_id)
        return view.connector.name if view is not None else None

    def _warn_on_default_api_url(self) -> None:
        """A gateway of its own fetches IP database files from the api, which is not on its own host by default."""
        default = Settings.model_fields["internal_api_url"].default
        if self._settings.role == Role.GATEWAY and self._settings.internal_api_url == default:
            logger.warning(
                "Gateway reaches the api at the default internal_api_url",
                internal_api_url=self._settings.internal_api_url,
                setting="OCTOPROX_INTERNAL_API_URL",
            )
