# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the database store (config files, stored files and their sources) and the settings row."""

import asyncio
import hashlib
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.geo_repository import GeoDatabaseRepository, GeoSettingsRepository
from api.feed.models import GeoDatabaseFeed
from api.geo import store as store_module
from api.geo.models import (
    DEFAULT_CITY_RADIUS_KM,
    ConflictRule,
    GeoDatabaseKind,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    GeoSourceKind,
    GeoVendor,
)
from api.geo.settings import GeoSettingsStore, defaults_from_config
from api.geo.store import (
    BlobMissingError,
    DatabaseRetry,
    GeoDatabaseStore,
    HttpBlobSource,
    PostgresBlobSource,
    path_database_id,
)
from api.models.project import Project
from tests.geo.test_readers import IPINFO_RECORD, MAXMIND_RECORD, write_mmdb
from tests.waits import running, until


class TestConfigDatabases:
    async def test_loads_files_in_priority_order(self, tmp_path: Path) -> None:
        city = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        country = write_mmdb(tmp_path / "ipinfo.mmdb", "ipinfo country_asn", {"81.2.69.0/24": IPINFO_RECORD})
        store = GeoDatabaseStore(
            None,
            tmp_path / "cache",
            [{"path": str(city), "priority": 20}, {"path": str(country), "priority": 10, "name": "IPinfo"}],
        )
        assert await store.sync_all() == 2
        loaded = store.loaded
        assert [d.name for d in loaded] == ["IPinfo", "city.mmdb"]
        assert all(d.source == GeoDatabaseSource.PATH for d in loaded)
        assert loaded[0].id == path_database_id(str(country))

        candidates = store.lookup("81.2.69.1")
        assert [c.country for c in candidates] == ["DE", "GB"]
        assert all(c.source == GeoSourceKind.DATABASE for c in candidates)
        assert store.lookup("8.8.8.8") == []
        store.close_all()
        assert store.loaded == []

    async def test_unreadable_file_is_reported_not_fatal(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.mmdb"
        junk.write_bytes(b"nope" * 100)
        good = write_mmdb(tmp_path / "good.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(junk)}, {"path": str(good)}])
        assert await store.sync_all() == 1
        assert path_database_id(str(junk)) in store.load_errors
        assert not store.is_loaded(path_database_id(str(junk)))

    async def test_disabled_entries_are_skipped(self, tmp_path: Path) -> None:
        good = write_mmdb(tmp_path / "good.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(good), "enabled": False}])
        assert await store.sync_all() == 0

    async def test_listing_failure_keeps_stored_databases_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Postgres blip during a periodic reload must not blind attribution until the next one."""
        good = write_mmdb(tmp_path / "GeoLite2-Country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        stored = GeoDatabaseRecord(
            id="stored-1", name="stored", vendor=GeoVendor.MAXMIND, kind=GeoDatabaseKind.COUNTRY,
            sha256=hashlib.sha256(good.read_bytes()).hexdigest(),
        )
        listing: list[str] = []

        class Session:
            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        async def get_all(self: object) -> list[GeoDatabaseRecord]:
            listing.append("call")
            if len(listing) > 1:
                raise ConnectionError("postgres away")
            return [stored]

        monkeypatch.setattr(GeoDatabaseRepository, "get_all", get_all)
        store = GeoDatabaseStore(lambda: Session(), tmp_path / "cache", [{"path": str(good)}])
        await store.apply_record(stored, good.read_bytes())  # bytes cached locally, as after an upload
        assert await store.sync_all() == 2  # listing works: config file + stored row
        assert await store.sync_all() == 2  # listing raises: nothing is closed
        assert store.is_loaded("stored-1")
        assert store.lookup("81.2.69.160")

    async def test_resync_is_idempotent(self, tmp_path: Path) -> None:
        good = write_mmdb(tmp_path / "good.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(good)}])
        await store.sync_all()
        first = store.loaded[0]
        await store.sync_all()
        assert store.loaded[0] == first

    async def test_only_a_config_file_database_is_loaded_from_config(self, tmp_path: Path) -> None:
        good = write_mmdb(tmp_path / "GeoLite2-Country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        stored = GeoDatabaseRecord(
            id="stored-1", name="stored", vendor=GeoVendor.MAXMIND, kind=GeoDatabaseKind.COUNTRY,
            sha256=hashlib.sha256(good.read_bytes()).hexdigest(),
        )
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(good)}])
        assert await store.sync_all() == 1
        await store.apply_record(stored, good.read_bytes())

        assert store.is_loaded_from_config(path_database_id(str(good)))
        assert store.is_loaded("stored-1") and not store.is_loaded_from_config("stored-1")
        assert not store.is_loaded_from_config("never-opened")


class TestSettingsSeed:
    def test_legacy_geo_lookup_settings_seed_the_echo(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            geo_lookup_url="https://echo.example/ip.json",
            geo_lookup_ip_path="addr",
            geo_lookup_country_path="",
            geo_lookup_timeout_seconds=7,
        )
        seeded = defaults_from_config(settings)
        assert seeded.echo_url == "https://echo.example/ip.json"
        assert seeded.echo_ip_path == "addr" and seeded.echo_country_path is None
        assert seeded.echo_timeout_seconds == 7

    def test_geo_defaults_override(self) -> None:
        settings = Settings(geo_policy_defaults={"default_conflict_rule": "first"})  # type: ignore[call-arg]
        seeded = defaults_from_config(settings)
        assert seeded.default_policy.conflict_rule.value == "first"

    def test_invalid_defaults_fall_back(self) -> None:
        settings = Settings(geo_policy_defaults={"default_conflict_rule": "loud"})  # type: ignore[call-arg]
        assert defaults_from_config(settings).default_conflict_rule.value == "consensus"

    def test_retired_source_order_is_ignored(self) -> None:
        # A config file from before migration 042 may still list an order; it no longer means anything.
        settings = Settings(geo_policy_defaults={"default_sources": ["vendor", "database"]})  # type: ignore[call-arg]
        assert defaults_from_config(settings).default_conflict_rule.value == "consensus"

    async def test_store_without_database_keeps_seed(self) -> None:
        settings = Settings(instance_id="x")  # type: ignore[call-arg]
        store = GeoSettingsStore(settings, None)
        assert (await store.load()) == store.settings and not store.from_database


class TestCityRadiusPolicy:
    def test_install_default_reaches_the_default_policy(self) -> None:
        assert GeoSettings().default_policy.city_radius_km == DEFAULT_CITY_RADIUS_KM
        assert GeoSettings(default_city_radius_km=40).default_policy.city_radius_km == 40

    def test_seeded_from_the_config_file(self) -> None:
        settings = Settings(geo_policy_defaults={"default_city_radius_km": 10})  # type: ignore[call-arg]
        assert defaults_from_config(settings).default_policy.city_radius_km == 10

    def test_out_of_range_radius_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            GeoSettings(default_city_radius_km=-1)
        with pytest.raises(ValidationError):
            Project(name="p", username="u", password="x", location_city_radius_km=10_000)

    def test_project_inherits_or_overrides_the_radius(self) -> None:
        default = GeoSettings(default_city_radius_km=30).default_policy
        inheriting = Project(name="p", username="u", password="x")
        assert inheriting.source_policy(default).city_radius_km == 30
        stricter = Project(name="p", username="u", password="x", location_city_radius_km=5)
        assert stricter.source_policy(default).city_radius_km == 5
        names_only = Project(name="p", username="u", password="x", location_city_radius_km=0)
        assert names_only.source_policy(default).city_radius_km == 0


def _mmdb(tmp_path: Path, name: str = "country.mmdb") -> bytes:
    return write_mmdb(tmp_path / name, "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _feed(database_id: str, data: bytes, **overrides: object) -> GeoDatabaseFeed:
    """A manifest entry of the global section, as a gateway receives it."""
    values: dict[str, object] = {
        "id": database_id, "name": database_id, "vendor": GeoVendor.MAXMIND, "kind": GeoDatabaseKind.COUNTRY,
        "format": "mmdb", "source": GeoDatabaseSource.UPLOAD, "enabled": True, "priority": 100,
        "path": None, "sha256": _sha(data), "created_at": "2026-10-17T12:00:00",
    }
    values.update(overrides)
    return GeoDatabaseFeed.model_validate(values)


class FakeSource:
    """A blob source holding files by checksum; ``down`` makes it unreachable."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = {_sha(data): data for data in (files or {}).values()}
        self.down = False
        self.fetched: list[str] = []

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        self.fetched.append(database_id)
        if self.down:
            raise ConnectionError("api unreachable")
        if sha256 not in self.files:
            raise BlobMissingError("the api has no file at the requested checksum")
        path.write_bytes(self.files[sha256])


class TestStoredDatabases:
    """A gateway's store: the manifest comes from the feed, the bytes from the api."""

    async def test_the_manifest_is_loaded_from_the_source_and_config_files_are_kept(self, tmp_path: Path) -> None:
        data = _mmdb(tmp_path)
        config = write_mmdb(tmp_path / "config.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        source = FakeSource({"db": data})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(config)}], blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        assert {d.id for d in store.loaded} == {"stored-1", path_database_id(str(config))}
        assert store.missing() == [] and store.lookup("81.2.69.160")
        # The same manifest again changes nothing and fetches nothing.
        await store.sync_records([_feed("stored-1", data)])
        assert source.fetched == ["stored-1"]
        # Disabled, then gone from the manifest: closed; the config file stays.
        await store.sync_records([_feed("stored-1", data, enabled=False)])
        assert not store.is_loaded("stored-1") and store.is_loaded(path_database_id(str(config)))
        await store.sync_records([])
        assert [d.source for d in store.loaded] == [GeoDatabaseSource.PATH]
        store.close_all()

    async def test_a_copy_already_on_the_shared_volume_is_not_fetched(self, tmp_path: Path) -> None:
        data = _mmdb(tmp_path)
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "stored-1.mmdb").write_bytes(data)
        source = FakeSource({"db": data})
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        assert store.is_loaded("stored-1") and source.fetched == []
        store.close_all()

    async def test_a_file_that_could_not_be_had_is_missing_until_a_retry_gets_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        source.down = True
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        assert store.missing() == ["stored-1"] and "api unreachable" in store.load_errors["stored-1"]
        source.down = False
        # Too soon: not tried again, and not on a repeated sync either.
        assert await store.retry_missing() == 0
        await store.sync_records([_feed("stored-1", data)])
        assert source.fetched == ["stored-1"]
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.0)
        assert await store.retry_missing() == 1
        assert store.missing() == [] and store.load_errors == {} and store.is_loaded("stored-1")
        store.close_all()

    async def test_a_404_is_missing_and_retried_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data = _mmdb(tmp_path)
        source = FakeSource()
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        assert store.missing() == ["stored-1"]
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.0)
        source.files[_sha(data)] = data
        assert await store.retry_missing() == 1 and store.missing() == []
        store.close_all()

    async def test_a_corrupt_file_is_skipped_until_its_checksum_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.0)
        junk = b"not a database" * 100
        good = _mmdb(tmp_path)
        source = FakeSource({"junk": junk, "good": good})
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", junk)])
        # Logged and skipped: a gateway does not wait for it, and the same bytes are never fetched again.
        assert store.missing() == [] and "stored-1" in store.load_errors
        assert await store.retry_missing() == 0
        await store.sync_records([_feed("stored-1", junk)])
        assert source.fetched == ["stored-1"]
        # A new build at a new checksum is tried.
        await store.sync_records([_feed("stored-1", good)])
        assert store.is_loaded("stored-1") and source.fetched == ["stored-1", "stored-1"]
        store.close_all()

    async def test_a_row_without_a_file_is_not_waited_for(self, tmp_path: Path) -> None:
        """A download that never succeeded leaves a row with no file: nothing to fetch, nothing to wait for."""
        source = FakeSource()
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", b"", sha256="")])
        assert store.missing() == [] and store.load_errors == {"stored-1": "no file stored yet"} and source.fetched == []

    async def test_blob_file_serves_the_copy_at_the_checksum_asked_for(self, tmp_path: Path) -> None:
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        path = await store.blob_file("stored-1", _sha(data))
        assert path is not None and path.read_bytes() == data and source.fetched == ["stored-1"]
        # Not known at that checksum, or not at all: nothing to serve.
        assert await store.blob_file("stored-1", "0" * 64) is None
        assert await store.blob_file("other", _sha(data)) is None
        # The copy gone from disk: fetched again from the source, the checksum verified.
        path.unlink()
        store.close_all()
        assert await store.blob_file("stored-1", _sha(data)) == path and path.read_bytes() == data

    async def test_a_blob_file_fetch_cut_by_a_deadline_is_logged_by_its_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type, and nothing is served."""
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        source.down = True
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        monkeypatch.setattr(source, "fetch", AsyncMock(side_effect=TimeoutError))

        with patch.object(store_module, "logger") as log:
            assert await store.blob_file("stored-1", _sha(data)) is None

        log.warning.assert_called_once_with(
            "Could not fetch IP database bytes", database_id="stored-1", error="TimeoutError"
        )

    async def test_a_sync_that_changes_nothing_says_nothing(self, tmp_path: Path) -> None:
        """The refresher syncs on every change entry about the definitions and at its hourly read; info lines are for changes only."""
        data = _mmdb(tmp_path)
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=FakeSource({"db": data}))
        with patch.object(store_module, "logger") as log:
            await store.sync_records([_feed("stored-1", data)])
            synced = [c for c in log.info.call_args_list if c.args == ("IP databases synced",)]
            assert len(synced) == 1
            log.reset_mock()
            await store.sync_records([_feed("stored-1", data)])
            assert log.info.call_args_list == [] and log.warning.call_args_list == []
        store.close_all()


async def _still_waiting(store: GeoDatabaseStore, seconds: float = 0.05) -> bool:
    """Whether :meth:`GeoDatabaseStore.wait_missing` is still waiting after ``seconds``."""
    try:
        async with asyncio.timeout(seconds):
            await store.wait_missing()
    except TimeoutError:
        return True
    return False


class TestWaitMissing:
    """What arms a process's retry of its database files: a file it could not have, or the configured files' look on disk."""

    async def test_a_file_that_could_not_be_had_wakes_the_retry_until_a_retry_gets_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        source.down = True
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        # Nothing configured and nothing missing: nothing wakes it.
        waiter = asyncio.create_task(store.wait_missing())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        await store.sync_records([_feed("stored-1", data)])
        async with asyncio.timeout(1):
            await waiter
        # While the file is missing, it returns at once.
        async with asyncio.timeout(1):
            await store.wait_missing()
        source.down = False
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.0)
        assert await store.retry_missing() == 1
        assert await _still_waiting(store)
        store.close_all()

    async def test_a_file_that_fails_to_open_or_was_never_stored_wakes_nothing(self, tmp_path: Path) -> None:
        """Neither can a retry help: the same bytes would fail again, and there are no bytes to fetch."""
        junk = b"not a database" * 100
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=FakeSource({"junk": junk}))
        await store.sync_records([_feed("stored-1", junk), _feed("stored-2", b"", sha256="")])
        assert set(store.load_errors) == {"stored-1", "stored-2"}
        assert await _still_waiting(store)

    async def test_a_configured_file_that_cannot_be_read_is_missing_until_it_can(self, tmp_path: Path) -> None:
        path = tmp_path / "later.mmdb"
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(path)}])
        assert await store.sync_all() == 0
        async with asyncio.timeout(1):
            await store.wait_missing()
        write_mmdb(path, "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        assert await store.retry_missing() == 1 and store.load_errors == {}
        assert await _still_waiting(store)
        store.close_all()

    async def test_the_configured_files_are_due_a_look_an_interval_after_the_last(self, tmp_path: Path) -> None:
        """A file system offers no event a container can follow, so the store asks for a look on its own."""
        config = write_mmdb(tmp_path / "config.mmdb", "GeoLite2-Country", {"1.0.0.0/24": MAXMIND_RECORD})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(config)}], config_check_interval=0.5)
        assert await store.sync_all() == 1
        started = time.monotonic()
        waiter = asyncio.create_task(store.wait_missing())
        await asyncio.sleep(0.3)
        await store.sync_all()  # a look: the next is due an interval from here
        await asyncio.sleep(0.35)
        assert not waiter.done()
        async with asyncio.timeout(2):
            await waiter
        assert time.monotonic() - started >= 0.75
        store.close_all()

    async def test_a_configured_file_replaced_on_disk_is_reopened_at_the_next_look(self, tmp_path: Path) -> None:
        """geoipupdate renames each new build into place; the open reader keeps the old one until the store looks."""
        path = tmp_path / "country.mmdb"
        write_mmdb(path, "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD})
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(path)}])
        assert await store.sync_all() == 1
        assert [c.country for c in store.lookup("81.2.69.160")] == ["GB"]
        moved = path.stat().st_mtime + 60
        os.replace(write_mmdb(tmp_path / "staged.mmdb", "ipinfo country_asn", {"81.2.69.0/24": IPINFO_RECORD}), path)
        os.utime(path, (moved, moved))
        assert [c.country for c in store.lookup("81.2.69.160")] == ["GB"]
        assert await store.retry_missing() == 1  # the look a wake of wait_missing() leads to
        assert [c.country for c in store.lookup("81.2.69.160")] == ["DE"]
        store.close_all()


class TestDatabaseRetry:
    """The process's one retry of its database files, as its node runs it."""

    @pytest.fixture(autouse=True)
    def _clean_job_stats(self) -> Iterator[None]:
        """One registry per process: no test may see another's counters."""
        job_stats.reset()
        yield
        job_stats.reset()

    async def test_a_file_that_could_not_be_had_is_fetched_again_until_it_can(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.05)
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        source.down = True
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        retry = DatabaseRetry(store)
        try:
            async with running(retry):
                await until(lambda: len(source.fetched) >= 3, timeout=2)
                assert store.missing() == ["stored-1"]

                # Nothing else moves: the retry alone brings the file once its source hands it over.
                source.down = False
                await until(lambda: not store.missing(), timeout=2)
                assert [database.id for database in store.loaded] == ["stored-1"]
        finally:
            store.close_all()
        stats = job_stats.get(WorkerName.GEO_DATABASE_RETRY)
        assert stats is not None and stats.interval_seconds == 0.05 and stats.idle_runs >= 2

    async def test_a_configured_file_not_on_disk_yet_is_opened_once_it_is_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file on a shared volume ``geoipupdate`` has not filled yet: nothing announces it, the retry finds it."""
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.05)
        later = tmp_path / "later" / "city.mmdb"
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(later)}])
        assert await store.sync_all() == 0
        retry = DatabaseRetry(store)
        try:
            async with running(retry):
                await asyncio.sleep(0.2)
                assert not store.is_loaded(path_database_id(str(later)))

                # Moved into place whole, as geoipupdate does, so no retry meets half a file.
                staged = write_mmdb(tmp_path / "staged.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
                later.parent.mkdir()
                staged.replace(later)
                await until(lambda: store.is_loaded(path_database_id(str(later))), timeout=2)
        finally:
            store.close_all()

    async def test_with_nothing_missing_the_retry_waits_and_never_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.02)
        data = _mmdb(tmp_path)
        source = FakeSource({"db": data})
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=source)
        await store.sync_records([_feed("stored-1", data)])
        retry = DatabaseRetry(store)
        task = asyncio.create_task(retry.run())

        await asyncio.sleep(0.3)
        await retry.stop()
        await asyncio.wait_for(task, timeout=2)
        store.close_all()

        # Many retry spacings have passed: a retry on a timer would have run every one of them.
        stats = job_stats.get(WorkerName.GEO_DATABASE_RETRY)
        assert stats is not None and stats.runs == 0
        assert source.fetched == ["stored-1"]


class TestPostgresBlobSource:
    async def test_the_stored_bytes_at_the_checksum_or_missing(
        self, tmp_path: Path, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        data = _mmdb(tmp_path)
        record = GeoDatabaseRecord(name="stored", sha256=_sha(data))
        await GeoDatabaseRepository(db_session).create(record, data)
        await GeoDatabaseRepository(db_session).create(GeoDatabaseRecord(name="no-file"), None)
        await db_session.commit()
        source = PostgresBlobSource(db_session_factory)
        await source.fetch(record.id, _sha(data), tmp_path / "copy.mmdb")
        assert (tmp_path / "copy.mmdb").read_bytes() == data
        with pytest.raises(BlobMissingError, match="checksum"):
            await source.fetch(record.id, "0" * 64, tmp_path / "wrong.mmdb")
        assert not (tmp_path / "wrong.mmdb").exists()
        with pytest.raises(BlobMissingError, match="missing"):
            await source.fetch("unknown", _sha(data), tmp_path / "none.mmdb")

    async def test_a_store_on_postgres_loads_rows_and_their_files(
        self, tmp_path: Path, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        data = _mmdb(tmp_path)
        record = GeoDatabaseRecord(name="stored", sha256=_sha(data))
        await GeoDatabaseRepository(db_session).create(record, data)
        await db_session.commit()
        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache", blob_source=PostgresBlobSource(db_session_factory))
        assert await store.sync_all() == 1 and store.is_loaded(record.id)
        assert await store.blob_file(record.id, _sha(data)) == tmp_path / "cache" / f"{record.id}.mmdb"
        store.close_all()


@pytest.fixture
def api_server() -> Iterator[tuple[str, dict[str, bytes], list[dict[str, str]]]]:
    """A stand-in for the api's internal endpoint on a real socket: files by checksum, behind the token."""
    files: dict[str, bytes] = {}
    requests: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the name http.server dispatches to)
            url = urlparse(self.path)
            sha = parse_qs(url.query).get("sha256", [""])[0]
            requests.append({"path": url.path, "sha256": sha, "auth": self.headers.get("Authorization", "")})
            if self.headers.get("Authorization") != "Bearer secret-token":
                self.send_response(401)
                self.end_headers()
                return
            body = files.get(sha)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", files, requests
    finally:
        server.shutdown()
        server.server_close()


