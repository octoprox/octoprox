# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The controller role: the routing feed, and the timers that publish the periodic jobs the workers run.

Every controller process composes the same loops under the controller lease; only the one holding it acts, and the
others stand by until they lead.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog

from api.controller.feed_builder import FeedBuilder
from api.controller.producers import JobProducer
from api.core.leadership import Lease
from api.core.roles import JobKind
from api.core.tasks import quietly
from api.core.workers import LeaseName
from api.db.redis import TUNNEL_STATUS_INTERVAL
from api.feed.codec import FeedCodec
from api.feed.compiler import FullFeedCompiler
from api.feed.publisher import FeedPublisher
from api.geo.settings import defaults_from_config
from api.jobs.queue import JobQueue
from api.models.system import ControllerSnapshot
from api.roles.runtime import RoleRuntime

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.core.loops import Loop
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.geo.settings import GeoSettingsStore
    from api.providers.store import ProviderStore

logger = structlog.get_logger()

# The periods of the periodic kinds no setting times, in seconds.
SCALE_PERIOD_SECONDS = 30
COMPACTION_PERIOD_SECONDS = 3600
INDEX_HYGIENE_PERIOD_SECONDS = 60
# Hourly, so a new install checks within the hour; the job fetches only once a day.
UPDATE_CHECK_PERIOD_SECONDS = 3600


class ControllerRuntime(RoleRuntime):
    """The controller's feed builder and one job producer per periodic kind, under the lease it keeps from start to stop."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        provider_store: ProviderStore,
        geo_settings: GeoSettingsStore,
    ) -> None:
        super().__init__()
        self._settings = settings
        self.lease = Lease(redis_client, name=LeaseName.CONTROLLER, owner_id=settings.instance_id)
        self.jobs = JobQueue(redis_client)
        self.builder = FeedBuilder(
            FullFeedCompiler(
                session_factory,
                provider_store.registry,
                descriptor_sink=provider_store,
                geo_settings_sink=geo_settings,
                geo_defaults=defaults_from_config(settings),
                default_strategy=settings.default_strategy,
            ),
            FeedPublisher(
                redis_client,
                FeedCodec(settings.internal_secret),
                lease_key=self.lease.key,
                owner_id=settings.instance_id,
            ),
            self.lease,
            self.jobs,
            redis_client,
            registry=provider_store.registry,
            compile_interval=settings.feed_compile_interval,
            reconcile_interval=settings.reconcile_interval,
        )
        self.producers = [
            JobProducer(self.jobs, kind, self.builder, period=period, lease=self.lease)
            for kind, period in self._periods(settings).items()
        ]

    def loops(self) -> Sequence[Loop]:
        """The builder, then the producers."""
        return [self.builder, *self.producers]

    async def start(self) -> None:
        """Keep the lease, then start the loops, which act only while this process leads."""
        self.lease.keep()
        await super().start()
        logger.info("Controller started", instance=self._settings.instance_id)

    async def stop(self) -> None:
        """Stop the loops, then give the lease up so a standby leads within a poll; never raises."""
        await super().stop()
        await quietly("controller lease", self.lease.release())
        logger.info("Controller stopped", instance=self._settings.instance_id)

    def readiness(self) -> str | None:
        """None when ready, else why not; a standby is ready, and Postgres is the heartbeat's to report."""
        if self.lease.last_error is not None:
            return "Redis unreachable"
        if self.lease.is_held and self.builder.publish_error is not None:
            return "routing feed not published"
        return None

    def snapshot(self) -> ControllerSnapshot:
        """This process's controller state, for its heartbeat and its metrics."""
        builder = self.builder
        compiled = builder.compiled
        return ControllerSnapshot(
            is_leader=self.lease.is_held,
            lease_terms=self.lease.term,
            changes=compiled.changes if compiled is not None else None,
            compiled_at=compiled.compiled_at if compiled is not None else None,
            compiles=builder.compiles,
            compile_ms=builder.compile_ms,
            compile_error=builder.compile_error,
            publishes=builder.publishes,
            published_at=builder.published_at,
            publish_ms=builder.publish_ms,
            publish_error=builder.publish_error,
            sections=len(builder.sections),
            sections_written=builder.sections_written,
            sections_removed=builder.sections_removed,
            quarantined=builder.quarantined,
            blocked_connectors=builder.blocked_connectors,
        )

    @staticmethod
    def _periods(settings: Settings) -> dict[JobKind, float]:
        """Each periodic kind's period, in seconds; no snapshots while ``system_metrics_interval`` is 0, no update checks while ``update_check`` is off."""
        periods: dict[JobKind, float] = {
            JobKind.HEALTH: settings.health_check_interval,
            JobKind.SYNC: settings.ip_refresh_interval,
            JobKind.SCALE: SCALE_PERIOD_SECONDS,
            JobKind.METRICS_FLUSH: settings.metrics_flush_interval,
            JobKind.COMPACTION: COMPACTION_PERIOD_SECONDS,
            JobKind.SNAPSHOT: settings.system_metrics_interval,
            JobKind.GEO_UPDATE: settings.geo_updater_interval,
            JobKind.TUNNEL_LAST_SEEN: TUNNEL_STATUS_INTERVAL,
            JobKind.INDEX_HYGIENE: INDEX_HYGIENE_PERIOD_SECONDS,
            JobKind.UPDATE_CHECK: UPDATE_CHECK_PERIOD_SECONDS,
        }
        if not settings.system_metrics_interval:
            del periods[JobKind.SNAPSHOT]
        if not settings.update_check:
            del periods[JobKind.UPDATE_CHECK]
        return periods
