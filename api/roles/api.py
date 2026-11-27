# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api role's runtime: the admin API's services, read model, previews, Needs attention and alerts, attribution,
tunnels and sign-in.

Its one loop keeps the project map the exit reports route takes its project from; the node runs the process's
definitions refresher and observation recorder. At start it creates the tunnel settings rows a fresh install lacks.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from redis.exceptions import RedisError
from sqlalchemy.pool import QueuePool

from api.attention.history import AlertHistory
from api.attention.requests import AlertChecks
from api.attention.service import AttentionService, InstallAttentionService
from api.attention.settings import AlertSettingsService
from api.core.demand_tracker import DemandTracker
from api.core.logging import error_text
from api.core.roles import Role
from api.core.system_stats import ClusterReader
from api.core.tasks import quietly
from api.feed.compiler import FeedCompiler
from api.feed.preview import RoutingPreview
from api.feed.state import RuntimeStateReader
from api.geo.attributor import ProxyAttributor
from api.geo.databases import GeoDatabaseService
from api.geo.history import ObservationHistory
from api.geo.reattribution import ReattributionService
from api.geo.settings import GeoSettingsService, defaults_from_config
from api.geo.status import GeoStatusService
from api.jobs.attribution import Attribution
from api.models.system import ApiSnapshot, GatewaySnapshot, InstanceSnapshot
from api.openvpn.service import OpenVpnService
from api.reputation.ip_service import ExitIpService
from api.reputation.reports import ExitReportService
from api.reputation.site_service import SiteReputationService
from api.roles.refresh import ProjectMap
from api.roles.runtime import RoleRuntime
from api.services.api_tokens import ApiTokenService
from api.services.audit import AuditService
from api.services.backup import BackupService
from api.services.connect import ConnectService
from api.services.connectors import ConnectorService
from api.services.countries import CountryService
from api.services.credentials import CredentialService
from api.services.detect import DetectService
from api.services.failures import FailureLogService
from api.services.hosts import HostMetricsService
from api.services.metrics import MetricsService
from api.services.mitm import MitmService
from api.services.projects import ProjectService
from api.services.provider_calls import ProviderCallService
from api.services.providers import ProviderService
from api.services.proxies import ProxyService
from api.services.read_model import RuntimeView
from api.services.releases import ReleaseService
from api.services.system import SystemService
from api.services.system_settings import SystemSettingsService
from api.services.traffic_split import TrafficSplitService
from api.services.users import UserService
from api.sso import SsoFlow
from api.sso.providers import SsoProviderService
from api.wireguard.service import WireGuardService, seed_tunnel_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from api.core.config import Settings
    from api.core.loops import Loop
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.geo.runtime import GeoStack
    from api.jobs.queue import JobQueue
    from api.models.tunnel import TunnelEndpointState
    from api.providers.store import ProviderStore
    from api.routing.router import Router

logger = structlog.get_logger()


