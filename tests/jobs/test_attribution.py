# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for attribution on the workers: the Postgres store, the sighting ingest, re-attribution and lookups.

The store is checked against the contract ``tests/geo/doubles.py`` spells out in memory, here with the real repository,
services, change log and Redis; the jobs run the real attributor over a database file that places London.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import api.core.consumers as consumers
import api.db.streams as streams
from api.core import utc_now
from api.core.consumers import decode_items
from api.core.roles import JobKind
from api.db.geo_repository import GeoSettingsRepository
from api.db.redis import GEO_PREFLIGHT_KEY, SIGHTING_STREAM_KEY, RedisClient
from api.db.repository import ProxyRepository
from api.db.streams import ChangeKind, ChangeLog, StreamEntry, ref_of
from api.gateway.sightings import SightingPublisher
from api.geo.attributor import ProxyAttributor
from api.geo.models import (
    META_LOCATION_CHECKED_AT,
    META_LOCATION_VERDICT,
    ContradictedAction,
    ExitJudgement,
    GeoSettings,
    GeoSourceKind,
    IpLocation,
    IpObservation,
    LocationCandidate,
    ObservationSource,
)
from api.geo.runtime import GeoStack
from api.jobs.attribution import (
    REATTRIBUTE_ALL,
    Attribution,
    EnrichJob,
    PostgresProxyStore,
    ReattributeJob,
    ReattributeScope,
    Sighting,
    SightingIngest,
)
from api.jobs.messages import SightingMessage
from api.jobs.queue import JobQueue
from api.jobs.runner import JobRunner
from api.models.connector import Connector
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_DISCOVERED_IP, META_GEO_COUNTRY, META_SESSION_ID
from api.services.proxies import ProxyService
from tests.conftest import queued_job_keys
from tests.geo.test_readers import write_mmdb
from tests.jobs.conftest import (
    ECHO_URL,
    LONDON_IP,
    UNKNOWN_IP,
    FakeUpstream,
    Rows,
    announced,
    batch_of,
    counted_selects,
    geo_settings,
    recorded,
    waiting,
)
from tests.waits import running, until

T0 = datetime(2026, 10, 18, 12, 0, 0)
# Exits a database of :class:`TestBatchesThatReadTheSameRow` places: two
# networks in Dallas (US) and one in Berlin (DE).
DALLAS_IP = "203.0.113.10"
OTHER_DALLAS_IP = "192.0.2.30"
BERLIN_IP = "198.51.100.20"


async def _patch(sessions: async_sessionmaker[AsyncSession], proxy_id: str, **changed: object) -> None:
    """Another writer's metadata patch, committed."""
    async with sessions() as session:
        await ProxyRepository(session).patch_metadata(proxy_id, changed, (), None)
        await session.commit()


def _sighting(proxy: Proxy, ip: str, observed_at: datetime, **fields: object) -> str:
    """A gateway's sighting, as its stream entry's ``item`` holds it."""
    values: dict[str, object] = {
        "proxy_id": proxy.id,
        "ip": ip,
        "source": ObservationSource.PREFLIGHT,
        "candidates": [],
        "observed_at": observed_at,
        "instance_id": "gateway-1",
    }
    values.update(fields)
    return SightingMessage.model_validate(values).model_dump_json()


async def _append(redis_client: RedisClient, *items: str) -> None:
    """Sightings appended to the stream in one transaction, as a gateway's publisher appends a window."""
    pipe = redis_client.client.pipeline(transaction=True)
    for item in items:
        pipe.xadd(SIGHTING_STREAM_KEY, {"item": item})
    await pipe.execute()


@pytest.fixture
def store(attribution: Attribution) -> PostgresProxyStore:
    return attribution.store()


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


@pytest.fixture
async def static(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project)


@pytest.fixture
async def pool(rows: Rows, project: Project) -> Connector:
    """A vendor session pool: a connector whose provider syncs."""
    return await rows.connector(project, "oxylabs", credential={"username": "alice", "password": "s3cret"})


class TestReattributeKeys:
    def test_keys_name_their_scope(self) -> None:
        assert ReattributeScope().key == REATTRIBUTE_ALL == "all"
        assert ReattributeScope(project_id="p1").key == "project:p1"
        assert ReattributeScope(connector_id="c1").key == "connector:c1"
        assert ReattributeScope.parse("all") == ReattributeScope()
        assert ReattributeScope.parse("project:p1") == ReattributeScope(project_id="p1")
        assert ReattributeScope.parse("connector:c1") == ReattributeScope(connector_id="c1")
        assert ReattributeScope.parse("connector:") is None and ReattributeScope.parse("bogus") is None


