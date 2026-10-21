# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the definitions refresher: an api or worker process follows the changes stream and reads Postgres again.

The stores are the real ones over the test Postgres and the stream is the test Redis's, written as an admin's writes
and the services' change entries would be; a :class:`Harness` runs the refresher's loop as a task and counts its reads.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.geo_repository import GeoDatabaseRepository, GeoSettingsRepository
from api.db.redis import CHANGES_STREAM_KEY, RedisClient
from api.db.repository import ProviderDescriptorRepository
from api.db.streams import ChangeKind, ChangeLog
from api.geo.models import GeoDatabaseRecord, GeoSettings
from api.geo.runtime import GeoStack
from api.geo.store import PostgresBlobSource
from api.models.provider import ProviderRecord
from api.providers.registry import ProviderRegistry
from api.providers.store import ProviderStore
from api.roles.refresh import READ_ON, DefinitionsRefresher
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.jobs.conftest import geo_settings
from tests.waits import until

# A custom descriptor as an admin stores it: a session pool on one gateway.
ACME: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "credential_fields": [{"key": "username", "label": "User", "required": True}],
    "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number", "default": 1, "min": 1}],
    "proxy_types": [
        {
            "key": "res",
            "label": "Res",
            "mode": "session",
            "host": "gw.acme.test",
            "port": 9000,
            "username": "{credential.username}-{session_id}",
        }
    ],
}
# Another, saved while a test runs.
BETA: dict[str, Any] = {**ACME, "id": "beta", "name": "Beta"}
# The kinds that name what an api or worker keeps in memory, and those the
# controller compiles, which it keeps nothing of.
KEPT = [ChangeKind.DESCRIPTOR, ChangeKind.GEO_SETTINGS, ChangeKind.GEO_DATABASE, ChangeKind.ALL]
SKIPPED = [
    ChangeKind.PROJECT,
    ChangeKind.CREDENTIAL,
    ChangeKind.CONNECTOR,
    ChangeKind.PROXY,
    ChangeKind.WIREGUARD,
    ChangeKind.OPENVPN,
]


class Flaky:
    """A session factory that fails like an unreachable Postgres while ``down``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.down = False

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self.down:
            raise OSError("connection refused")
        return self._sessions()


class Harness:
    """A refresher over the real stores, its reads counted and its loop run as a task, as a runtime spawns it.

    The provider store reads through ``flaky``, so a test can take Postgres away from the first of the three reads.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        stack: GeoStack,
        redis_client: RedisClient,
        *,
        reconcile_interval: float = 3600.0,
    ) -> None:
        self.flaky = Flaky(sessions)
        self.registry = registry
        self.stack = stack
        self.refresher = DefinitionsRefresher(
            ProviderStore(registry, self.flaky),
            stack.settings_store,
            stack.database_store,
            redis_client=redis_client,
            reconcile_interval=reconcile_interval,
        )
        # Reads begun and reads that worked, whoever asked for them.
        self.reads = 0
        self.done = 0
        read = self.refresher.read

        async def counted() -> None:
            self.reads += 1
            await read()
            self.done += 1

        self.refresher.read = counted
        self.task: asyncio.Task[None] | None = None

    def follow(self) -> None:
        """Run the loop."""
        self.task = asyncio.create_task(self.refresher.run(), name=WorkerName.DEFINITIONS_REFRESHER)

    async def close(self) -> None:
        await self.refresher.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    @property
    def entries(self) -> int:
        """Stream entries the loop has read so far."""
        return self.refresher.streams()["changes"].entries


@pytest.fixture(autouse=True)
def _counters() -> None:
    """The loop counts into the process's job stats: each test starts from none."""
    job_stats.reset()


@pytest.fixture
async def stack(
    tmp_path: Path, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
) -> AsyncIterator[GeoStack]:
    """A worker's attribution stack before anything was read."""
    built = GeoStack.build(
        geo_settings(tmp_path),
        session_factory=sessions,
        redis_client=redis_client,
        blob_source=PostgresBlobSource(sessions),
    )
    yield built
    built.database_store.close_all()


@pytest.fixture
async def harness(
    sessions: async_sessionmaker[AsyncSession], registry: ProviderRegistry, stack: GeoStack, redis_client: RedisClient
) -> AsyncIterator[Harness]:
    built = Harness(sessions, registry, stack, redis_client)
    yield built
    await built.close()


