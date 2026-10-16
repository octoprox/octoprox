# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Scheduled database downloads: redirects are followed by hand, each hop vetted."""

import gzip
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.db.geo_repository import GeoDatabaseRepository
from api.geo import updater
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.store import GeoDatabaseStore
from api.geo.updater import (
    DownloadError,
    GeoDatabaseUpdater,
    TickResult,
    candidate_urls,
    download,
    expand_update_url,
    next_update_at,
)
from api.providers.sdk.egress import EgressGuard, EgressPolicy
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

OPEN = EgressPolicy(allow_http=True, allow_private=True, pin_dns=False)
# Captured before any test patches the module attribute, so repeated patching does not nest.
_REAL_CLIENT = httpx.AsyncClient


def _record(
    url: str = "https://download.vendor.test/db.mmdb.gz", update_interval_hours: int = 0, **auth: str
) -> GeoDatabaseRecord:
    return GeoDatabaseRecord(
        name="db",
        source=GeoDatabaseSource.URL,
        update_url=url,
        update_interval_hours=update_interval_hours,
        update_auth=dict(auth),
    )


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> list[httpx.Request]:
    """Route every client the downloader builds through ``handler`` and record the requests."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator]

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(recording), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(updater.httpx, "AsyncClient", factory)
    return seen


class TestDatedUrls:
    def test_placeholders_fill_the_current_month_then_the_previous(self) -> None:
        template = "https://download.db-ip.com/free/dbip-country-lite-{YYYY}-{MM}.mmdb.gz"
        assert expand_update_url(template, datetime(2026, 1, 3)) == "https://download.db-ip.com/free/dbip-country-lite-2026-01.mmdb.gz"
        assert candidate_urls(template, now=datetime(2026, 1, 3, 9, 0)) == [
            "https://download.db-ip.com/free/dbip-country-lite-2026-01.mmdb.gz",
            "https://download.db-ip.com/free/dbip-country-lite-2025-12.mmdb.gz",
        ]
        # A fixed URL is tried once, as it is.
        assert candidate_urls("https://download.maxmind.com/x?suffix=tar.gz") == ["https://download.maxmind.com/x?suffix=tar.gz"]

    async def test_falls_back_to_last_month_only_on_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime(2026, 9, 1, 0, 5)
        monkeypatch.setattr(updater, "utc_now", lambda: now)
        payload = gzip.compress(b"september-not-yet")

        def handler(request: httpx.Request) -> httpx.Response:
            if "2026-09" in request.url.path:
                return httpx.Response(404)
            assert "2026-08" in request.url.path
            return httpx.Response(200, content=payload)

        seen = _use_transport(monkeypatch, handler)
        record = _record("https://download.db-ip.com/free/dbip-country-lite-{YYYY}-{MM}.mmdb.gz")
        assert await download(record, EgressGuard(OPEN)) == b"september-not-yet"
        assert [r.url.path for r in seen] == [
            "/free/dbip-country-lite-2026-09.mmdb.gz",
            "/free/dbip-country-lite-2026-08.mmdb.gz",
        ]

        # Any other failure stops at the first URL; a fixed URL never falls back.
        _use_transport(monkeypatch, lambda request: httpx.Response(500))
        with pytest.raises(DownloadError, match="HTTP 500"):
            await download(record, EgressGuard(OPEN))
        seen = _use_transport(monkeypatch, lambda request: httpx.Response(404))
        with pytest.raises(DownloadError, match="HTTP 404"):
            await download(_record("https://download.vendor.test/fixed.mmdb"), EgressGuard(OPEN))
        assert len(seen) == 1


class TestDownloadRedirects:
    async def test_redirect_to_storage_drops_vendor_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = gzip.compress(b"mmdb-bytes")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "download.vendor.test":
                assert request.headers["Authorization"] == "Bearer secret"
                return httpx.Response(302, headers={"Location": "https://storage.example.test/signed/db.mmdb.gz?sig=1"})
            assert request.url.host == "storage.example.test"
            assert "authorization" not in request.headers and request.headers["Host"] == "storage.example.test"
            return httpx.Response(200, content=payload)

        seen = _use_transport(monkeypatch, handler)
        assert await download(_record(token="secret"), EgressGuard(OPEN)) == b"mmdb-bytes"
        assert [r.url.host for r in seen] == ["download.vendor.test", "storage.example.test"]

    async def test_redirect_target_is_vetted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

        _use_transport(monkeypatch, handler)
        with pytest.raises(DownloadError, match="egress policy"):
            await download(_record(), EgressGuard(EgressPolicy(allow_http=True, allow_private=False, pin_dns=False)))

    async def test_redirect_loop_gives_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": str(request.url)})

        seen = _use_transport(monkeypatch, handler)
        with pytest.raises(DownloadError, match="redirects"):
            await download(_record(), EgressGuard(OPEN))
        assert len(seen) == updater.MAX_REDIRECTS + 1

    async def test_relative_location_and_plain_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/db.mmdb":
                return httpx.Response(301, headers={"Location": "/files/db.mmdb"})
            return httpx.Response(200, content=b"raw")

        _use_transport(monkeypatch, handler)
        assert await download(_record("https://download.vendor.test/db.mmdb"), EgressGuard(OPEN)) == b"raw"


class TestRefresh:
    async def test_stores_and_publishes_only_when_the_file_changed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        _use_transport(monkeypatch, lambda request: httpx.Response(200, content=gzip.compress(mmdb)))
        published: list[tuple[str, str]] = []

        async def publish(database_id: str, op: str) -> None:
            published.append((database_id, op))

        record = _record(update_interval_hours=24)
        await GeoDatabaseRepository(db_session).create(record, None)
        await db_session.commit()
        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache")
        refresher = GeoDatabaseUpdater(
            db_session_factory, MagicMock(), "inst-1", store, publish, egress_policy=OPEN
        )

        await refresher.refresh(record)
        assert record.last_update_error is None and record.sha256 == hashlib.sha256(mmdb).hexdigest()
        assert await GeoDatabaseRepository(db_session).get_blob(record.id) == mmdb
        assert published == [(record.id, "updated")]
        assert [d.id for d in store.loaded] == [record.id]

        # Same bytes again: nothing stored, nothing announced.
        await refresher.refresh(record)
        assert published == [(record.id, "updated")]
        store.close_all()


class TestSchedule:
    def test_next_update_at(self) -> None:
        record = _record(update_interval_hours=24)
        # Never run: due from the moment it was registered.
        assert next_update_at(record) == record.created_at
        record.last_update_at = utc_now()
        assert next_update_at(record) == record.last_update_at + timedelta(hours=24)
        record.update_paused = True
        assert next_update_at(record) is None
        record.update_paused = False
        record.enabled = False
        assert next_update_at(record) is None
        assert next_update_at(_record(update_interval_hours=0)) is None
        uploaded = GeoDatabaseRecord(name="up", source=GeoDatabaseSource.UPLOAD, update_interval_hours=24)
        assert next_update_at(uploaded) is None

    async def test_a_paused_schedule_is_skipped_until_resumed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        seen = _use_transport(monkeypatch, lambda request: httpx.Response(200, content=gzip.compress(mmdb)))

        async def publish(database_id: str, op: str) -> None:
            pass

        repo = GeoDatabaseRepository(db_session)
        record = _record(update_interval_hours=24)
        record.update_paused = True
        await repo.create(record, None)
        await db_session.commit()
        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache")
        ticker = GeoDatabaseUpdater(db_session_factory, MagicMock(), "inst-1", store, publish, egress_policy=OPEN)

        # The tick also sweeps the stored files, against the real table.
        assert (await ticker.tick()).idle and seen == []

        record.update_paused = False
        await repo.update(record)
        await db_session.commit()
        assert (await ticker.tick()).refreshed == 1 and len(seen) == 1
        stored = await repo.get_by_id(record.id)
        assert stored is not None and stored.last_update_at is not None and stored.update_paused is False
        store.close_all()

    async def test_a_disabled_database_is_downloaded_by_hand_only_and_stays_unloaded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        seen = _use_transport(monkeypatch, lambda request: httpx.Response(200, content=gzip.compress(mmdb)))
        reattributed: list[bool] = []

        async def publish(database_id: str, op: str) -> None:
            pass

        async def after_update() -> None:
            reattributed.append(True)

        repo = GeoDatabaseRepository(db_session)
        record = _record(update_interval_hours=24)
        record.enabled = False
        await repo.create(record, None)
        await db_session.commit()
        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache")
        ticker = GeoDatabaseUpdater(
            db_session_factory, MagicMock(), "inst-1", store, publish, after_update=after_update, egress_policy=OPEN
        )

        assert (await ticker.tick()).refreshed == 0 and seen == []

        # Download now: the file is stored, the row stays disabled, nothing loads it or is re-judged.
        refreshed = await ticker.refresh(record)
        assert len(seen) == 1 and refreshed.enabled is False
        assert await repo.get_blob(record.id) == mmdb
        assert store.loaded == [] and reattributed == []
        store.close_all()

    async def test_a_failed_sweep_does_not_stop_the_downloads(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        seen = _use_transport(monkeypatch, lambda request: httpx.Response(200, content=gzip.compress(mmdb)))

        async def failing(_conn: object) -> bool:
            raise RuntimeError("permission denied")

        async def publish(database_id: str, op: str) -> None:
            pass

        monkeypatch.setattr(updater, "reclaim_blob_space", failing)
        await GeoDatabaseRepository(db_session).create(_record(update_interval_hours=24), None)
        await db_session.commit()
        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache")
        ticker = GeoDatabaseUpdater(db_session_factory, MagicMock(), "inst-1", store, publish, egress_policy=OPEN)
        assert (await ticker.tick()).refreshed == 1 and len(seen) == 1
        store.close_all()

    async def test_a_pause_made_during_a_download_survives_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        _use_transport(monkeypatch, lambda request: httpx.Response(200, content=gzip.compress(mmdb)))

        async def publish(database_id: str, op: str) -> None:
            pass

        repo = GeoDatabaseRepository(db_session)
        record = _record(update_interval_hours=24)
        await repo.create(record, None)
        await db_session.commit()
        # The tick read the row; then an admin paused it before the download finished.
        stale = await repo.get_by_id(record.id)
        assert stale is not None
        record.update_paused = True
        await repo.update(record)
        await db_session.commit()

        store = GeoDatabaseStore(db_session_factory, tmp_path / "cache")
        ticker = GeoDatabaseUpdater(db_session_factory, MagicMock(), "inst-1", store, publish, egress_policy=OPEN)
        refreshed = await ticker.refresh(stale)
        stored = await repo.get_by_id(record.id)
        assert stored is not None and stored.update_paused is True and stored.last_update_at is not None
        assert refreshed.update_paused is True
        store.close_all()

    async def test_the_tick_rewrites_a_bloated_blob_table(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The sweep goes through the session factory's own connection, put in autocommit."""
        size = 2 * 1024 * 1024  # random so TOAST cannot compress it away
        repo = GeoDatabaseRepository(db_session)
        record = GeoDatabaseRecord(name="City", sha256="a" * 64, size_bytes=size)
        await repo.create(record, os.urandom(size))
        await db_session.commit()
        for _ in range(3):
            await repo.update(record, blob=os.urandom(size))
            await db_session.commit()

        async def table_bytes() -> int:
            result = await db_session.execute(text("SELECT pg_total_relation_size('geo_database_blobs')"))
            await db_session.commit()
            return int(result.scalar_one())

        assert await table_bytes() >= 3 * size
        ticker = GeoDatabaseUpdater(db_session_factory, MagicMock(), "inst-1", MagicMock(), MagicMock(), egress_policy=OPEN)
        # Nothing was due, but the rewrite is work: the run is not reported idle.
        assert await ticker.tick() == TickResult(refreshed=0, reclaimed=True)
        assert await table_bytes() < size + size // 2
        assert (await ticker.tick()).idle
