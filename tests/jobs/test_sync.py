# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider sync job: what it reads, what it writes through the services, and what it attributes.

Two providers: the built-in Oxylabs descriptor as a session pool ("All countries", two slots), whose sync needs no
vendor round trip, and a scripted plugin provider (:class:`Vendor`) for the refresh and list paths.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.roles import JobKind
from api.db.redis import RedisClient
from api.db.repository import (
    ConnectorRepository,
    ProviderDescriptorRepository,
    ProxyRepository,
)
from api.geo.models import META_ENDPOINT_COUNTRY, IpObservation, ObservationSource
from api.geo.runtime import GeoStack
from api.jobs.attribution import Attribution
from api.jobs.sync import ProviderSyncJob
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import Project
from api.models.provider import ProviderRecord
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.providers.sdk.strategies import META_DISCOVERED_IP, META_GEO_COUNTRY, META_SESSION_ID
from api.providers.store import ProviderStore
from api.roles.refresh import DefinitionsRefresher
from api.services.proxies import ProxyService
from tests.jobs.conftest import LONDON_IP, UNKNOWN_IP, Rows, announced, batch_of, recorded, waiting
from tests.jobs.test_refresh import ACME

OXYLABS = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
POOL = {"num_proxies": 2, "session_duration_minutes": 10}
INTERVAL = 3600.0

# A scripted answer: (rows to add or that changed, ids to remove), from the rows the job handed over.
Answer = Callable[[list[Proxy]], Awaitable[tuple[list[Proxy], list[str]]]]


@dataclass
class Vendor:
    """A provider whose answers the test sets: a port or list pool, as the job sees one."""

    refresh: Answer | None = None
    sync: Answer | None = None
    session_based: bool = False
    # What the job asked, in order.
    calls: list[str] = field(default_factory=list)