async def _store_definitions(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> GeoDatabaseRecord:
    data = write_mmdb(tmp_path / "stored.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
    record = GeoDatabaseRecord(name="stored", sha256=hashlib.sha256(data).hexdigest())
    async with sessions() as session:
        await ProviderDescriptorRepository(session).create(ProviderRecord(id="acme", name="Acme", spec=ACME))
        await GeoSettingsRepository(session).save(GeoSettings(preflight_max_attempts=5))
        await GeoDatabaseRepository(session).create(record, data)
        await session.commit()
    return record


async def _save_descriptor(sessions: async_sessionmaker[AsyncSession], spec: dict[str, Any]) -> None:
    async with sessions() as session:
        await ProviderDescriptorRepository(session).create(ProviderRecord(id=spec["id"], name=spec["name"], spec=spec))
        await session.commit()


class TestStart:
    async def test_start_reads_the_descriptors_the_settings_and_the_databases_from_the_stream_s_position(
        self, harness: Harness, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        record = await _store_definitions(sessions, tmp_path)
        assert harness.refresher.readiness() == "definitions not loaded yet"

        await harness.refresher.start()

        assert harness.registry.get("acme") is not None
        assert harness.stack.settings_store.settings.preflight_max_attempts == 5
        assert record.id in {database.id for database in harness.stack.database_store.loaded}
        assert harness.refresher.readiness() is None
        # No stream yet: the loop follows it from its start, and owes no read of its own.
        state = harness.refresher.streams()["changes"]
        assert (state.position, state.full_reads) == ("0-0", {"start": 1})

    async def test_the_loop_owes_no_second_read_after_start(self, harness: Harness, change_log: ChangeLog) -> None:
        """The start read covers everything announced before the position; the loop goes on from there."""
        await change_log.record(ChangeKind.DESCRIPTOR, "before")
        await harness.refresher.start()
        harness.follow()

        await change_log.record(ChangeKind.PROJECT)
        await until(lambda: harness.entries == 1)
        await asyncio.sleep(0.05)

        assert harness.reads == 1
        assert harness.refresher.streams()["changes"].full_reads == {"start": 1}

    async def test_the_position_is_taken_before_the_read_so_a_change_during_it_is_read_after(
        self,
        harness: Harness,
        sessions: async_sessionmaker[AsyncSession],
        change_log: ChangeLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A descriptor saved after the start read listed the descriptors, and announced before it ended."""
        load = harness.stack.settings_store.load
        saved = False

        async def save_meanwhile() -> GeoSettings:
            nonlocal saved
            if not saved:
                saved = True
                await _save_descriptor(sessions, BETA)
                await change_log.record(ChangeKind.DESCRIPTOR, "beta")
            return await load()

        monkeypatch.setattr(harness.stack.settings_store, "load", save_meanwhile)
        await harness.refresher.start()
        assert harness.registry.get("beta") is None

        harness.follow()

        await until(lambda: harness.registry.get("beta") is not None)
        assert harness.reads == 2

    async def test_a_start_that_cannot_read_postgres_raises_and_leaves_the_process_unready(
        self, harness: Harness
    ) -> None:
        harness.flaky.down = True

        with pytest.raises(OSError):
            await harness.refresher.start()

        assert harness.refresher.readiness() == "definitions not loaded yet"

    async def test_without_redis_start_still_reads_and_the_loop_reads_in_full_once_redis_answers(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        stack: GeoStack,
        redis_client: RedisClient,
        test_redis_url: str,
        tmp_path: Path,
    ) -> None:
        await _store_definitions(sessions, tmp_path)
        # A deadline far below the pause, so start gives up on Redis in time.
        hurried = RedisClient(test_redis_url, request_timeout=0.2)
        await hurried.connect()
        built = Harness(sessions, registry, stack, hurried)
        try:
            await redis_client.client.execute_command("CLIENT", "PAUSE", 800, "ALL")
            await built.refresher.start()
            # Read without the position: the definitions are there, the stream not yet followed.
            assert registry.get("acme") is not None and built.refresher.readiness() is None
            assert built.refresher.streams()["changes"].position is None

            built.follow()

            await until(lambda: built.done == 2)
            assert built.refresher.streams()["changes"].full_reads == {"start": 1}
        finally:
            await built.close()
            await hurried.close()


class TestFollowing:
    def test_every_kind_is_either_read_on_or_skipped(self) -> None:
        """A kind added later must be placed: read on, or skipped as the controller's."""
        assert set(KEPT) == READ_ON and set(KEPT) | set(SKIPPED) == set(ChangeKind)

    @pytest.mark.parametrize("kind", KEPT)
    async def test_reads_again_on_a_kind_it_keeps(
        self, harness: Harness, change_log: ChangeLog, kind: ChangeKind
    ) -> None:
        await harness.refresher.start()
        harness.follow()

        await change_log.record(kind)

        await until(lambda: harness.done == 2)

    async def test_skips_the_kinds_the_controller_compiles(self, harness: Harness, change_log: ChangeLog) -> None:
        await harness.refresher.start()
        harness.follow()

        for kind in SKIPPED:
            await change_log.record(kind, "row-1")
        await until(lambda: harness.entries == len(SKIPPED))
        await asyncio.sleep(0.05)

        assert harness.reads == 1
        stats = job_stats.get(WorkerName.DEFINITIONS_REFRESHER)
        assert stats is not None and stats.runs == stats.idle_runs >= 1

    async def test_one_kept_kind_among_skipped_ones_read_together_reads_once(
        self, harness: Harness, change_log: ChangeLog
    ) -> None:
        await harness.refresher.start()
        for kind in [*SKIPPED[:3], ChangeKind.GEO_SETTINGS, *SKIPPED[3:]]:
            await change_log.record(kind)

        harness.follow()

        await until(lambda: harness.done == 2)
        assert harness.entries == len(SKIPPED) + 1
        await asyncio.sleep(0.05)
        assert harness.reads == 2

    async def test_a_descriptor_saved_on_another_process_is_applied_at_once(
        self, harness: Harness, sessions: async_sessionmaker[AsyncSession], change_log: ChangeLog
    ) -> None:
        await harness.refresher.start()
        harness.follow()

        await _save_descriptor(sessions, BETA)
        await change_log.record(ChangeKind.DESCRIPTOR, "beta")

        # Within a second, not at the next reconcile an hour away.
        await until(lambda: harness.registry.get("beta") is not None, timeout=1.0)

    async def test_a_gap_in_the_stream_reads_in_full(
        self, harness: Harness, change_log: ChangeLog, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stream wiped after the position: whatever it held may have announced a change."""
        monkeypatch.setattr("api.db.streams.STREAM_BLOCK_SECONDS", 0.1)
        await harness.refresher.start()
        harness.follow()
        await change_log.record(ChangeKind.PROJECT)
        await until(lambda: harness.entries == 1)

        await redis_client.client.delete(CHANGES_STREAM_KEY)

        await until(lambda: harness.done == 2)
        assert harness.refresher.streams()["changes"].full_reads == {"start": 1, "gap": 1}

    async def test_the_reconcile_reads_what_announced_nothing(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        stack: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        """A write whose entry was lost (its process died after the commit), or one made by hand in SQL."""
        built = Harness(sessions, registry, stack, redis_client, reconcile_interval=0.3)
        try:
            await built.refresher.start()
            built.follow()

            await _save_descriptor(sessions, BETA)

            await until(lambda: registry.get("beta") is not None)
            assert built.refresher.streams()["changes"].full_reads["reconcile"] >= 1
        finally:
            await built.close()


class TestFailures:
    async def test_a_read_that_fails_is_made_again_until_postgres_answers(
        self,
        harness: Harness,
        sessions: async_sessionmaker[AsyncSession],
        change_log: ChangeLog,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No later entry may come to ask again: the loop owes the read and retries it, 1 s doubling to 30 s."""
        monkeypatch.setattr("api.db.streams.RETRY_FIRST_SECONDS", 0.02)
        monkeypatch.setattr("api.db.streams.RETRY_MAX_SECONDS", 0.1)
        await _store_definitions(sessions, tmp_path)
        await harness.refresher.start()
        harness.follow()

        with structlog.testing.capture_logs() as logs:
            harness.flaky.down = True
            await _save_descriptor(sessions, BETA)
            await change_log.record(ChangeKind.DESCRIPTOR, "beta")
            await until(lambda: harness.reads - harness.done >= 3)
            # Backing off, at the 0.1 s the wait is capped at here: not as fast as Postgres refuses.
            tried = harness.reads
            await asyncio.sleep(0.5)
            assert harness.reads - tried <= 6
            # What was read stands, and the process stays ready: Postgres is the heartbeat's to report.
            assert harness.registry.get("acme") is not None and harness.registry.get("beta") is None
            assert harness.refresher.readiness() is None

            harness.flaky.down = False
            done = harness.done
            await until(lambda: harness.done > done)
            assert harness.registry.get("beta") is not None

        # Once as the streak began, at error, and once as it ended.
        mine = [(log["event"], log["log_level"]) for log in logs if log.get("loop") == WorkerName.DEFINITIONS_REFRESHER]
        assert mine.count(("Loop cycle failed", "error")) == 1
        assert mine.count(("Loop recovered", "info")) == 1
        stats = job_stats.get(WorkerName.DEFINITIONS_REFRESHER)
        assert stats is not None and stats.failures >= 3 and stats.consecutive_failures == 0

    async def test_a_change_announced_while_a_job_reads_is_read_after(
        self,
        harness: Harness,
        sessions: async_sessionmaker[AsyncSession],
        change_log: ChangeLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The job's read listed the descriptors before the save, so the read the loop owes must still be made."""
        monkeypatch.setattr("api.db.streams.RETRY_FIRST_SECONDS", 1.0)
        await harness.refresher.start()
        harness.follow()
        harness.flaky.down = True
        await change_log.record(ChangeKind.ALL)
        await until(lambda: harness.reads - harness.done == 1)
        harness.flaky.down = False
        load = harness.stack.settings_store.load

        async def save_meanwhile() -> GeoSettings:
            await _save_descriptor(sessions, BETA)
            await change_log.record(ChangeKind.DESCRIPTOR, "beta")
            # The loop, its back-off over, reads the entry and waits for this read to end.
            await until(lambda: harness.entries == 2)
            return await load()

        monkeypatch.setattr(harness.stack.settings_store, "load", save_meanwhile)
        # A sync job meeting an unknown credential type reads at once.
        await harness.refresher.read()
        monkeypatch.setattr(harness.stack.settings_store, "load", load)
        assert harness.registry.get("beta") is None

        await until(lambda: harness.registry.get("beta") is not None, timeout=3.0)

    async def test_reads_take_turns(self, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        """The loop's read and a sync job's: the later one waits, then reads what was committed before it asked."""
        load = harness.stack.settings_store.load
        running = most = 0

        async def slow_load() -> GeoSettings:
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.05)
            running -= 1
            return await load()

        monkeypatch.setattr(harness.stack.settings_store, "load", slow_load)

        await asyncio.gather(harness.refresher.read(), harness.refresher.read())

        assert (harness.done, most) == (2, 1)


class TestReporting:
    async def test_ready_once_read_and_ready_still_while_postgres_is_away(self, harness: Harness) -> None:
        """Postgres unreachable is the node's to report, from its heartbeat; the definitions read stay usable."""
        assert harness.refresher.readiness() == "definitions not loaded yet"
        await harness.refresher.start()
        assert harness.refresher.readiness() is None

        harness.flaky.down = True
        with pytest.raises(OSError):
            await harness.refresher.read()

        assert harness.refresher.readiness() is None

    async def test_streams_report_how_far_the_changes_stream_was_read(
        self, harness: Harness, change_log: ChangeLog, redis_client: RedisClient
    ) -> None:
        await harness.refresher.start()
        harness.follow()

        for kind in SKIPPED[:3]:
            await change_log.record(kind)
        await until(lambda: harness.entries == 3)

        [(last, _)] = await redis_client.client.xrevrange(CHANGES_STREAM_KEY, count=1)
        streams = harness.refresher.streams()
        assert set(streams) == {"changes"}
        assert (streams["changes"].position, streams["changes"].full_reads) == (last, {"start": 1})

    async def test_a_stop_ends_the_loop(self, harness: Harness) -> None:
        await harness.refresher.start()
        harness.follow()
        await asyncio.sleep(0.05)
        assert harness.task is not None

        await harness.refresher.stop()

        # The blocked read is cut: the loop ends without being cancelled.
        await asyncio.wait_for(asyncio.shield(harness.task), timeout=2.0)
        assert not harness.task.cancelled()
