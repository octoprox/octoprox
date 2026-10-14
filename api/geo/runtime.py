# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Builds and runs the attribution stack for one process.

The lifespan constructs one :class:`GeoRuntime` right after Postgres and Redis
are reachable and starts it. Everything that needs a piece of it is then
composed in the lifespan, explicitly: the proxy manager receives the
extraction rules, the two cross-instance handlers and the reload hook through
its constructor; the attributor is started against the manager as its proxy
store; the verifier is built from the preflight checker and the manager as
its selector and handed to the proxy server. The admin routes read the whole
runtime off ``app.state.geo_runtime``. The proxy manager imports nothing from this
package but data types.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

import structlog

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.signals import geo_database_changed
from api.core.tasks import spawn_held
from api.core.workers import WorkerName
from api.db.redis import RedisClient
from api.db.session import SessionFactory
from api.geo.attributor import ProxyAttributor
from api.geo.extraction import EchoExtractionRules
from api.geo.observations import ObservationFlusher, ObservationRecorder
from api.geo.preflight import PreflightChecker
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.geo.updater import GeoDatabaseUpdater
from api.providers.sdk.egress import EgressPolicy

logger = structlog.get_logger()


class GeoRuntime:
    """The attribution components of one instance and their lifecycle."""

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        redis_client: RedisClient,
    ) -> None:
        self._settings = settings
        self.database_store = GeoDatabaseStore(session_factory, settings.geo_cache_dir, settings.geo_databases)
        self.settings_store = GeoSettingsStore(settings, session_factory)
        self.observation_recorder = ObservationRecorder(
            redis_client,
            max_buffer=settings.geo_observation_max_buffer,
            interval=settings.geo_observation_publish_interval,
        )
        self.geo_service = GeoService(settings, self.database_store, self.settings_store, self.observation_recorder)
        self.observation_flusher = ObservationFlusher(
            session_factory,
            redis_client,
            settings.instance_id,
            interval=settings.geo_observation_flush_interval,
            retention_days=lambda: self.geo_service.settings.observation_retention_days,
            exit_ip_retention_days=lambda: self.geo_service.settings.exit_ip_retention_days,
        )
        self.database_updater = GeoDatabaseUpdater(
            session_factory,
            redis_client,
            settings.instance_id,
            self.database_store,
            self.publish_database_changed,
            after_update=self._reattribute_after_update,
            interval=settings.geo_updater_interval,
            egress_policy=EgressPolicy(
                allow_http=settings.provider_egress_allow_http,
                allow_private=settings.provider_egress_allow_private,
                pin_dns=not settings.provider_egress_allow_private,
            ),
        )
        self.preflight_checker = PreflightChecker(self.geo_service, redis_client)
        self.proxy_attributor = ProxyAttributor(self.geo_service, settings)
        self.extraction_rules = EchoExtractionRules(self.geo_service)
        self._tasks: list[asyncio.Task[None]] = []
        self._passes: set[asyncio.Task[Any]] = set()

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Load the policy and databases, then run the background loops."""
        await self.settings_store.load()
        await self.database_store.sync_all()
        self._spawn(WorkerName.GEO_OBSERVATION_PUBLISHER, self.observation_recorder.run())
        self._spawn(WorkerName.GEO_OBSERVATION_FLUSHER, self.observation_flusher.run())
        self._spawn(WorkerName.GEO_DATABASE_UPDATER, self.database_updater.run())
        self.preflight_checker.start()
        logger.info("IP attribution started", databases=len(self.database_store.loaded))

    # --- what the proxy manager runs on our behalf -----------------------------------

    async def reload_database(self, database_id: str, op: str | None) -> None:
        """Cross-instance handler for ``geo_database_changed``."""
        await self.database_store.reload_one(database_id, op)

    async def reload_settings(self, _entity_id: str, _op: str | None) -> None:
        """Cross-instance handler for ``geo_settings_changed``."""
        await self.geo_service.apply_settings_change()

    async def _reattribute_after_update(self) -> None:
        """A scheduled download stored a new build: re-judge every proxy with it, once, here on the leader."""
        await self.proxy_attributor.reattribute_all()

    def reattribute_later(self, project_id: str | None = None, *, inheriting_rule_only: bool = False) -> None:
        """Re-judge every row, or one project's rows, without holding the request that asked.

        Used after a database changed, after the install's default conflict
        rule changed (then only for projects that inherit it), and after a
        project's location decisions or rule changed: a row's value and
        verdict were written under the settings of the time, and a sighting
        of an unchanged exit is not re-applied. Passes never interleave: the
        attributor runs one at a time, whatever asked for them, and nothing
        is cancelled mid-way, since a pass judges rows in memory as it goes
        and persists them at the end. The task is held here, apart from the
        periodic loops, so it is neither collected before it runs nor left
        behind at shutdown, and a finished pass is forgotten.
        """

        spawn_held(
            self._passes,
            self.proxy_attributor.reattribute_all(project_id=project_id, inheriting_rule_only=inheriting_rule_only),
            name="reattribute",
            project_id=project_id,
        )

    async def resync(self) -> None:
        """Reload hook: re-read settings and databases on the periodic full reload."""
        await self.geo_service.resync()

    async def stop(self) -> None:
        """Stop the loops, hand the last observations to Redis, close the readers."""
        # A re-judgement pass persists at its end; cancelling it would discard what it judged.
        passes = list(self._passes)
        if passes:
            await asyncio.gather(*passes, return_exceptions=True)
        await self.proxy_attributor.stop()
        self.preflight_checker.stop()
        self.observation_recorder.stop()
        self.observation_flusher.stop()
        self.database_updater.stop()
        with contextlib.suppress(Exception):
            await self.observation_recorder.publish()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self.database_store.close_all()

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        """The loops this runtime owns, for the admin workers view."""
        return list(self._tasks)

    # --- cross-instance changes ----------------------------------------------------

    async def publish_database_changed(self, database_id: str, op: str) -> None:
        """Tell every instance (this one included) that a database row changed."""
        await event_bus.publish(geo_database_changed, self, entity_id=database_id, op=op)