class TestHttpBlobSource:
    async def test_downloads_the_file_with_the_token(
        self, tmp_path: Path, api_server: tuple[str, dict[str, bytes], list[dict[str, str]]]
    ) -> None:
        url, files, requests = api_server
        data = _mmdb(tmp_path)
        files[_sha(data)] = data
        target = tmp_path / "stored-1.mmdb"
        await HttpBlobSource(url, "secret-token").fetch("stored-1", _sha(data), target)
        assert target.read_bytes() == data and oct(target.stat().st_mode & 0o777) == "0o600"
        assert requests == [
            {"path": "/internal/v1/geo/databases/stored-1/blob", "sha256": _sha(data), "auth": "Bearer secret-token"}
        ]
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []

    async def test_a_404_is_missing_and_other_failures_raise(
        self, tmp_path: Path, api_server: tuple[str, dict[str, bytes], list[dict[str, str]]]
    ) -> None:
        url, files, _ = api_server
        target = tmp_path / "stored-1.mmdb"
        with pytest.raises(BlobMissingError):
            await HttpBlobSource(url, "secret-token").fetch("stored-1", "0" * 64, target)
        with pytest.raises(httpx.HTTPStatusError):
            await HttpBlobSource(url, "wrong-token").fetch("stored-1", "0" * 64, target)
        # Bytes that are not what was asked for never land.
        files["0" * 64] = b"something else"
        with pytest.raises(BlobMissingError, match="checksum"):
            await HttpBlobSource(url, "secret-token").fetch("stored-1", "0" * 64, target)
        assert list(tmp_path.iterdir()) == []

    async def test_an_api_that_cannot_be_reached_is_tried_again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=HttpBlobSource("http://127.0.0.1:9/", "t"))
        data = _mmdb(tmp_path)
        await store.sync_records([_feed("stored-1", data)])
        assert store.missing() == ["stored-1"] and store.load_errors["stored-1"].startswith("fetch failed")

    async def test_a_gateway_store_end_to_end(
        self, tmp_path: Path, api_server: tuple[str, dict[str, bytes], list[dict[str, str]]]
    ) -> None:
        url, files, _ = api_server
        data = _mmdb(tmp_path)
        files[_sha(data)] = data
        store = GeoDatabaseStore(None, tmp_path / "cache", blob_source=HttpBlobSource(url, "secret-token"))
        await store.sync_records([_feed("stored-1", data)])
        assert store.missing() == [] and store.lookup("81.2.69.160")[0].country == "GB"
        store.close_all()


