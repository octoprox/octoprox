# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the database store (config-file databases) and the settings seed."""

import hashlib
from pathlib import Path

import pytest

from api.core.config import Settings
from api.db.geo_repository import GeoDatabaseRepository
from api.geo.models import (
    GeoDatabaseKind,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSourceKind,
    GeoVendor,
)
from api.geo.settings import GeoSettingsStore, defaults_from_config
from api.geo.store import GeoDatabaseStore, path_database_id
from tests.geo.test_readers import IPINFO_RECORD, MAXMIND_RECORD, write_mmdb


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
        settings = Settings(geo_policy_defaults={"default_sources": ["vendor", "database"], "default_conflict_rule": "first"})  # type: ignore[call-arg]
        seeded = defaults_from_config(settings)
        assert seeded.default_policy.sources == [GeoSourceKind.VENDOR, GeoSourceKind.DATABASE]
        assert seeded.default_policy.conflict_rule.value == "first"

    def test_invalid_defaults_fall_back(self) -> None:
        settings = Settings(geo_policy_defaults={"default_sources": []})  # type: ignore[call-arg]
        assert defaults_from_config(settings).default_sources[0] == GeoSourceKind.DATABASE

    async def test_store_without_database_keeps_seed(self) -> None:
        settings = Settings(instance_id="x")  # type: ignore[call-arg]
        store = GeoSettingsStore(settings, None)
        assert (await store.load()) == store.settings and not store.from_database
