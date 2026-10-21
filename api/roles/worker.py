# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The worker runtime: what a ``worker`` process (and ``all``) runs besides the node's own loops.

A job runner per kind it takes (``worker_jobs``), the sighting and observation ingests and the definitions refresher,
all reading Redis streams; what its jobs write is announced on the change stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core.demand_tracker import DemandTracker
from api.core.job_stats import job_stats
from api.core.roles import JobKind
from api.geo.attributor import ProxyAttributor
from api.geo.observations import ObservationIngest
from api.jobs.attribution import Attribution, EnrichJob, ReattributeJob, SightingIngest
from api.jobs.health import HealthCheckJob
from api.jobs.housekeeping import GeoUpdateJob, IndexHygieneJob, SnapshotJob, TunnelLastSeenJob
from api.jobs.metrics import CompactionJob, MetricsFlushJob
from api.jobs.provision import ProvisionJob
from api.jobs.queue import JobQueue
from api.jobs.runner import JobRunner
from api.jobs.scale import AutoscaleJob
from api.jobs.sync import ProviderSyncJob
from api.jobs.traffic import TrafficJob
from api.models.system import JobKindStats, WorkerSnapshot
from api.roles.refresh import DefinitionsRefresher
from api.roles.runtime import RoleRuntime
from api.services.connectors import ConnectorService
from api.services.proxies import ProxyService

if TYPE_CHECKING:
    from collections.abc import Sequence

    from api.core.config import Settings
    from api.core.loops import Loop
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.geo.runtime import GeoStack
    from api.jobs.job import Job
    from api.providers.store import ProviderStore

logger = structlog.get_logger()


class WorkerRuntime(RoleRuntime):
    """Runs a job runner per kind it takes, the sighting and observation ingests, and the definitions refresher.

    The node makes the refresher's first read before any runtime starts, so the first runs judge with the definitions.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: RedisClient,
        session_factory: SessionFactory,
        provider_store: ProviderStore,
        geo: GeoStack,
        changes: ChangeLog,
    ) -> None:
        super().__init__()
        registry = provider_store.registry
        self.jobs = JobQueue(redis_client)
        self.refresher = DefinitionsRefresher(
            provider_store,
            geo.settings_store,
            geo.database_store,
            redis_client=redis_client,
            reconcile_interval=settings.reconcile_interval,
        )
        proxies = ProxyService(
            session_factory,
            self.jobs,
            redis_client,
            registry,
            lookup_enabled=settings.geo_lookup_enabled,
            changes=changes,
        )
        connectors = ConnectorService(session_factory, self.jobs, redis_client, registry, changes=changes)
        attribution = Attribution(
            ProxyAttributor(geo.geo_service, lookup_enabled=settings.geo_lookup_enabled),
            session_factory,
            proxies,
            redis_client,
            changes=changes,
        )
        updater = geo.updater(settings, session_factory=session_factory, changes=changes, jobs=self.jobs)
        every_job: list[Job] = [
            HealthCheckJob(
                session_factory,
                redis_client,
                proxies,
                attribution,
                geo.extraction_rules,
                timeout=settings.health_check_timeout,
                probes=settings.health_check_concurrency,
            ),
            ProviderSyncJob(session_factory, registry, proxies, attribution, refresher=self.refresher),
            AutoscaleJob(session_factory, redis_client, registry, proxies, connectors, DemandTracker(redis_client)),
            ProvisionJob(session_factory, registry, proxies),
            ReattributeJob(attribution, geo.geo_service),
            EnrichJob(attribution),
            TrafficJob(session_factory, redis_client),
            MetricsFlushJob(session_factory, redis_client),
            CompactionJob(session_factory, geo.geo_service),
            SnapshotJob(
                session_factory,
                redis_client,
                interval=settings.system_metrics_interval,
                retention_days=settings.system_metrics_retention_days,
            ),
            GeoUpdateJob(updater),
            TunnelLastSeenJob(session_factory, redis_client),
            IndexHygieneJob(redis_client, self.jobs),
        ]
        by_kind = {job.kind: job for job in every_job}
        # One runner per kind this worker takes, in the order the settings list them.
        self.runners = {kind: JobRunner(redis_client, by_kind[kind]) for kind in settings.worker_jobs}
        self.sighting_ingest = SightingIngest(redis_client, attribution)
        self.observation_ingest = ObservationIngest(redis_client, session_factory)
        self._loops: tuple[Loop, ...] = (
            self.refresher,
            *self.runners.values(),
            self.sighting_ingest,
            self.observation_ingest,
        )

    def loops(self) -> Sequence[Loop]:
        """The definitions refresher, a runner per kind it takes, then the sighting and observation ingests."""
        return self._loops

    async def start(self) -> None:
        """Start every loop, each a task named after it."""
        await super().start()
        logger.info("Worker started", jobs=[kind.value for kind in self.runners])

    def readiness(self) -> str | None:
        """None while the worker can work: started, with its definitions read; else why not.

        Whether Postgres and Redis answer is the node's heartbeat's to say; a runner or an ingest that fails tries again.
        """
        if not self.started:
            return "worker not started"
        return self.refresher.readiness()

    def snapshot(self) -> WorkerSnapshot:
        """This worker's counters: per job kind, and the sightings and observations it ingested."""
        jobs: dict[JobKind, JobKindStats] = {}
        for kind, runner in self.runners.items():
            stats = job_stats.get(runner.name)
            jobs[kind] = JobKindStats(
                runs=stats.runs if stats else 0,
                failures=stats.failures if stats else 0,
                duration_seconds=stats.total_duration_ms / 1000 if stats else 0.0,
                in_flight=runner.in_flight,
            )
        return WorkerSnapshot(
            jobs=jobs,
            sightings_consumed=self.sighting_ingest.consumed,
            sightings_dropped=self.sighting_ingest.dropped,
            observations_consumed=self.observation_ingest.consumed,
            observations_dropped=self.observation_ingest.dropped,
        )
