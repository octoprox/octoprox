# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-process attribution stack: one composition, the file source picked per role."""

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.roles import JobKind
from api.db.geo_repository import GeoDatabaseRepository, GeoSettingsRepository
from api.db.redis import OBSERVATION_STREAM_KEY, RedisClient
from api.db.streams import ChangeLog
from api.feed.models import GeoDatabaseFeed
from api.geo.models import GeoDatabaseRecord, GeoSettings, IpObservation, ObservationSource
from api.geo.runtime import GeoStack
from api.geo.store import GeoDatabaseStore, PostgresBlobSource, path_database_id
from api.jobs.queue import JobQueue
from tests.conftest import queued_job_keys
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.geo.test_store_and_policy import FakeSource


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    config_file = write_mmdb(tmp_path / "config.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
    values: dict[str, object] = {
        "instance_id": "proc-1",
        "geo_cache_dir": str(tmp_path / "cache"),
        "geo_databases": [{"path": str(config_file)}],
        "geo_observation_publish_interval": 2.5,
        "geo_observation_max_buffer": 40,
        "reconcile_interval": 120.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class TestPostgresRoles:
    """api, worker, controller and all: the list, the settings and the files from Postgres."""

    async def test_a_resync_loads_the_settings_row_and_the_stored_files(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        data = write_mmdb(tmp_path / "stored.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        record = GeoDatabaseRecord(name="stored", sha256=hashlib.sha256(data).hexdigest())
        await GeoDatabaseRepository(db_session).create(record, data)
        await GeoSettingsRepository(db_session).save(GeoSettings(preflight_max_attempts=5))
        await db_session.commit()
        settings = _settings(tmp_path)
        stack = GeoStack.build(
            settings, session_factory=db_session_factory, redis_client=redis_client,
            blob_source=PostgresBlobSource(db_session_factory),
        )
        # One service over the stack's own stores, recorder and rules.
        assert stack.geo_service.database_store is stack.database_store
        assert stack.geo_service.settings_store is stack.settings_store
        assert stack.geo_service.observation_recorder is stack.observation_recorder
        assert stack.extraction_rules.default_check_url == stack.geo_service.settings.echo_url
        assert stack.database_store.loaded == []  # nothing is read until asked
        await stack.geo_service.resync()
        assert stack.geo_service.settings.preflight_max_attempts == 5 and stack.settings_store.from_database
        assert {d.id for d in stack.database_store.loaded} == {record.id, path_database_id(settings.geo_databases[0]["path"])}
        assert (tmp_path / "cache" / f"{record.id}.mmdb").read_bytes() == data

        # Closing the stack closes the readers and leaves the recorder to the node, whose stop sends what it holds.
        publishing = asyncio.create_task(stack.observation_recorder.run())
        stack.geo_service.record(IpObservation(source=ObservationSource.MANUAL, ip="81.2.69.160"))
        await stack.close()
        assert stack.database_store.loaded == [] and not stack.observation_recorder.stopped
        await stack.observation_recorder.stop()
        await asyncio.wait_for(publishing, timeout=1)
        assert await redis_client.client.xlen(OBSERVATION_STREAM_KEY) == 1

    async def test_the_pieces_take_their_settings(
        self, tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        settings = _settings(tmp_path, provider_egress_allow_http=True)
        stack = GeoStack.build(settings, session_factory=db_session_factory, redis_client=redis_client, blob_source=None)
        assert stack.observation_recorder.interval == 2.5
        for n in range(41):
            stack.observation_recorder.record(IpObservation(source=ObservationSource.MANUAL, ip=f"10.0.0.{n}"))
        assert (stack.observation_recorder.pending, stack.observation_recorder.dropped) == (40, 1)
        # The configured files are looked at on disk with every other hourly read.
        assert stack.database_store._config_check_interval == 120.0

        changes = ChangeLog(redis_client)
        updater = stack.updater(settings, session_factory=db_session_factory, changes=changes, jobs=JobQueue(redis_client))
        assert updater._guard._policy.allow_http and updater._changes is changes
        assert updater._store is stack.database_store
        await updater.tick()
        # A new file of an enabled database asks for every proxy to be judged again.
        assert updater._after_update is not None
        await updater._after_update()
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]


class TestGateway:
    """No Postgres: the feed brings the list and the settings, the api the files."""

    async def test_a_gateway_stack_is_filled_by_the_feed(self, tmp_path: Path, redis_client: RedisClient) -> None:
        data = write_mmdb(tmp_path / "stored.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        source = FakeSource({"stored": data})
        settings = _settings(tmp_path)
        stack = GeoStack.build(settings, session_factory=None, redis_client=redis_client, blob_source=source)
        # A resync finds nothing to read, and must not drop what the feed brought.
        await stack.geo_service.resync()
        assert not stack.settings_store.from_database
        manifest = GeoDatabaseFeed(
            id="stored-1", name="stored", vendor="maxmind", kind="city", format="mmdb", source="upload",
            enabled=True, priority=100, path=None, sha256=hashlib.sha256(data).hexdigest(), created_at="2026-10-17T12:00:00",
        )
        await stack.database_store.sync_records([manifest])
        assert stack.settings_store.adopt(GeoSettings(preflight_max_attempts=7), stored=True)
        await stack.geo_service.resync()
        assert stack.database_store.is_loaded("stored-1") and stack.database_store.missing() == []
        assert stack.geo_service.settings.preflight_max_attempts == 7
        assert stack.geo_service.attribute("81.2.69.160")[0].country == "GB"
        await stack.close()


class TestEcho:
    async def test_echo_opens_the_configured_files_only(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        store = GeoDatabaseStore.echo(settings)
        assert await store.sync_all() == 1 and store.lookup("1.0.0.1")[0].country == "GB"
        assert store.missing() == []
        store.close_all()