class ApiRuntime(RoleRuntime):
    """The admin API's services, read model, previews, attribution, tunnels and sign-in, over the node's infrastructure.

    Every collaborator a route uses is a public attribute, which the routes reach through ``api/routes/common.py``.

    Args:
        engine: the engine of ``session_factory``, whose pool the snapshot reports.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        jobs: JobQueue,
        provider_store: ProviderStore,
        geo: GeoStack,
        changes: ChangeLog,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._engine = engine
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.jobs = jobs
        self.changes = changes
        self.provider_store = provider_store
        self.provider_registry = provider_store.registry
        self.state_reader = RuntimeStateReader(redis_client)
        self._cluster = ClusterReader(redis_client)
        self.demand_tracker = DemandTracker(redis_client)
        self.runtime_view = RuntimeView(session_factory, self.state_reader, self.provider_registry, self.demand_tracker)
        # Asks the workers for a project's alert check after a write, and says when each was last checked.
        self.alert_checks = AlertChecks(redis_client, jobs)
        self.connector_service = ConnectorService(
            session_factory, jobs, redis_client, self.provider_registry, changes=changes, alerts=self.alert_checks
        )
        self.project_service = ProjectService(
            session_factory,
            jobs,
            redis_client,
            changes=changes,
            connectors=self.connector_service,
            alerts=self.alert_checks,
        )
        self.credential_service = CredentialService(session_factory, self.provider_registry, changes=changes)
        # The provider catalog and its descriptor writes, and the live vendor calls of the forms and the builder.
        self.provider_service = ProviderService(session_factory, provider_store, changes=changes)
        self.provider_call_service = ProviderCallService(session_factory, self.provider_registry)
        self.proxy_service = ProxyService(
            session_factory, jobs, redis_client, self.provider_registry, changes=changes, alerts=self.alert_checks
        )
        self.geo = geo
        self.geo_service = geo.geo_service
        self.geo_settings_store = geo.settings_store
        self.geo_database_store = geo.database_store
        self.observation_recorder = geo.observation_recorder
        # Compiles the previews, one project each, with no store: this process's stores follow Postgres on their own.
        self._compiler = FeedCompiler(
            session_factory,
            self.provider_registry,
            geo_defaults=defaults_from_config(settings),
            default_strategy=settings.default_strategy,
        )
        self.preview = RoutingPreview(
            self._compiler, self.runtime_view, self.provider_registry, default_strategy=settings.default_strategy
        )
        # The Connect page, and the MITM captures with the CA certificate the gateways generated.
        self.connect_service = ConnectService(
            session_factory, preview=self.preview, proxy_endpoint=settings.proxy_endpoint
        )
        self.mitm_service = MitmService(session_factory, redis_client, ca_cert_path=settings.tls_mitm_ca_cert_path)
        # The Countries page: which connectors serve each country, and where each would route now.
        self.country_service = CountryService(session_factory, preview=self.preview)
        # Exit reputation: the exit IPs view, and one site across its places for the Reputation tab and the project form.
        self.exit_ip_service = ExitIpService(session_factory)
        self.site_reputation_service = SiteReputationService(session_factory, preview=self.preview)
        # The metrics pages: the pool and counters, the traffic split, the Hosts page and the failure log.
        self.metrics_service = MetricsService(
            session_factory, views=self.runtime_view, default_strategy=settings.default_strategy
        )
        self.traffic_split_service = TrafficSplitService(session_factory, preview=self.preview)
        self.host_metrics_service = HostMetricsService(session_factory, enabled=settings.host_metrics_enabled)
        self.failure_log_service = FailureLogService(
            session_factory,
            redis_client,
            enabled=settings.failure_log_enabled,
            max_entries=settings.failure_log_max_entries,
            retention_hours=settings.failure_log_retention_hours,
        )
        # Needs attention and alerts, read from the alerts the workers store.
        self.attention = AttentionService(session_factory, self.alert_checks)
        self.install_attention = InstallAttentionService(session_factory, self.alert_checks, self._cluster)
        self.alert_history = AlertHistory(session_factory)
        self.alert_settings = AlertSettingsService(session_factory, self.alert_checks)
        # How long the install's alerts and the audit log's entries are kept, and an entity's History.
        self.system_settings_service = SystemSettingsService(session_factory)
        self.audit_service = AuditService(session_factory)
        # The System page, What's New, and the backup export and import.
        self.system_service = SystemService(
            session_factory,
            redis_client,
            self.provider_registry,
            cluster=self._cluster,
            metrics_interval=settings.system_metrics_interval,
        )
        self.release_service = ReleaseService(session_factory, redis_client, update_check=settings.update_check)
        self.backup_service = BackupService(
            session_factory,
            jobs,
            redis_client,
            self.provider_registry,
            changes=changes,
            provider_store=provider_store,
            geo=geo.geo_service,
        )
        self.attributor = ProxyAttributor(geo.geo_service, lookup_enabled=settings.geo_lookup_enabled)
        # Detect: a store per request (``attribution.store()``), written in one transaction.
        self.attribution = Attribution(
            self.attributor, session_factory, self.proxy_service, redis_client, changes=changes
        )
        self.detect_service = DetectService(self.attribution, views=self.runtime_view)
        # A manual refresh, and an add from a URL: the scheduled downloads are the workers' jobs.
        self.geo_updater = geo.updater(settings, session_factory=session_factory, changes=changes, jobs=jobs)
        # IP attribution's admin API: re-judging asked of the workers, the settings with a test lookup, the databases,
        # the observation log with the exit statistics, and the pipeline's status.
        self.reattribution_service = ReattributionService(session_factory, jobs)
        self.geo_settings_service = GeoSettingsService(
            session_factory,
            geo=geo.geo_service,
            changes=changes,
            reattribution=self.reattribution_service,
            echo_enabled=settings.geo_echo_enabled,
            echo_trusted_proxies=settings.geo_echo_trusted_proxies,
        )
        self.geo_database_service = GeoDatabaseService(
            session_factory,
            store=geo.database_store,
            updater=self.geo_updater,
            gateways=self.gateway_snapshots,
            changes=changes,
            reattribution=self.reattribution_service,
        )
        self.observation_history = ObservationHistory(session_factory, coverage=self.runtime_view.exit_coverage)
        self.geo_status_service = GeoStatusService(session_factory, geo=geo.geo_service, gateways=self.gateway_snapshots)
        self.wireguard_service = WireGuardService(
            session_factory,
            redis_client,
            endpoint_states=self.tunnel_endpoints,
            device_totals=self.runtime_view.device_counters,
            defaults=settings.wireguard_defaults,
            changes=changes,
        )
        self.openvpn_service = OpenVpnService(
            session_factory,
            redis_client,
            endpoint_states=self.tunnel_endpoints,
            device_totals=self.runtime_view.device_counters,
            defaults=settings.openvpn_defaults,
            changes=changes,
        )
        self.sso_flow = SsoFlow(redis_client)
        # The admin's sign-in provider settings; the sign-in routes read the enabled providers themselves.
        self.sso_provider_service = SsoProviderService(session_factory)
        # User accounts with their linked sign-ins, and their personal access tokens.
        self.user_service = UserService(session_factory, invite_expiry_hours=settings.invite_token_expiry_hours)
        self.api_token_service = ApiTokenService(session_factory)
        # Every project, kept current from the changes stream: the exit reports route reads no project per call.
        self.project_map = ProjectMap(
            session_factory, redis_client=redis_client, reconcile_interval=settings.reconcile_interval
        )
        self.exit_report_service = ExitReportService(redis_client, projects=self.project_map)
        self._loops: tuple[Loop, ...] = (self.project_map,)

    # --- Lifecycle ----------------------------------------------------------------------

    def loops(self) -> Sequence[Loop]:
        """The project map; the node runs the process's definitions refresher and observation recorder."""
        return self._loops

    async def start(self) -> None:
        """Create the tunnel settings rows a fresh install lacks and announce each, so its first feed carries both protocols,
        then read the project map before its loop follows the changes stream.

        Raises when Postgres cannot be written or read, which the node has just migrated.
        """
        await seed_tunnel_settings(self.session_factory, self._settings, changes=self.changes)
        self._note_default_proxy_endpoint()
        await self.project_map.start()
        await super().start()
        logger.info("API started", instance=self._settings.instance_id)

    async def stop(self) -> None:
        """Close the sign-in client; never raises, and nothing here is buffered (the node closes the change log)."""
        await super().stop()
        await quietly("sign-in client", self.sso_flow.close())

    def readiness(self) -> str | None:
        """None once started: the routes need Postgres too, which the node's heartbeat checks."""
        return None if self.started else "api not started"

    def snapshot(self) -> ApiSnapshot:
        """The process's database pool, from memory."""
        size, checked_out, overflow = _pool_usage(self._engine)
        return ApiSnapshot(pool_size=size, pool_checked_out=checked_out, pool_overflow=overflow)

    # --- Previews ------------------------------------------------------------------------

    async def preview_router(self, project_id: str) -> Router:
        """A router over one project's routing as a fresh gateway would build it from a feed compiled now; built per request.

        The quarantines and the traffic blocks the traffic job stored come from Redis, none while it cannot be read.
        """
        return await self.preview.project(project_id)

    # --- Heartbeats ---------------------------------------------------------------------

    async def instance_snapshots(self) -> dict[str, InstanceSnapshot]:
        """What every live process last published on its heartbeat, by instance id, read as the System page reads them.

        A process whose heartbeat expired is left out, as is one with no snapshot this release can read.
        """
        return {
            instance.instance_id: instance.snapshot
            for instance in await self._cluster.instances()
            if instance.snapshot is not None
        }

    async def gateway_snapshots(self) -> dict[str, GatewaySnapshot]:
        """Every live gateway's part of its last heartbeat, by instance id in order; none when Redis cannot be read."""
        try:
            snapshots = await self.instance_snapshots()
        except (RedisError, OSError) as exc:
            logger.warning("Gateway heartbeats unavailable, shown without them", error=error_text(exc))
            return {}
        return {
            instance_id: snapshot.gateway
            for instance_id, snapshot in sorted(snapshots.items())
            if snapshot.gateway is not None
        }

    async def tunnel_endpoints(self) -> list[TunnelEndpointState]:
        """Every tunnel endpoint the live gateways report, both protocols, from their heartbeats."""
        snapshots = await self.instance_snapshots()
        return [
            endpoint
            for snapshot in snapshots.values()
            if snapshot.gateway is not None
            for endpoint in snapshot.gateway.tunnels
        ]

    # --- Helpers ------------------------------------------------------------------------

    def _note_default_proxy_endpoint(self) -> None:
        """Outside role all, clients may reach the proxy at another host than the web UI: name the setting for it."""
        settings = self._settings
        if settings.role != Role.ALL and not settings.proxy_public_url:
            logger.info(
                "The Connect page names the web UI's host as the proxy's",
                role=settings.role.value,
                setting="OCTOPROX_PROXY_PUBLIC_URL",
            )


def _pool_usage(engine: AsyncEngine) -> tuple[int | None, int | None, int | None]:
    """The pool's size, connections in use and overflow; None for each when the pool does not keep them."""
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return None, None, None
    return pool.size(), pool.checkedout(), max(0, pool.overflow())
