# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The attribution stack of one process: its two stores, its observation recorder, the service and the extraction rules.

Only the source of database files differs by role; the runtimes compose the rest, such as :meth:`GeoStack.updater`'s updater.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from api.core.config import Settings
from api.core.roles import JobKind
from api.db.redis import RedisClient
from api.db.session import SessionFactory
from api.db.streams import ChangeLog
from api.geo.extraction import EchoExtractionRules
from api.geo.observations import ObservationRecorder
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import BlobSource, GeoDatabaseStore
from api.geo.updater import GeoDatabaseUpdater
from api.jobs.attribution import REATTRIBUTE_ALL
from api.jobs.queue import JobQueue
from api.providers.registry import egress_policy_from_settings


@dataclass(frozen=True)
class GeoStack:
    """One process's attribution components, as :meth:`build` composes them."""

    settings_store: GeoSettingsStore
    database_store: GeoDatabaseStore
    observation_recorder: ObservationRecorder
    geo_service: GeoService
    extraction_rules: EchoExtractionRules

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        session_factory: SessionFactory | None,
        redis_client: RedisClient | None,
        blob_source: BlobSource | None,
    ) -> GeoStack:
        """The attribution stack of one process, nothing read yet: the Postgres roles load it with ``geo_service.resync()``, a gateway on its first feed.

        Args:
            session_factory: None on a gateway, whose stores the feed fills.
        """
        database_store = GeoDatabaseStore(
            session_factory,
            settings.geo_cache_dir,
            settings.geo_databases,
            blob_source=blob_source,
            # The configured files are looked at on disk again with the hourly read of everything else.
            config_check_interval=settings.reconcile_interval,
        )
        settings_store = GeoSettingsStore(settings, session_factory)
        recorder = ObservationRecorder(
            redis_client,
            max_buffer=settings.geo_observation_max_buffer,
            interval=settings.geo_observation_publish_interval,
        )
        geo_service = GeoService(settings, database_store, settings_store, recorder)
        return cls(
            settings_store=settings_store,
            database_store=database_store,
            observation_recorder=recorder,
            geo_service=geo_service,
            extraction_rules=EchoExtractionRules(geo_service),
        )

    def updater(
        self, settings: Settings, *, session_factory: SessionFactory, changes: ChangeLog, jobs: JobQueue
    ) -> GeoDatabaseUpdater:
        """The database downloader over this stack's store: a worker's ``geo_update`` job ticks it, and the api refreshes by hand.

        A new file of an enabled database asks ``jobs`` for every proxy to be judged again.
        """
        return GeoDatabaseUpdater(
            session_factory,
            self.database_store,
            changes=changes,
            after_update=partial(jobs.request_now, JobKind.REATTRIBUTE, [REATTRIBUTE_ALL]),
            egress_policy=egress_policy_from_settings(settings),
        )

    async def close(self) -> None:
        """Close every reader; the node stops the observation recorder with its own loops, after the runtimes."""
        self.database_store.close_all()