@pytest.fixture
def vendor(registry: ProviderRegistry) -> Vendor:
    script = Vendor()

    class ScriptedProvider:
        def __init__(self, connector: Connector, credential: Credential) -> None:
            self.connector = connector

        def is_session_based(self) -> bool:
            return script.session_based

        def needs_periodic_sync(self) -> bool:
            return False

        async def refresh_ips(self, proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            script.calls.append("refresh")
            return await script.refresh(proxies) if script.refresh else ([], [])

        async def sync_proxies(self, existing: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            script.calls.append("sync")
            return await script.sync(existing) if script.sync else ([], [])

    oxylabs = registry.get("oxylabs")
    assert oxylabs is not None and oxylabs.descriptor is not None
    registry.register_descriptor(
        oxylabs.descriptor.model_copy(update={"id": "scripted", "name": "Scripted"}),
        "plugin",
        provider_class=ScriptedProvider,
    )
    return script


@pytest.fixture
def job(
    sessions: async_sessionmaker[AsyncSession],
    registry: ProviderRegistry,
    proxy_service: ProxyService,
    attribution: Attribution,
    geo: GeoStack,
    redis_client: RedisClient,
) -> ProviderSyncJob:
    """The job on a worker whose definitions refresher has not read anything yet."""
    refresher = DefinitionsRefresher(
        ProviderStore(registry, sessions),
        geo.settings_store,
        geo.database_store,
        redis_client=redis_client,
        reconcile_interval=INTERVAL,
    )
    return ProviderSyncJob(sessions, registry, proxy_service, attribution, refresher=refresher)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


@pytest.fixture
async def pool(rows: Rows, project: Project) -> Connector:
    """An Oxylabs session pool of two slots per country, listing no country."""
    return await rows.connector(project, "oxylabs", credential=OXYLABS, config=POOL)


@pytest.fixture
async def scripted(rows: Rows, project: Project, vendor: Vendor) -> Connector:
    return await rows.connector(project, "scripted")


async def _set_config(sessions: async_sessionmaker[AsyncSession], connector: Connector, **config: Any) -> None:
    async with sessions() as session:
        edited = connector.model_copy(update={"config": {**connector.config, **config}})
        await ConnectorRepository(session).update(edited)
        await session.commit()


class TestSessionPool:
    async def test_the_first_sync_provisions_the_slots(
        self, job: ProviderSyncJob, rows: Rows, pool: Connector, redis_client: RedisClient
    ) -> None:
        await job.run(batch_of(pool.id))

        slots = await rows.of(pool)
        assert len(slots) == 2 and all(slot.status == ProxyStatus.HEALTHY for slot in slots)
        assert len({slot.metadata[META_SESSION_ID] for slot in slots}) == 2
        # Session slots are healthy from the start: no check asked for.
        assert await waiting(redis_client, "health") == set()

    async def test_a_sync_is_idempotent(self, job: ProviderSyncJob, rows: Rows, pool: Connector) -> None:
        await job.run(batch_of(pool.id))
        first = {slot.id for slot in await rows.of(pool)}
        await job.run(batch_of(pool.id))
        assert {slot.id for slot in await rows.of(pool)} == first

    async def test_a_smaller_pool_loses_slots_without_asking_for_another_sync(
        self,
        job: ProviderSyncJob,
        rows: Rows,
        pool: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await job.run(batch_of(pool.id))
        await _set_config(sessions, pool, num_proxies=1)

        await job.run(batch_of(pool.id))

        assert len(await rows.of(pool)) == 1
        assert await waiting(redis_client, "sync") == set()

    async def test_a_deleted_slot_comes_back_as_a_fresh_session(
        self,
        job: ProviderSyncJob,
        rows: Rows,
        pool: Connector,
        proxy_service: ProxyService,
        redis_client: RedisClient,
    ) -> None:
        await job.run(batch_of(pool.id))
        deleted = (await rows.of(pool))[0]

        await proxy_service.delete(deleted.id)
        assert await waiting(redis_client, "sync") == {pool.id}
        await job.run(batch_of(pool.id))

        slots = await rows.of(pool)
        assert len(slots) == 2 and deleted.id not in {slot.id for slot in slots}
        assert deleted.metadata[META_SESSION_ID] not in {slot.metadata[META_SESSION_ID] for slot in slots}

    async def test_an_on_demand_country_group_is_kept_in_sync(
        self, job: ProviderSyncJob, rows: Rows, pool: Connector
    ) -> None:
        await job.run(batch_of(pool.id))
        await rows.proxy(pool, metadata={META_SESSION_ID: "de-1", META_GEO_COUNTRY: "DE"})

        await job.run(batch_of(pool.id))

        groups = [slot.metadata.get(META_GEO_COUNTRY) for slot in await rows.of(pool)]
        assert sorted(groups, key=str) == sorted([None, None, "DE", "DE"], key=str)


class TestWhatIsSynced:
    def test_a_worker_syncs_four_connectors_at_once_one_a_run(self) -> None:
        assert (ProviderSyncJob.kind, ProviderSyncJob.batch_size, ProviderSyncJob.concurrency) == (JobKind.SYNC, 1, 4)

    async def test_each_connector_of_a_batch_is_synced(
        self, job: ProviderSyncJob, rows: Rows, project: Project, pool: Connector
    ) -> None:
        other = await rows.connector(project, "oxylabs", credential=OXYLABS, config=POOL)

        await job.run(batch_of(pool.id, other.id))

        assert (len(await rows.of(pool)), len(await rows.of(other))) == (2, 2)

    async def test_a_connector_that_is_gone_disabled_or_does_not_sync_is_left_alone(
        self, job: ProviderSyncJob, rows: Rows, project: Project, redis_client: RedisClient
    ) -> None:
        disabled = await rows.connector(project, "oxylabs", credential=OXYLABS, enabled=False)
        static = await rows.connector(project)

        await job.run(batch_of("gone", disabled.id, static.id))

        assert await rows.of(disabled) == [] and await rows.of(static) == []
        assert await announced(redis_client) == []

    async def test_a_provider_this_worker_has_not_read_yet_is_read_before_the_connector_is_judged(
        self,
        job: ProviderSyncJob,
        rows: Rows,
        project: Project,
        registry: ProviderRegistry,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """A custom provider created on the api moments before its connector: synced now, not an interval later."""
        async with sessions() as session:
            await ProviderDescriptorRepository(session).create(ProviderRecord(id="acme", name="Acme", spec=ACME))
            await session.commit()
        acme = await rows.connector(project, "acme", credential={"username": "alice"}, config={"num_proxies": 2})
        assert registry.get("acme") is None

        await job.run(batch_of(acme.id))

        assert len(await rows.of(acme)) == 2

    async def test_a_session_pool_is_not_refreshed(
        self, job: ProviderSyncJob, scripted: Connector, vendor: Vendor, rows: Rows
    ) -> None:
        vendor.session_based = True
        await rows.proxy(scripted)
        await job.run(batch_of(scripted.id))
        assert vendor.calls == ["sync"]

    async def test_an_empty_port_pool_is_only_reconciled(
        self, job: ProviderSyncJob, scripted: Connector, vendor: Vendor
    ) -> None:
        await job.run(batch_of(scripted.id))
        assert vendor.calls == ["sync"]


class TestRefresh:
    async def test_what_a_refresh_and_a_reconcile_change_is_written_and_their_exits_attributed(
        self,
        job: ProviderSyncJob,
        rows: Rows,
        scripted: Connector,
        vendor: Vendor,
        sessions: async_sessionmaker[AsyncSession],
        geo: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        moved = await rows.proxy(scripted, port=10001, metadata={META_DISCOVERED_IP: UNKNOWN_IP, "vendor_note": "old"})
        lost = await rows.proxy(scripted, port=10002)
        surplus = await rows.proxy(scripted, port=10003)
        handed: list[list[str]] = []

        async def refresh(proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            handed.append([p.id for p in proxies])
            # Written by another writer while the vendor answers: the refresh must not undo it.
            async with sessions() as session:
                await ProxyRepository(session).patch_metadata(moved.id, {"operator_note": "kept"}, (), None)
                await session.commit()
            changed = next(p for p in proxies if p.id == moved.id)
            changed.port = 10011
            changed.metadata[META_DISCOVERED_IP] = LONDON_IP
            del changed.metadata["vendor_note"]
            return [changed], [lost.id]

        async def sync(existing: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            handed.append([p.id for p in existing])
            added = Proxy(
                host="pr.example", port=10004, connector_id=scripted.id, status=ProxyStatus.HEALTHY,
                metadata={META_DISCOVERED_IP: LONDON_IP, META_ENDPOINT_COUNTRY: "GB"},
            )
            return [added], [surplus.id]

        vendor.refresh, vendor.sync = refresh, sync

        await job.run(batch_of(scripted.id))

        # The reconcile was handed the rows the refresh kept, as it left them.
        assert handed == [[moved.id, lost.id, surplus.id], [moved.id, surplus.id]]
        stored = {proxy.port: proxy for proxy in await rows.of(scripted)}
        assert set(stored) == {10011, 10004}
        assert stored[10011].metadata[META_DISCOVERED_IP] == LONDON_IP and "vendor_note" not in stored[10011].metadata
        assert stored[10011].metadata["operator_note"] == "kept"
        # Judged by attribution once the rows were written: London, by the database.
        assert stored[10011].country == "GB" and stored[10004].country == "GB"
        observations = [o for o in await recorded(geo, redis_client) if isinstance(o, IpObservation)]
        assert {(o.proxy_id, o.source) for o in observations} == {
            (moved.id, ObservationSource.DISCOVERY), (stored[10004].id, ObservationSource.DISCOVERY)
        }
        # Its own deletions ask for no further sync.
        assert await waiting(redis_client, "sync") == set()

    async def test_a_runs_refreshes_and_removals_are_one_write_announced_once(
        self, job: ProviderSyncJob, rows: Rows, scripted: Connector, vendor: Vendor, redis_client: RedisClient
    ) -> None:
        """A port pool whose exits moved: one transaction for the run, not one per row."""
        unknown = {META_DISCOVERED_IP: UNKNOWN_IP}
        ports = [await rows.proxy(scripted, port=10001 + n, metadata=dict(unknown)) for n in range(5)]

        async def refresh(proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            for proxy in proxies:
                proxy.metadata[META_DISCOVERED_IP] = LONDON_IP
            return proxies[:3], [proxy.id for proxy in proxies[3:]]

        vendor.refresh = refresh

        await job.run(batch_of(scripted.id))

        stored = await rows.of(scripted)
        assert [proxy.id for proxy in stored] == [proxy.id for proxy in ports[:3]]
        assert all(proxy.metadata[META_DISCOVERED_IP] == LONDON_IP for proxy in stored)
        # The run's write, then attribution's judgement of the exits it found: one entry each.
        assert await announced(redis_client) == [("proxy", ""), ("proxy", "")]

    async def test_a_vendor_error_writes_nothing_and_fails_the_run(
        self, job: ProviderSyncJob, rows: Rows, scripted: Connector, vendor: Vendor
    ) -> None:
        proxy = await rows.proxy(scripted)

        async def refuse(proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
            raise RuntimeError("vendor API unavailable")

        vendor.refresh = refuse
        with pytest.raises(RuntimeError, match="vendor API unavailable"):
            await job.run(batch_of(scripted.id))
        assert [p.id for p in await rows.of(scripted)] == [proxy.id]
