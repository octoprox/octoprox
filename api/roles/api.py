# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api role's runtime: the admin API's services, read model, previews, attribution, tunnels and sign-in.

It runs no loop of its own, since the node runs the process's definitions refresher and observation recorder, and at
start it creates the tunnel settings rows a fresh install lacks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.pool import QueuePool

from api.core.demand_tracker import DemandTracker
from api.core.roles import Role
from api.core.system_stats import ClusterReader
from api.core.tasks import quietly
from api.feed.compiler import FeedCompiler
from api.feed.state import RuntimeStateReader
from api.geo.attributor import ProxyAttributor
from api.geo.settings import defaults_from_config
from api.jobs.attribution import Attribution
from api.models.system import ApiSnapshot, InstanceSnapshot
from api.openvpn.service import OpenVpnService
from api.roles.runtime import RoleRuntime
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.services.connectors import ConnectorService
from api.services.credentials import CredentialService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService
from api.services.read_model import RuntimeView
from api.sso import SsoFlow
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
        self.project_service = ProjectService(session_factory, jobs, redis_client, changes=changes)
        self.credential_service = CredentialService(session_factory, self.provider_registry, changes=changes)
        self.connector_service = ConnectorService(
            session_factory, jobs, redis_client, self.provider_registry, changes=changes
        )
        self.proxy_service = ProxyService(session_factory, jobs, redis_client, self.provider_registry, changes=changes)
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
        self.attributor = ProxyAttributor(geo.geo_service, lookup_enabled=settings.geo_lookup_enabled)
        # Detect: a store per request (``attribution.store()``), written in one transaction.
        self.attribution = Attribution(
            self.attributor, session_factory, self.proxy_service, redis_client, changes=changes
        )
        # A manual refresh, and an add from a URL: the scheduled downloads are the workers' jobs.
        self.geo_updater = geo.updater(settings, session_factory=session_factory, changes=changes, jobs=jobs)
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

    # --- Lifecycle ----------------------------------------------------------------------

    def loops(self) -> Sequence[Loop]:
        """None: the node runs the process's definitions refresher and observation recorder."""
        return ()

    async def start(self) -> None:
        """Create the tunnel settings rows a fresh install lacks and announce each, so its first feed carries both protocols.

        Raises when Postgres cannot be written, which the node has just migrated.
        """
        await seed_tunnel_settings(self.session_factory, self._settings, changes=self.changes)
        self._note_default_proxy_endpoint()
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
        quarantined, blocked = await asyncio.gather(self.runtime_view.quarantined(), self.runtime_view.blocked())
        sections = await self._compiler.compile_project(project_id, quarantined=quarantined, blocked_until=blocked)
        return Router(
            RoutingSnapshot.from_sections(sections, registry=self.provider_registry),
            default_strategy=self._settings.default_strategy,
        )

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