class TestSettingsAdopt:
    def test_adopt_applies_only_a_change(self) -> None:
        store = GeoSettingsStore(Settings(instance_id="x"), None)  # type: ignore[call-arg]
        defaults = store.settings
        assert not store.adopt(defaults, stored=False) and store.settings == defaults and not store.from_database
        stored = GeoSettings(default_conflict_rule=ConflictRule.FIRST)
        with patch("api.geo.settings.logger") as log:
            assert store.adopt(stored, stored=True) and store.settings == stored and store.from_database
            assert not store.adopt(stored.model_copy(), stored=True)
            assert len(log.info.call_args_list) == 1
        # The row is gone: back to the config defaults.
        assert store.adopt(defaults, stored=False) and store.settings == defaults and not store.from_database

    async def test_a_missing_row_reads_the_same_from_every_source(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Role ``all`` shares one store between the api's reads, the controller's compile and a gateway's feed.

        With no row saved, each hands over the config defaults as not stored, so none of them flips the store.
        """
        settings = Settings(instance_id="x")  # type: ignore[call-arg]
        store = GeoSettingsStore(settings, db_session_factory)
        with patch("api.geo.settings.logger") as log:
            await store.load()
            assert not store.adopt(defaults_from_config(settings), stored=False)
            await store.load()
            assert not log.info.called and not store.from_database

    async def test_load_adopts_the_row(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        store = GeoSettingsStore(Settings(instance_id="x"), db_session_factory)  # type: ignore[call-arg]
        await store.load()
        assert not store.from_database
        await GeoSettingsRepository(db_session).save(GeoSettings(default_city_radius_km=40))
        await db_session.commit()
        assert (await store.load()).default_city_radius_km == 40 and store.from_database