class TestStore:
    async def test_a_load_reads_the_rows_their_connectors_and_projects_in_one_query_each(
        self, store: PostgresProxyStore, rows: Rows, project: Project, pool: Connector, db_engine: AsyncEngine
    ) -> None:
        other_project = await rows.project()
        other = await rows.connector(other_project)
        slot = await rows.proxy(pool, username="customer-{username}", password="{password}")
        plain = await rows.proxy(other)

        with counted_selects(db_engine) as selects:
            await store.load([slot.id, plain.id, "gone"])

        assert len(selects) == 3
        loaded = store.get_proxy(slot.id)
        assert loaded is not None and (loaded.username, loaded.password) == ("customer-alice", "s3cret")
        assert store.get_proxy("gone") is None
        assert store.get_connector(other.id) == other and store.get_project(other_project.id) is not None
        assert store.get_project(project.id) is not None

    async def test_a_save_writes_only_the_keys_it_changed_and_never_over_a_newer_sighting(
        self, attribution: Attribution, rows: Rows, static: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        proxy = await rows.proxy(static, metadata={"stale": True, "country": "US"})
        store = attribution.store()
        await store.load([proxy.id])
        loaded = store.get_proxy(proxy.id)
        assert loaded is not None
        loaded.metadata["country"] = "GB"
        loaded.metadata[META_LOCATION_CHECKED_AT] = T0.isoformat()
        del loaded.metadata["stale"]
        # Written by someone else after the batch was loaded: kept.
        await _patch(sessions, proxy.id, other="kept")

        store.save(loaded, checked_at=T0)
        await store.flush()

        stored = await rows.get(proxy.id)
        assert stored is not None
        assert stored.metadata == {"country": "GB", "other": "kept", META_LOCATION_CHECKED_AT: T0.isoformat()}

        # A batch judged from an older sighting loses: the row stays as it is.
        older = attribution.store()
        await older.load([proxy.id])
        stale = older.get_proxy(proxy.id)
        assert stale is not None
        stale.metadata["country"] = "FR"
        older.save(stale, checked_at=T0 - timedelta(seconds=30))
        await older.flush()
        assert (await rows.get(proxy.id)).metadata["country"] == "GB"  # type: ignore[union-attr]

    async def test_rotated_slots_are_gone_from_the_batch_and_deleted_after_the_write_asking_for_one_sync(
        self, store: PostgresProxyStore, rows: Rows, pool: Connector, redis_client: RedisClient
    ) -> None:
        slots = [await rows.proxy(pool, metadata={META_SESSION_ID: f"s{n}"}) for n in range(3)]
        await store.load(slot.id for slot in slots)
        for slot in slots:
            loaded = store.get_proxy(slot.id)
            assert loaded is not None
            loaded.metadata[META_LOCATION_VERDICT] = "contradicted"
            store.save(loaded, checked_at=utc_now(), rotate=True)
            assert store.get_proxy(slot.id) is None

        await store.flush()

        assert [await rows.get(slot.id) for slot in slots] == [None] * 3
        # The batch's rotations are one deletion: one sync of their connector, not one a slot.
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]

    async def test_a_rotation_that_fails_leaves_the_write_and_the_slot_for_the_next_sighting(
        self,
        store: PostgresProxyStore,
        rows: Rows,
        pool: Connector,
        proxy_service: ProxyService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slot = await rows.proxy(pool, metadata={META_SESSION_ID: "abc"})

        async def refused(proxy_ids: Sequence[str]) -> list[str]:
            raise OSError("Postgres is down")

        monkeypatch.setattr(proxy_service, "delete_many", refused)
        await store.load([slot.id])
        loaded = store.get_proxy(slot.id)
        assert loaded is not None
        loaded.metadata[META_LOCATION_VERDICT] = "contradicted"
        store.save(loaded, checked_at=utc_now(), rotate=True)

        with structlog.testing.capture_logs() as logs:
            await store.flush()

        stored = await rows.get(slot.id)
        assert stored is not None and stored.metadata[META_LOCATION_VERDICT] == "contradicted"
        warned = [(log["log_level"], log["count"]) for log in logs if log["event"] == "Could not rotate refused session slots"]
        assert warned == [("warning", 1)]

    async def test_a_write_refused_as_older_neither_rotates_nor_drops_the_verdict(
        self, store: PostgresProxyStore, rows: Rows, project: Project, pool: Connector, redis_client: RedisClient
    ) -> None:
        slot = await rows.proxy(pool, metadata={META_SESSION_ID: "abc", META_LOCATION_CHECKED_AT: T0.isoformat()})
        verdict = GEO_PREFLIGHT_KEY.format(project_id=project.id, proxy_id=slot.id)
        await redis_client.client.set(verdict, "cached")
        await store.load([slot.id])
        loaded = store.get_proxy(slot.id)
        assert loaded is not None

        store.save(loaded, checked_at=T0 - timedelta(minutes=1), rotate=True, drop_verdict=True)
        await store.flush()

        assert await rows.get(slot.id) is not None
        assert await redis_client.client.get(verdict) == "cached"
        assert await waiting(redis_client, "sync") == set()

    async def test_an_exit_that_moved_drops_the_cached_preflight_verdict(
        self, store: PostgresProxyStore, rows: Rows, project: Project, static: Connector, redis_client: RedisClient
    ) -> None:
        proxy = await rows.proxy(static, metadata={META_DISCOVERED_IP: UNKNOWN_IP})
        verdict = GEO_PREFLIGHT_KEY.format(project_id=project.id, proxy_id=proxy.id)
        await redis_client.client.set(verdict, "cached")
        await store.load([proxy.id])
        loaded = store.get_proxy(proxy.id)
        assert loaded is not None
        loaded.metadata[META_DISCOVERED_IP] = LONDON_IP

        store.save(loaded, checked_at=utc_now(), drop_verdict=True)
        await store.flush()

        assert await redis_client.client.get(verdict) is None

    async def test_a_verdict_drop_cut_by_a_deadline_is_logged_by_its_type(
        self,
        store: PostgresProxyStore,
        rows: Rows,
        static: Connector,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type."""
        proxy = await rows.proxy(static, metadata={META_DISCOVERED_IP: UNKNOWN_IP})
        await store.load([proxy.id])
        loaded = store.get_proxy(proxy.id)
        assert loaded is not None
        loaded.metadata[META_DISCOVERED_IP] = LONDON_IP
        store.save(loaded, checked_at=utc_now(), drop_verdict=True)
        monkeypatch.setattr(redis_client.client, "delete", AsyncMock(side_effect=TimeoutError))

        with structlog.testing.capture_logs() as logs:
            await store.flush()

        assert [log["error"] for log in logs if log["event"] == "Could not drop preflight verdicts"] == ["TimeoutError"]

    async def test_a_rotation_cut_by_a_deadline_is_logged_by_its_type(
        self,
        store: PostgresProxyStore,
        rows: Rows,
        pool: Connector,
        proxy_service: ProxyService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type."""
        slot = await rows.proxy(pool, metadata={META_SESSION_ID: "abc"})
        monkeypatch.setattr(proxy_service, "delete_many", AsyncMock(side_effect=TimeoutError))
        await store.load([slot.id])
        loaded = store.get_proxy(slot.id)
        assert loaded is not None
        loaded.metadata[META_LOCATION_VERDICT] = "contradicted"
        store.save(loaded, checked_at=utc_now(), rotate=True)

        with structlog.testing.capture_logs() as logs:
            await store.flush()

        failed = [log["error"] for log in logs if log["event"] == "Could not rotate refused session slots"]
        assert failed == ["TimeoutError"]

    async def test_pages_walk_one_scope_in_id_order(
        self, store: PostgresProxyStore, rows: Rows, project: Project, static: Connector, pool: Connector
    ) -> None:
        elsewhere = await rows.connector(await rows.project())
        mine = sorted([(await rows.proxy(static)).id, (await rows.proxy(static)).id, (await rows.proxy(pool)).id])
        theirs = (await rows.proxy(elsewhere)).id

        first = await store.load_page(None, 2, connector_id=None, project_id=project.id)
        rest = await store.load_page(first[-1].id, 2, connector_id=None, project_id=project.id)
        assert [p.id for p in first + rest] == mine

        static_ids = [p.id for p in await store.load_page(None, 10, connector_id=static.id, project_id=None)]
        assert static_ids == sorted(p.id for p in await rows.of(static))
        everything = await store.load_page(None, 10, connector_id=None, project_id=None)
        assert [p.id for p in everything] == sorted([*mine, theirs])
        # A page replaces the batch.
        assert store.get_proxy(theirs) is not None and store.get_proxy(mine[0]) is not None


class Reading(ChangeLog):
    """A change log that reads the row each entry names the moment it is recorded: what the entry's reader would find."""

    def __init__(self, redis_client: RedisClient, rows: Rows) -> None:
        super().__init__(redis_client)
        self._rows = rows
        # Each entry, with the metadata its row had in Postgres when it was announced.
        self.entries: list[tuple[ChangeKind, str, dict[str, Any] | None]] = []

    async def record(self, kind: ChangeKind, *ids: str) -> None:
        ref = ref_of(ids)
        stored = await self._rows.get(ref) if ref else None
        self.entries.append((kind, ref, stored.metadata if stored is not None else None))


class TestAnnouncements:
    """A batch's write is announced on the change stream once it has committed: the controller compiles the rows' new places."""

    async def test_a_write_is_announced_once_after_its_commit_naming_its_row(
        self,
        sessions: async_sessionmaker[AsyncSession],
        proxy_service: ProxyService,
        redis_client: RedisClient,
        rows: Rows,
        static: Connector,
    ) -> None:
        proxy = await rows.proxy(static)
        log = Reading(redis_client, rows)
        store = PostgresProxyStore(sessions, proxy_service, redis_client, changes=log)
        await store.load([proxy.id])
        loaded = store.get_proxy(proxy.id)
        assert loaded is not None
        loaded.metadata["country"] = "GB"

        store.save(loaded, checked_at=T0)
        await store.flush()

        # The entry's reader finds the write already there.
        assert log.entries == [(ChangeKind.PROXY, proxy.id, {"country": "GB"})]

    async def test_rows_written_together_are_one_entry(
        self, store: PostgresProxyStore, rows: Rows, static: Connector, redis_client: RedisClient
    ) -> None:
        proxies = [await rows.proxy(static), await rows.proxy(static)]
        await store.load(proxy.id for proxy in proxies)
        for proxy in proxies:
            loaded = store.get_proxy(proxy.id)
            assert loaded is not None
            loaded.metadata["country"] = "GB"
            store.save(loaded, checked_at=T0)

        await store.flush()

        assert await announced(redis_client) == [("proxy", "")]

    async def test_a_batch_that_wrote_no_row_announces_nothing(
        self, attribution: Attribution, rows: Rows, static: Connector, redis_client: RedisClient
    ) -> None:
        stamped = await rows.proxy(static, metadata={META_LOCATION_CHECKED_AT: T0.isoformat()})
        unsaved = attribution.store()
        await unsaved.load([stamped.id])
        await unsaved.flush()
        # Judged from a sighting older than the row as read: refused before it is sent.
        older = attribution.store()
        await older.load([stamped.id])
        loaded = older.get_proxy(stamped.id)
        assert loaded is not None
        loaded.metadata["country"] = "FR"
        older.save(loaded, checked_at=T0 - timedelta(minutes=1))
        await older.flush()

        assert await announced(redis_client) == []

    async def test_a_change_log_redis_does_not_answer_never_fails_the_write(
        self,
        sessions: async_sessionmaker[AsyncSession],
        proxy_service: ProxyService,
        redis_client: RedisClient,
        rows: Rows,
        static: Connector,
    ) -> None:
        """The write is committed: its entry is sent again later, never at the write's expense."""
        proxy = await rows.proxy(static)
        away = RedisClient("redis://127.0.0.1:1/0", request_timeout=0.2)
        await away.connect()
        log = ChangeLog(away)
        try:
            store = PostgresProxyStore(sessions, proxy_service, redis_client, changes=log)
            await store.load([proxy.id])
            loaded = store.get_proxy(proxy.id)
            assert loaded is not None
            loaded.metadata["country"] = "GB"
            store.save(loaded, checked_at=T0)

            await store.flush()

            stored = await rows.get(proxy.id)
            assert stored is not None and stored.metadata["country"] == "GB"
            assert log.pending == {ChangeKind.PROXY}
        finally:
            await log.close()
            await away.close()


def _place(country: str, subdivision: str, city: str, latitude: float, longitude: float) -> dict[str, Any]:
    """A city database record."""
    return {
        "country": {"iso_code": country, "names": {"en": country}},
        "subdivisions": [{"iso_code": subdivision, "names": {"en": subdivision}}],
        "city": {"names": {"en": city}},
        "location": {"latitude": latitude, "longitude": longitude},
    }


class TestBatchesThatReadTheSameRow:
    """Batches on several workers that read one row: a patch is only ever written over the row it was computed from."""

    @pytest.fixture
    async def places(
        self, tmp_path: Path, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> AsyncIterator[GeoStack]:
        dallas, berlin = _place("US", "TX", "Dallas", 32.78, -96.8), _place("DE", "BE", "Berlin", 52.52, 13.4)
        database = write_mmdb(
            tmp_path / "places.mmdb",
            "GeoLite2-City",
            {"203.0.113.0/24": dallas, "192.0.2.0/24": dallas, "198.51.100.0/24": berlin},
        )
        stack = GeoStack.build(
            geo_settings(tmp_path, geo_databases=[{"path": str(database)}]),
            session_factory=sessions,
            redis_client=redis_client,
            blob_source=None,
        )
        await stack.geo_service.resync()
        yield stack
        stack.database_store.close_all()

    @pytest.fixture
    def judging(
        self,
        places: GeoStack,
        sessions: async_sessionmaker[AsyncSession],
        proxy_service: ProxyService,
        redis_client: RedisClient,
        change_log: ChangeLog,
    ) -> Attribution:
        return Attribution(
            ProxyAttributor(places.geo_service), sessions, proxy_service, redis_client, changes=change_log
        )

    async def test_the_later_write_is_refused_and_the_next_sighting_judges_the_row_again(
        self, judging: Attribution, rows: Rows, pool: Connector
    ) -> None:
        slot = await rows.proxy(pool, metadata={META_SESSION_ID: "abc", META_GEO_COUNTRY: "US"})
        await judging.observe([Sighting(slot.id, DALLAS_IP, ObservationSource.HEALTH_CHECK, observed_at=T0)])
        attributor = judging.attributor
        # A sighting batch holding an older preflight sighting of a Berlin exit,
        # and a health check's batch with a newer exit placed like the row's
        # own: both read the row before either writes, and the older commits first.
        ingest, health = judging.store(), judging.store()
        await ingest.load([slot.id])
        await health.load([slot.id])
        attributor.observe(
            ingest, slot.id, BERLIN_IP, source=ObservationSource.PREFLIGHT, observed_at=T0 + timedelta(seconds=10)
        )
        attributor.observe(
            health,
            slot.id,
            OTHER_DALLAS_IP,
            source=ObservationSource.HEALTH_CHECK,
            observed_at=T0 + timedelta(seconds=20),
        )
        await ingest.flush()
        await health.flush()

        # The ingest's whole judgement, not the newer exit under Berlin's place.
        stored = await rows.get(slot.id)
        assert stored is not None
        assert (stored.metadata[META_DISCOVERED_IP], stored.country) == (BERLIN_IP, "DE")
        assert stored.metadata[META_LOCATION_CHECKED_AT] == (T0 + timedelta(seconds=10)).isoformat()

        # The refused exit is not on the row, so its next check is no repeat and is judged.
        later = T0 + timedelta(seconds=80)
        await judging.observe([Sighting(slot.id, OTHER_DALLAS_IP, ObservationSource.HEALTH_CHECK, observed_at=later)])
        stored = await rows.get(slot.id)
        assert stored is not None
        assert (stored.metadata[META_DISCOVERED_IP], stored.country) == (OTHER_DALLAS_IP, "US")
        assert stored.metadata[META_LOCATION_CHECKED_AT] == later.isoformat()

    async def test_a_new_row_keeps_its_first_judgement_whole(
        self, judging: Attribution, rows: Rows, static: Connector
    ) -> None:
        """A new static proxy's first health check and its lookup race: the row is never stamped twice from one read."""
        proxy = await rows.proxy(static)
        attributor = judging.attributor
        check, lookup = judging.store(), judging.store()
        await check.load([proxy.id])
        await lookup.load([proxy.id])
        attributor.observe(check, proxy.id, BERLIN_IP, source=ObservationSource.HEALTH_CHECK, observed_at=T0)
        attributor.observe(
            lookup, proxy.id, DALLAS_IP, source=ObservationSource.GEO_LOOKUP, observed_at=T0 + timedelta(seconds=5)
        )
        await check.flush()
        await lookup.flush()

        stored = await rows.get(proxy.id)
        assert stored is not None
        assert (stored.metadata[META_DISCOVERED_IP], stored.country) == (BERLIN_IP, "DE")
        assert stored.metadata[META_LOCATION_CHECKED_AT] == T0.isoformat()

    async def test_another_writers_key_does_not_refuse_the_patch(
        self, judging: Attribution, rows: Rows, static: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A key a sync or the api writes leaves the stamp where it was: only an attribution moves it."""
        stamped = {META_DISCOVERED_IP: DALLAS_IP, META_LOCATION_CHECKED_AT: T0.isoformat()}
        proxy = await rows.proxy(static, metadata=stamped)
        store = judging.store()
        await store.load([proxy.id])
        await _patch(sessions, proxy.id, operator_note="kept")
        judging.attributor.observe(
            store, proxy.id, BERLIN_IP, source=ObservationSource.HEALTH_CHECK, observed_at=T0 + timedelta(seconds=5)
        )
        await store.flush()

        stored = await rows.get(proxy.id)
        assert stored is not None
        assert (stored.metadata[META_DISCOVERED_IP], stored.country, stored.metadata["operator_note"]) == (
            BERLIN_IP,
            "DE",
            "kept",
        )


class TestSightingMessage:
    """What a gateway's preflight tells the workers of a pooled slot's exit, on the sightings stream."""

    async def test_crosses_the_stream_unchanged(self, redis_client: RedisClient) -> None:
        """Appended by a gateway's publisher, and read back as the sighting ingest reads its entries."""
        message = SightingMessage(
            proxy_id="proxy-1",
            ip="203.0.113.7",
            source=ObservationSource.PREFLIGHT,
            endpoint_country="DE",
            endpoint_city="berlin",
            session_id="session-1",
            candidates=[
                LocationCandidate(
                    source=GeoSourceKind.DATABASE,
                    origin="db-1",
                    country="de",
                    location=IpLocation(country="DE", state_code="BE", city="Berlin", asn=3320),
                )
            ],
            observed_at=utc_now(),
            instance_id="gateway-1",
        )
        publisher = SightingPublisher(redis_client, interval=60, max_pending=10)
        publisher.push(message)
        assert await publisher.flush() == 1

        entries = [StreamEntry(entry_id, fields) for entry_id, fields in await redis_client.client.xrange(SIGHTING_STREAM_KEY)]
        (received,), malformed = decode_items(entries, SightingMessage.model_validate_json)
        assert malformed == []
        assert received == message
        # Naive UTC both sides: the worker orders and stamps by it as sent.
        assert received.observed_at.tzinfo is None
        assert received.candidates[0].city == "berlin"

    def test_the_gateway_must_say_what_the_worker_cannot_learn(self) -> None:
        """No databases' answer, no time and no gateway would make the worker guess."""
        with pytest.raises(ValidationError) as caught:
            SightingMessage(  # type: ignore[call-arg]
                proxy_id="proxy-1", ip="203.0.113.7", source=ObservationSource.PREFLIGHT
            )
        missing = {error["loc"][0] for error in caught.value.errors()}
        assert missing == {"candidates", "observed_at", "instance_id"}


class Refusing(Attribution):
    """An attribution whose writes fail, as with Postgres away; it notes the proxies of each batch it was handed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.batches: list[list[str]] = []

    async def observe(self, sightings: Sequence[Sighting]) -> None:
        self.batches.append([sighting.proxy_id for sighting in sightings])
        raise OSError("Postgres is down")


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Claims taken after 0.4 s, refreshes and reclaim checks every 0.1 s, and read back-offs of 0.05 s to 0.1 s."""
    monkeypatch.setattr(consumers, "CLAIM_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 0.1)
    monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.1)
    yield


class TestSightingIngest:
    @pytest.fixture
    def ingest(self, redis_client: RedisClient, attribution: Attribution) -> SightingIngest:
        return SightingIngest(redis_client, attribution)

    @pytest.fixture
    def refusing_attribution(
        self,
        geo: GeoStack,
        sessions: async_sessionmaker[AsyncSession],
        proxy_service: ProxyService,
        redis_client: RedisClient,
        change_log: ChangeLog,
    ) -> Refusing:
        return Refusing(ProxyAttributor(geo.geo_service), sessions, proxy_service, redis_client, changes=change_log)

    async def test_a_batch_is_applied_in_the_order_its_sightings_were_seen(
        self, ingest: SightingIngest, rows: Rows, static: Connector, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        proxy = await rows.proxy(static)
        # The newer sighting was appended first.
        await _append(redis_client, _sighting(proxy, LONDON_IP, T0 + timedelta(seconds=5)), _sighting(proxy, UNKNOWN_IP, T0))

        async with running(ingest):
            await until(lambda: ingest.consumed == 2)

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == LONDON_IP
        observations = await recorded(geo, redis_client)
        assert [(o.ip, o.observed_at, o.instance_id) for o in observations if isinstance(o, IpObservation)] == [
            (UNKNOWN_IP, T0, "gateway-1"),
            (LONDON_IP, T0 + timedelta(seconds=5), "gateway-1"),
        ]
        # Completed: the stream holds only work in hand.
        assert await redis_client.client.xlen(SIGHTING_STREAM_KEY) == 0

    async def test_an_older_sighting_ingested_later_is_recorded_and_leaves_the_row_on_the_newer_exit(
        self, ingest: SightingIngest, rows: Rows, static: Connector, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        proxy = await rows.proxy(static)

        async with running(ingest):
            await _append(redis_client, _sighting(proxy, LONDON_IP, T0 + timedelta(seconds=5)))
            await until(lambda: ingest.consumed == 1)
            await _append(redis_client, _sighting(proxy, UNKNOWN_IP, T0))
            await until(lambda: ingest.consumed == 2)

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == LONDON_IP
        observations = [o for o in await recorded(geo, redis_client) if isinstance(o, IpObservation)]
        assert [o.ip for o in observations] == [LONDON_IP, UNKNOWN_IP]

    async def test_every_sighting_is_one_observation_and_a_repeat_writes_nothing(
        self, ingest: SightingIngest, rows: Rows, pool: Connector, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        """Two sessions on one exit: two observations and one row write (the gateway-worker seam)."""
        slot = await rows.proxy(pool, metadata={META_SESSION_ID: "abc", META_GEO_COUNTRY: "GB"})
        await _append(
            redis_client,
            _sighting(slot, LONDON_IP, T0, session_id="s1", endpoint_country="GB"),
            _sighting(slot, LONDON_IP, T0 + timedelta(seconds=1), session_id="s2"),
        )

        async with running(ingest):
            await until(lambda: ingest.consumed == 2)

        observations = [o for o in await recorded(geo, redis_client) if isinstance(o, IpObservation)]
        assert [(o.session_id, o.source, o.endpoint_country) for o in observations] == [
            ("s1", "preflight", "GB"),
            ("s2", "preflight", None),
        ]
        # One write, announced once.
        assert await announced(redis_client) == [("proxy", slot.id)]

    async def test_contradicted_slots_of_a_refusing_project_are_rotated_with_one_sync_of_their_connector(
        self, ingest: SightingIngest, rows: Rows, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        refusing = await rows.project(location_contradicted=ContradictedAction.REFUSE)
        pool = await rows.connector(refusing, "oxylabs", credential={"username": "alice", "password": "s3cret"})
        slots = [
            await rows.proxy(pool, metadata={META_SESSION_ID: f"s{n}", META_GEO_COUNTRY: "US"}) for n in range(3)
        ]
        candidates = [c.model_dump(mode="json") for c in geo.geo_service.attribute(LONDON_IP)]
        await _append(redis_client, *(_sighting(slot, LONDON_IP, T0, candidates=candidates) for slot in slots))

        async with running(ingest):
            await until(lambda: ingest.consumed == 3)

        assert [await rows.get(slot.id) for slot in slots] == [None] * 3
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]

    async def test_a_sighting_is_judged_with_the_answers_the_gateway_had_and_not_looked_up_again(
        self, ingest: SightingIngest, rows: Rows, static: Connector, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        """The request was routed on the gateway's answers; the worker's own databases may know less, or differ."""
        proxy = await rows.proxy(static)
        london = [c.model_dump(mode="json") for c in geo.geo_service.attribute(LONDON_IP)]
        # An exit this worker's database does not know, which the gateway's databases placed in London.
        await _append(redis_client, _sighting(proxy, UNKNOWN_IP, T0, candidates=london))

        async with running(ingest):
            await until(lambda: ingest.consumed == 1)

        stored = await rows.get(proxy.id)
        assert stored is not None and (stored.metadata[META_DISCOVERED_IP], stored.country) == (UNKNOWN_IP, "GB")

    async def test_unreadable_entries_are_logged_and_completed_with_the_rest_applied(
        self, ingest: SightingIngest, rows: Rows, static: Connector, redis_client: RedisClient
    ) -> None:
        proxy = await rows.proxy(static)
        await _append(redis_client, "not json", '{"proxy_id": "x"}', _sighting(proxy, LONDON_IP, T0))

        with structlog.testing.capture_logs() as logs:
            async with running(ingest):
                await until(lambda: ingest.consumed == 3)

        skipped = [(log["log_level"], log["count"]) for log in logs if log["event"] == "Unreadable sightings skipped"]
        assert skipped == [("warning", 2)]
        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == LONDON_IP
        assert await redis_client.client.xlen(SIGHTING_STREAM_KEY) == 0

    async def test_a_sighting_of_a_proxy_that_is_gone_is_ignored(
        self, ingest: SightingIngest, rows: Rows, static: Connector, geo: GeoStack, redis_client: RedisClient
    ) -> None:
        proxy = await rows.proxy(static)
        await _append(redis_client, _sighting(proxy, LONDON_IP, T0).replace(proxy.id, "gone"))

        async with running(ingest):
            await until(lambda: ingest.consumed == 1)

        assert await recorded(geo, redis_client) == []

    @pytest.mark.usefixtures("quick")
    async def test_a_batch_that_fails_is_delivered_again_then_given_up_after_five_deliveries(
        self, rows: Rows, static: Connector, redis_client: RedisClient, refusing_attribution: Refusing
    ) -> None:
        proxy = await rows.proxy(static)
        ingest = SightingIngest(redis_client, refusing_attribution)
        await _append(redis_client, _sighting(proxy, LONDON_IP, T0))

        async with running(ingest):
            await until(lambda: ingest.dropped == 1, timeout=10)

        assert refusing_attribution.batches == [[proxy.id]] * 5
        assert ingest.consumed == 0
        assert await redis_client.client.xlen(SIGHTING_STREAM_KEY) == 0


class TestReattributeJob:
    @pytest.fixture
    def job(self, attribution: Attribution, geo: GeoStack) -> ReattributeJob:
        return ReattributeJob(attribution, geo.geo_service)

    @pytest.fixture
    def passes(self, attribution: Attribution, monkeypatch: pytest.MonkeyPatch) -> list[ReattributeScope]:
        """The scope of each pass the attributor runs, in order."""
        made: list[ReattributeScope] = []
        reattribute = attribution.attributor.reattribute

        async def counted(pages: Any, *, connector_id: str | None = None, project_id: str | None = None) -> Any:
            made.append(ReattributeScope(connector_id=connector_id, project_id=project_id))
            return await reattribute(pages, connector_id=connector_id, project_id=project_id)

        monkeypatch.setattr(attribution.attributor, "reattribute", counted)
        return made

    def test_a_worker_reads_a_hundred_asks_at_a_time_and_runs_one_batch_at_once(self) -> None:
        assert (ReattributeJob.kind, ReattributeJob.batch_size, ReattributeJob.concurrency) == (
            JobKind.REATTRIBUTE, 100, 1
        )

    async def test_each_key_re_judges_its_scope(
        self,
        job: ReattributeJob,
        rows: Rows,
        project: Project,
        static: Connector,
        pool: Connector,
        geo: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        elsewhere = await rows.connector(await rows.project())
        a = await rows.proxy(static, metadata={META_DISCOVERED_IP: LONDON_IP})
        b = await rows.proxy(pool, metadata={META_DISCOVERED_IP: LONDON_IP})
        c = await rows.proxy(elsewhere, metadata={META_DISCOVERED_IP: LONDON_IP})
        await rows.proxy(static)  # no exit known: nothing to re-judge

        async def judged(key: str) -> set[str | None]:
            await job.run(batch_of(key))
            return {o.proxy_id for o in await recorded(geo, redis_client) if isinstance(o, ExitJudgement)}

        assert await judged(ReattributeScope(connector_id=static.id).key) == {a.id}
        assert await judged(ReattributeScope(project_id=project.id).key) == {a.id, b.id}
        assert await judged(REATTRIBUTE_ALL) == {a.id, b.id, c.id}
        stored = await rows.get(a.id)
        assert stored is not None and stored.country == "GB"

    async def test_a_batch_runs_one_pass_per_scope_and_every_proxy_covers_the_rest(
        self, job: ReattributeJob, passes: list[ReattributeScope]
    ) -> None:
        await job.run(batch_of("connector:c1", "project:p1"))
        assert passes == [ReattributeScope(connector_id="c1"), ReattributeScope(project_id="p1")]

        passes.clear()
        await job.run(batch_of("connector:c1", REATTRIBUTE_ALL, "project:p1"))
        assert passes == [ReattributeScope()]

    async def test_ten_asks_for_every_proxy_read_together_run_one_pass(
        self, job: ReattributeJob, passes: list[ReattributeScope], redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        """A burst of asks (a database file and two settings saves, say) costs one pass, not one per ask."""
        await jobs.publish(JobKind.REATTRIBUTE, [REATTRIBUTE_ALL] * 10)
        runner = JobRunner(redis_client, job)

        async with running(runner):
            await until(lambda: runner.consumed == 10)

        assert passes == [ReattributeScope()]

    async def test_a_pass_first_reads_what_was_committed_before_it(
        self, job: ReattributeJob, geo: GeoStack, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            await GeoSettingsRepository(session).save(GeoSettings(preflight_max_attempts=5))
            await session.commit()
        assert geo.geo_service.settings.preflight_max_attempts != 5

        await job.run(batch_of(REATTRIBUTE_ALL))

        assert geo.geo_service.settings.preflight_max_attempts == 5

    async def test_an_unknown_key_is_logged_and_runs_no_pass(
        self, job: ReattributeJob, passes: list[ReattributeScope]
    ) -> None:
        with structlog.testing.capture_logs() as logs:
            await job.run(batch_of("bogus"))

        assert passes == []
        dropped = [(log["log_level"], log["key"]) for log in logs if log["event"] == "Unknown re-attribution key dropped"]
        assert dropped == [("warning", "bogus")]


class TestEnrichJob:
    def test_a_worker_looks_up_five_proxies_at_once_one_a_run(self) -> None:
        assert (EnrichJob.kind, EnrichJob.batch_size, EnrichJob.concurrency) == (JobKind.ENRICH, 1, 5)

    async def test_a_new_static_proxy_is_looked_up_through_itself(
        self,
        attribution: Attribution,
        rows: Rows,
        static: Connector,
        upstream: FakeUpstream,
        geo: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        upstream.answer({"ip": LONDON_IP, "country": "GB"})
        proxy = await rows.proxy(static, port=upstream.port, username="u", password="p")

        await EnrichJob(attribution).run(batch_of(proxy.id))

        assert upstream.requests == [(f"GET {ECHO_URL} HTTP/1.1", "Basic " + base64.b64encode(b"u:p").decode())]
        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == LONDON_IP and stored.country == "GB"
        observations = [o for o in await recorded(geo, redis_client) if isinstance(o, IpObservation)]
        assert [o.source for o in observations] == [ObservationSource.GEO_LOOKUP]

    async def test_no_lookup_for_a_country_set_by_hand_or_while_lookups_are_off(
        self,
        attribution: Attribution,
        geo: GeoStack,
        rows: Rows,
        static: Connector,
        upstream: FakeUpstream,
        sessions: async_sessionmaker[AsyncSession],
        proxy_service: ProxyService,
        redis_client: RedisClient,
        change_log: ChangeLog,
    ) -> None:
        pinned = await rows.proxy(static, port=upstream.port, metadata={"country": "DE"})
        unpinned = await rows.proxy(static, port=upstream.port)
        lookups_off = Attribution(
            ProxyAttributor(geo.geo_service, lookup_enabled=False),
            sessions,
            proxy_service,
            redis_client,
            changes=change_log,
        )

        await EnrichJob(attribution).run(batch_of(pinned.id, "gone"))
        await EnrichJob(lookups_off).run(batch_of(unpinned.id))

        assert upstream.requests == []

    async def test_a_failed_lookup_writes_nothing(
        self, attribution: Attribution, rows: Rows, static: Connector, upstream: FakeUpstream
    ) -> None:
        upstream.answer({}, status=503)
        proxy = await rows.proxy(static, port=upstream.port)

        await EnrichJob(attribution).run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata == {}
