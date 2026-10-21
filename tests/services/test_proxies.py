# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the proxy service against the real Postgres and Redis test containers.

The admin's writes go to a static connector and the jobs' to Oxylabs session pools and an AWS connector, and what they
did is read back from the repository, the changes and runtime streams, and the job streams.
"""

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.roles import JobKind
from api.db.redis import PROXY_REQUESTS_KEY, QUARANTINE_KEY, RedisClient
from api.db.repository import ProxyRepository
from api.db.streams import ChangeKind, ChangeLog
from api.geo.models import META_LOCATION_CHECKED_AT
from api.jobs.queue import JobQueue
from api.models.connector import Connector, ConnectorUpdate
from api.models.project import Project
from api.models.proxy import Proxy, ProxyCreate, ProxyProtocol, ProxyStatus, ProxyUpdate
from api.providers.registry import ProviderRegistry
from api.services.connectors import ConnectorService
from api.services.errors import NotFoundError, WriteRejectedError
from api.services.proxies import ProxyService, StatusMove, SyncEdit
from tests.conftest import queued_job_keys
from tests.services.conftest import (
    OXYLABS_POOL,
    Connect,
    Executed,
    Witness,
    announced,
    forget_announcements,
    runtime_entries,
)


async def stored(sessions: async_sessionmaker[AsyncSession], proxy_id: str) -> Proxy | None:
    async with sessions() as session:
        return await ProxyRepository(session).get_by_id(proxy_id)


def slot(connector: Connector, status: ProxyStatus = ProxyStatus.HEALTHY) -> Proxy:
    """A row as a job builds it: a vendor session slot, or a cloud instance."""
    return Proxy(host="pr.oxylabs.io", port=7777, connector_id=connector.id, status=status)


@pytest.fixture
async def manual(
    proxy_service: ProxyService, project: Project, static_connector: Connector, redis_client: RedisClient
) -> Proxy:
    """A proxy added by hand, with the jobs its add requested cleared away."""
    proxy = await proxy_service.create(
        project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
    )
    await redis_client.client.flushdb()
    return proxy


class TestCreate:
    async def test_a_proxy_added_by_hand_is_checked_and_located_now(
        self,
        proxy_service: ProxyService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        proxy = await proxy_service.create(
            project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id, tags=["a"])
        )

        assert await stored(sessions, proxy.id) == proxy
        assert proxy.status == ProxyStatus.UNKNOWN
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [proxy.id]
        assert await queued_job_keys(redis_client, JobKind.ENRICH) == [proxy.id]

    async def test_a_country_set_by_hand_is_pinned_and_not_looked_up(
        self, proxy_service: ProxyService, project: Project, static_connector: Connector, redis_client: RedisClient
    ) -> None:
        pinned = ProxyCreate(
            host="10.0.0.1", port=8080, connector_id=static_connector.id, country="de", state="by", city="Munich"
        )

        proxy = await proxy_service.create(project.id, pinned)

        assert (proxy.country, proxy.metadata["country_source"]) == ("DE", "manual")
        assert (proxy.state_code, proxy.city_slug) == ("BY", "munich")
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [proxy.id]
        assert await queued_job_keys(redis_client, JobKind.ENRICH) == []

    async def test_while_lookups_are_off_no_lookup_is_asked_for(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jobs: JobQueue,
        redis_client: RedisClient,
        registry: ProviderRegistry,
        change_log: ChangeLog,
        project: Project,
        static_connector: Connector,
    ) -> None:
        """``geo_lookup_enabled`` off: the enrich job would only drop the run, so none is queued."""
        service = ProxyService(sessions, jobs, redis_client, registry, changes=change_log, lookup_enabled=False)

        added = ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
        proxy = await service.create(project.id, added)
        upload = await service.upload(project.id, static_connector.id, "http://10.0.0.2:8080")

        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [proxy.id, upload.added[0].id]
        assert await queued_job_keys(redis_client, JobKind.ENRICH) == []

    async def test_only_a_static_connector_of_the_project_takes_proxies_by_hand(
        self,
        proxy_service: ProxyService,
        project: Project,
        other_project: Project,
        pool: Connector,
        static_connector: Connector,
    ) -> None:
        def add(connector_id: str) -> ProxyCreate:
            return ProxyCreate(host="10.0.0.1", port=8080, connector_id=connector_id)

        with pytest.raises(WriteRejectedError, match="only be manually added to STATIC_PROXY_PROVIDER"):
            await proxy_service.create(project.id, add(pool.id))
        with pytest.raises(WriteRejectedError, match="Connector does not belong to this project"):
            await proxy_service.create(other_project.id, add(static_connector.id))
        with pytest.raises(NotFoundError, match="Connector not found"):
            await proxy_service.create(project.id, add("nope"))
        with pytest.raises(NotFoundError, match="Project not found"):
            await proxy_service.create("nope", add(static_connector.id))


class TestUpload:
    async def test_adds_the_lines_it_reads_and_reports_the_rest(
        self,
        proxy_service: ProxyService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        text = "http://10.0.0.1:8080\n\n  socks5://user:pass@10.0.0.2:1080  \nnot a proxy\nftp://10.0.0.3:21\n"

        upload = await proxy_service.upload(project.id, static_connector.id, text)

        assert upload.total_lines == 4
        assert [(p.host, p.port, p.protocol, p.username) for p in upload.added] == [
            ("10.0.0.1", 8080, ProxyProtocol.HTTP, None),
            ("10.0.0.2", 1080, ProxyProtocol.SOCKS5, "user"),
        ]
        assert [(e.line_number, e.line) for e in upload.errors] == [(4, "not a proxy"), (5, "ftp://10.0.0.3:21")]
        async with sessions() as session:
            added = {p.id for p in await ProxyRepository(session).get_by_project(project.id)}
        assert added == {p.id for p in upload.added}
        # Each line's first check and exit lookup, in the file's order.
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [p.id for p in upload.added]
        assert await queued_job_keys(redis_client, JobKind.ENRICH) == [p.id for p in upload.added]

    async def test_only_a_static_connector_takes_an_upload(
        self, proxy_service: ProxyService, project: Project, pool: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(WriteRejectedError, match="only be uploaded to STATIC_PROXY_PROVIDER"):
            await proxy_service.upload(project.id, pool.id, "http://10.0.0.1:8080")

        async with sessions() as session:
            assert await ProxyRepository(session).get_by_project(project.id) == []


class TestJobAdds:
    async def test_rows_keep_their_built_status_and_unchecked_ones_are_checked_now(
        self,
        proxy_service: ProxyService,
        pool: Connector,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """A vendor session slot is healthy from the start; a cloud instance initializes until its grace ends."""
        await redis_client.client.flushdb()
        slots = [slot(pool), slot(pool)]
        instance = slot(cloud, ProxyStatus.INITIALIZING)

        assert await proxy_service.bulk_add(slots) == slots
        await proxy_service.add(instance)

        assert [await stored(sessions, p.id) for p in [*slots, instance]] == [*slots, instance]
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [instance.id]
        # Only a static proxy's exit is looked up through it.
        assert await queued_job_keys(redis_client, JobKind.ENRICH) == []

    async def test_adding_a_row_whose_id_is_stored_keeps_the_stored_one_and_says_nothing(
        self,
        proxy_service: ProxyService,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """A row whose ID is stored already is kept as it is."""
        instance = slot(cloud, ProxyStatus.INITIALIZING)
        await proxy_service.add(instance)
        await redis_client.client.flushdb()

        await proxy_service.add(instance.model_copy(update={"host": "10.9.9.9"}))

        assert await stored(sessions, instance.id) == instance
        assert await announced(redis_client) == []
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == []

    async def test_a_batch_announces_and_checks_only_the_rows_it_stored(
        self,
        proxy_service: ProxyService,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        instance = slot(cloud, ProxyStatus.INITIALIZING)
        await proxy_service.add(instance)
        await redis_client.client.flushdb()
        fresh = slot(cloud, ProxyStatus.INITIALIZING)

        assert await proxy_service.bulk_add([instance, fresh]) == [fresh]
        assert await proxy_service.bulk_add([instance]) == []

        assert [await stored(sessions, p.id) for p in (instance, fresh)] == [instance, fresh]
        assert await announced(redis_client) == [("proxy", fresh.id)]
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == [fresh.id]

    async def test_rows_of_a_gone_connector_are_refused_whole(
        self, proxy_service: ProxyService, pool: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        rows = [slot(pool), Proxy(host="10.0.0.1", port=8080, connector_id="gone")]

        with pytest.raises(NotFoundError, match="Connector not found"):
            await proxy_service.bulk_add(rows)

        assert await stored(sessions, rows[0].id) is None
        assert await proxy_service.bulk_add([]) == []


class TestUpdate:
    async def test_applies_the_definition_and_never_the_status(
        self, proxy_service: ProxyService, project: Project, manual: Proxy, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await proxy_service.drain(manual.id)
        edit = ProxyUpdate(host="10.9.9.9", port=3128, tags=["x"], metadata={"note": "kept"})

        saved = await proxy_service.update(project.id, manual.id, edit)

        assert (saved.host, saved.port, saved.tags, saved.metadata) == ("10.9.9.9", 3128, ["x"], {"note": "kept"})
        assert saved.status == ProxyStatus.DRAINING
        assert await stored(sessions, manual.id) == saved

    async def test_pins_and_unpins_the_country_and_place(
        self, proxy_service: ProxyService, project: Project, manual: Proxy
    ) -> None:
        pinned = await proxy_service.update(project.id, manual.id, ProxyUpdate(country="FR", state="IDF", city="Paris"))
        assert (pinned.country, pinned.metadata["country_source"], pinned.state_code, pinned.city_slug) == (
            "FR", "manual", "IDF", "paris",
        )

        unpinned = await proxy_service.update(project.id, manual.id, ProxyUpdate(country="", state="", city=""))
        assert (unpinned.country, unpinned.state_code, unpinned.city_slug) == (None, None, None)
        assert "country_source" not in unpinned.metadata

    async def test_an_edit_waits_for_a_write_in_flight_and_keeps_what_it_wrote(
        self, proxy_service: ProxyService, project: Project, manual: Proxy, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            # Attribution's patch, written and not committed yet: it holds the row.
            assert await ProxyRepository(session).patch_metadata(manual.id, {"note": "patched"}, (), None)
            edit = asyncio.create_task(proxy_service.update(project.id, manual.id, ProxyUpdate(country="FR")))
            await asyncio.sleep(0.3)
            assert not edit.done()
            await session.commit()
        saved = await asyncio.wait_for(edit, timeout=5)

        # The edit read the row once the patch had committed, so it wrote the patch back with its own change.
        assert (saved.metadata["note"], saved.metadata["country"]) == ("patched", "FR")
        assert await stored(sessions, manual.id) == saved

    async def test_a_proxy_of_another_project_is_not_found(
        self, proxy_service: ProxyService, other_project: Project, manual: Proxy
    ) -> None:
        with pytest.raises(NotFoundError, match="Proxy not found"):
            await proxy_service.update(other_project.id, manual.id, ProxyUpdate(host="mine"))
        with pytest.raises(NotFoundError, match="Proxy not found"):
            await proxy_service.remove(other_project.id, manual.id)
        with pytest.raises(NotFoundError, match="Proxy not found"):
            await proxy_service.unquarantine(other_project.id, manual.id)


class TestDelete:
    async def test_a_static_proxy_is_deleted_and_nothing_follows(
        self,
        proxy_service: ProxyService,
        project: Project,
        manual: Proxy,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await proxy_service.remove(project.id, manual.id)

        assert await stored(sessions, manual.id) is None
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []

    async def test_a_synced_slot_is_filled_again(
        self,
        proxy_service: ProxyService,
        project: Project,
        pool: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        removed, other = await proxy_service.bulk_add([slot(pool), slot(pool)])
        await redis_client.client.flushdb()

        await proxy_service.remove(project.id, removed.id)
        assert await stored(sessions, removed.id) is None
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]

        # The sync job's own removals do not ask for another sync.
        await redis_client.client.flushdb()
        await proxy_service.apply_sync([], [other.id])
        assert await stored(sessions, other.id) is None
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []
        assert await proxy_service.delete(other.id) is False

    async def test_a_disabled_connector_is_not_synced(
        self,
        proxy_service: ProxyService,
        connector_service: ConnectorService,
        project: Project,
        pool: Connector,
        redis_client: RedisClient,
    ) -> None:
        added = slot(pool)
        await proxy_service.add(added)
        await connector_service.update(project.id, pool.id, ConnectorUpdate(enabled=False))
        await redis_client.client.flushdb()

        assert await proxy_service.delete(added.id) is True

        assert await queued_job_keys(redis_client, JobKind.SYNC) == []

    async def test_a_cloud_proxy_is_terminated_by_the_scale_job(
        self,
        proxy_service: ProxyService,
        project: Project,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """The instance has to go first: the row is marked and a scale run deletes both."""
        instance = slot(cloud)
        await proxy_service.add(instance)
        await redis_client.client.flushdb()

        await proxy_service.remove(project.id, instance.id)

        row = await stored(sessions, instance.id)
        assert row is not None and row.status == ProxyStatus.TERMINATING
        assert await queued_job_keys(redis_client, JobKind.SCALE) == [cloud.id]
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []


class TestDeleteMany:
    async def test_slots_of_one_connector_are_one_transaction_one_entry_and_one_sync(
        self,
        proxy_service: ProxyService,
        pool: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """As attribution rotates a batch's refused slots: a burst of rotations is one sync, not one a slot."""
        slots = await proxy_service.bulk_add([slot(pool), slot(pool), slot(pool)])
        kept = slot(pool)
        await proxy_service.add(kept)
        await redis_client.client.flushdb()

        deleted = await proxy_service.delete_many([p.id for p in reversed(slots)])

        assert deleted == sorted(p.id for p in slots)
        assert [await stored(sessions, p.id) for p in slots] == [None, None, None]
        assert await stored(sessions, kept.id) == kept
        assert await announced(redis_client) == [("proxy", "")]
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]

    async def test_slots_of_two_connectors_ask_for_a_sync_each(
        self,
        proxy_service: ProxyService,
        connect: Connect,
        project: Project,
        pool: Connector,
        static_connector: Connector,
        manual: Proxy,
        redis_client: RedisClient,
    ) -> None:
        """Only connectors that sync are asked: a static proxy deleted beside them asks for nothing."""
        other = await connect(project, "Pool 2", OXYLABS_POOL)
        rows = await proxy_service.bulk_add([slot(pool), slot(other), slot(pool), slot(other)])
        await redis_client.client.flushdb()

        deleted = await proxy_service.delete_many([*(p.id for p in rows), manual.id])

        assert deleted == sorted([*(p.id for p in rows), manual.id])
        assert await queued_job_keys(redis_client, JobKind.SYNC) == sorted([pool.id, other.id])
        assert await announced(redis_client) == [("proxy", "")]

    async def test_rows_gone_already_are_left_out_and_one_row_is_named(
        self,
        proxy_service: ProxyService,
        pool: Connector,
        redis_client: RedisClient,
    ) -> None:
        first, second = await proxy_service.bulk_add([slot(pool), slot(pool)])
        assert await proxy_service.delete(first.id) is True
        await redis_client.client.flushdb()

        assert await proxy_service.delete_many([first.id, second.id, "nope", second.id]) == [second.id]

        assert await announced(redis_client) == [("proxy", second.id)]
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]

    async def test_nothing_deleted_announces_and_asks_nothing(
        self, proxy_service: ProxyService, pool: Connector, redis_client: RedisClient
    ) -> None:
        await redis_client.client.flushdb()

        assert await proxy_service.delete_many([]) == []
        assert await proxy_service.delete_many(["nope"]) == []

        assert await announced(redis_client) == []
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []


class TestStatus:
    async def test_drain_and_terminate_set_the_status_once(
        self, proxy_service: ProxyService, manual: Proxy, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await proxy_service.drain(manual.id) is True
        drained = await stored(sessions, manual.id)
        assert drained is not None and drained.status == ProxyStatus.DRAINING
        assert drained.status_changed_at is not None
        assert await proxy_service.drain(manual.id) is False

        assert await proxy_service.terminate(manual.id) is True
        assert await proxy_service.terminate(manual.id) is False
        assert await proxy_service.drain("nope") is False

    async def test_the_health_compare_and_set_never_leaves_draining(
        self, proxy_service: ProxyService, manual: Proxy, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        healthy = StatusMove(ProxyStatus.HEALTHY, ProxyStatus.UNKNOWN)
        assert await proxy_service.set_statuses_if({manual.id: healthy}) == {manual.id}
        # The status moved since it was read: the check's result is stale.
        stale = StatusMove(ProxyStatus.UNHEALTHY, ProxyStatus.UNKNOWN)
        assert await proxy_service.set_statuses_if({manual.id: stale}) == set()

        await proxy_service.drain(manual.id)
        out_of_draining = StatusMove(ProxyStatus.HEALTHY, ProxyStatus.DRAINING)
        assert await proxy_service.set_statuses_if({manual.id: out_of_draining}) == set()
        row = await stored(sessions, manual.id)
        assert row is not None and row.status == ProxyStatus.DRAINING



class TestApplySync:
    async def test_a_syncs_edits_and_removals_are_one_transaction_merged_key_by_key(
        self,
        proxy_service: ProxyService,
        pool: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        moved, renamed, dropped = await proxy_service.bulk_add([
            slot(pool).model_copy(update={"metadata": {"exit_ip": "1.1.1.1", "vendor_note": "old"}}),
            slot(pool).model_copy(update={"username": "u", "password": "p"}),
            slot(pool),
        ])
        # Written by attribution while the vendor was asked: it stands.
        async with sessions() as session:
            seen = utc_now().isoformat()
            await ProxyRepository(session).patch_metadata(moved.id, {META_LOCATION_CHECKED_AT: seen}, (), None)
            await session.commit()
        await redis_client.client.flushdb()

        await proxy_service.apply_sync(
            [
                SyncEdit(moved.id, {}, {"exit_ip": "2.2.2.2"}, {"vendor_note"}),
                SyncEdit(renamed.id, {"port": 7778, "username": None, "password": None}, {}, ()),
            ],
            [dropped.id],
        )

        # One transaction, one entry, whatever it changed: a thousand moved exits cost one compile.
        assert await announced(redis_client) == [("proxy", "")]
        row = await stored(sessions, moved.id)
        assert row is not None and row.metadata == {"exit_ip": "2.2.2.2", META_LOCATION_CHECKED_AT: seen}
        row = await stored(sessions, renamed.id)
        # A credential the vendor's list no longer carries is cleared.
        assert row is not None and (row.port, row.username, row.password) == (7778, None, None)
        assert await stored(sessions, dropped.id) is None
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []


class TestUnquarantine:
    async def test_lifts_the_quarantine_and_empties_the_rate_window(
        self, proxy_service: ProxyService, project: Project, manual: Proxy, redis_client: RedisClient
    ) -> None:
        window = PROXY_REQUESTS_KEY.format(proxy_id=manual.id)
        await redis_client.client.zadd(QUARANTINE_KEY, {manual.id: time.time() + 600, "other": time.time() + 600})
        await redis_client.client.zadd(window, {"r1": time.time()})

        assert await proxy_service.unquarantine(project.id, manual.id) is True

        assert await redis_client.client.zrange(QUARANTINE_KEY, 0, -1) == ["other"]
        assert await redis_client.client.exists(window) == 0
        # The controller is told, so gateways route to it again at once; no definition changed.
        assert await runtime_entries(redis_client) == [("quarantine", manual.id)]
        assert await announced(redis_client) == []

    async def test_the_lift_the_window_and_the_entry_go_in_one_multi(
        self,
        proxy_service: ProxyService,
        project: Project,
        manual: Proxy,
        redis_client: RedisClient,
        pipelines: Executed,
    ) -> None:
        """Never one without the other: an entry without the lift, or a lift the controller is never told of."""
        await redis_client.client.zadd(QUARANTINE_KEY, {manual.id: time.time() + 600})

        assert await proxy_service.unquarantine(project.id, manual.id) is True

        assert pipelines == [(True, ["ZREM", "DEL", "XADD"])]

    async def test_a_proxy_not_quarantined_is_left_alone(
        self, proxy_service: ProxyService, project: Project, manual: Proxy, redis_client: RedisClient
    ) -> None:
        """A refused lift changes nothing: its rate window keeps counting, and the controller is not woken."""
        window = PROXY_REQUESTS_KEY.format(proxy_id=manual.id)
        await redis_client.client.zadd(window, {"r1": time.time()})

        assert await proxy_service.unquarantine(project.id, manual.id) is False

        assert await redis_client.client.exists(window) == 1
        assert await runtime_entries(redis_client) == []


class TestChangeEntries:
    async def test_each_admin_write_appends_one_proxy_entry(
        self,
        proxy_service: ProxyService,
        project: Project,
        static_connector: Connector,
        redis_client: RedisClient,
    ) -> None:
        await forget_announcements(redis_client)

        proxy = await proxy_service.create(
            project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
        )
        upload = await proxy_service.upload(
            project.id, static_connector.id, "http://10.0.0.2:8080\nhttp://10.0.0.3:8080"
        )
        # Nothing it could read, nothing stored, nothing announced.
        await proxy_service.upload(project.id, static_connector.id, "not a proxy")
        await proxy_service.update(project.id, proxy.id, ProxyUpdate(tags=["x"]))
        await proxy_service.remove(project.id, upload.added[0].id)

        assert await announced(redis_client) == [
            ("proxy", proxy.id),
            ("proxy", ""),
            ("proxy", proxy.id),
            ("proxy", upload.added[0].id),
        ]

    async def test_a_cloud_proxy_is_announced_once_when_marked_for_termination(
        self, proxy_service: ProxyService, project: Project, cloud: Connector, redis_client: RedisClient
    ) -> None:
        instance = slot(cloud)
        await proxy_service.add(instance)
        await forget_announcements(redis_client)

        await proxy_service.remove(project.id, instance.id)
        # Terminating already: the second delete changes no row.
        await proxy_service.remove(project.id, instance.id)

        assert await announced(redis_client) == [("proxy", instance.id)]

    async def test_a_job_write_is_announced_only_when_it_changed_a_row(
        self, proxy_service: ProxyService, pool: Connector, redis_client: RedisClient
    ) -> None:
        """A health cycle that moves nothing, or a drain of a draining proxy, must not recompile the feed."""
        first, second, third = await proxy_service.bulk_add([slot(pool), slot(pool), slot(pool)])
        await forget_announcements(redis_client)

        moves = {first.id: StatusMove(ProxyStatus.UNHEALTHY, ProxyStatus.HEALTHY)}
        assert await proxy_service.set_statuses_if(moves) == {first.id}
        # The same check again: its expected status is stale, nothing moves.
        assert await proxy_service.set_statuses_if(moves) == set()
        assert await proxy_service.drain(second.id) is True
        assert await proxy_service.drain(second.id) is False
        assert await proxy_service.terminate(second.id) is True
        assert await proxy_service.terminate(second.id) is False
        await proxy_service.apply_sync([SyncEdit(third.id, {}, {}, ())], [])
        assert await proxy_service.delete(third.id) is True
        assert await proxy_service.delete(third.id) is False

        assert await announced(redis_client) == [
            ("proxy", first.id),
            ("proxy", second.id),
            ("proxy", second.id),
            ("proxy", third.id),
        ]

    async def test_a_batch_is_one_entry(
        self, proxy_service: ProxyService, pool: Connector, redis_client: RedisClient
    ) -> None:
        await forget_announcements(redis_client)

        added = await proxy_service.bulk_add([slot(pool), slot(pool)])
        moved = await proxy_service.set_statuses_if(
            {proxy.id: StatusMove(ProxyStatus.UNHEALTHY, ProxyStatus.HEALTHY) for proxy in added}
        )

        assert moved == {proxy.id for proxy in added}
        assert await announced(redis_client) == [("proxy", ""), ("proxy", "")]

    async def test_each_entry_follows_its_commit(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jobs: JobQueue,
        redis_client: RedisClient,
        registry: ProviderRegistry,
        project: Project,
        static_connector: Connector,
    ) -> None:
        """A consumer woken by an entry reads Postgres: the write must be there by then, or it is missed."""
        witness = Witness(redis_client, lambda ref: stored(sessions, ref))
        service = ProxyService(sessions, jobs, redis_client, registry, changes=witness)

        added = ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
        proxy = await service.create(project.id, added)
        edited = await service.update(project.id, proxy.id, ProxyUpdate(tags=["x"]))
        assert await service.drain(proxy.id) is True
        drained = await stored(sessions, proxy.id)
        await service.remove(project.id, proxy.id)

        assert witness.seen == [
            (ChangeKind.PROXY, proxy.id, proxy),
            (ChangeKind.PROXY, proxy.id, edited),
            (ChangeKind.PROXY, proxy.id, drained),
            (ChangeKind.PROXY, proxy.id, None),
        ]
