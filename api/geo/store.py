# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Keeps this instance's open IP databases in step with Postgres and the config file.

Two sources of databases:

* Rows in ``geo_databases`` whose bytes live in ``geo_database_blobs``. Each
  instance copies the bytes to ``geo_cache_dir/<id>.<ext>`` once per checksum
  and memory-maps that file. Peers learn about a change through the
  ``geo_database_changed`` signal and re-sync just that one row.
* Operator-managed files named in the config (``geo.databases``), for
  installs that already run ``geoipupdate`` on a shared volume. They are never
  copied; a changed file on disk is picked up on the periodic re-sync.

Lookups iterate the enabled readers in priority order and never block on I/O
beyond a page fault.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.fileio import atomic_write
from api.db.geo_repository import GeoDatabaseRepository
from api.geo.models import (
    GeoDatabaseFormat,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSourceKind,
    LoadedDatabase,
    LocationCandidate,
)
from api.geo.readers import GeoDatabaseError, GeoReader, inspect_file, open_reader

logger = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_EXTENSIONS = {GeoDatabaseFormat.MMDB: "mmdb", GeoDatabaseFormat.IP2LOCATION_BIN: "BIN"}


@dataclass
class _Entry:
    record: GeoDatabaseRecord
    reader: GeoReader
    path: Path

    def loaded(self) -> LoadedDatabase:
        info = self.reader.info
        return LoadedDatabase(
            id=self.record.id,
            name=self.record.name,
            vendor=self.record.vendor,
            kind=self.record.kind,
            format=self.record.format,
            source=self.record.source,
            priority=self.record.priority,
            path=str(self.path),
            size_bytes=info.size_bytes,
            database_type=info.database_type,
            build_epoch=info.build_epoch,
            record_count=info.record_count,
        )


def path_database_id(path: str) -> str:
    """Stable id for an operator-managed file: derived from its path, not its content."""
    return "path-" + hashlib.sha1(path.encode()).hexdigest()[:16]


class GeoDatabaseStore:
    """The databases this process has open, and how they got here."""

    def __init__(
        self,
        session_factory: SessionFactory | None,
        cache_dir: str | Path,
        config_databases: list[dict[str, object]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cache_dir = Path(cache_dir)
        self._config_databases = list(config_databases or [])
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._load_errors: dict[str, str] = {}
        # Config-file databases are re-inspected (hashed) only when the file's
        # size or mtime moved; sync_all runs on every periodic full reload.
        self._path_cache: dict[str, tuple[tuple[int, float], GeoDatabaseRecord]] = {}

    # --- state ---------------------------------------------------------------------

    @property
    def loaded(self) -> list[LoadedDatabase]:
        """Open databases in lookup order."""
        return [e.loaded() for e in self._ordered()]

    @property
    def load_errors(self) -> dict[str, str]:
        """Database id to the reason it is not open on this instance."""
        return dict(self._load_errors)

    def is_loaded(self, database_id: str) -> bool:
        return database_id in self._entries

    def _ordered(self) -> list[_Entry]:
        return sorted(self._entries.values(), key=lambda e: (e.record.priority, e.record.created_at))

    # --- lookups -------------------------------------------------------------------

    def lookup(self, ip: str) -> list[LocationCandidate]:
        """Every enabled database's answer for ``ip``, highest priority first.

        Databases without a record for the IP are skipped, so a country
        database and an ASN database side by side yield one candidate each
        and the resolver merges them.
        """
        candidates: list[LocationCandidate] = []
        for entry in self._ordered():
            if not entry.record.enabled:
                continue
            location = entry.reader.lookup(ip)
            if location is None:
                continue
            candidates.append(
                LocationCandidate(
                    source=GeoSourceKind.DATABASE,
                    origin=entry.record.id,
                    country=location.country,
                    location=location,
                )
            )
        return candidates

    # --- sync ----------------------------------------------------------------------

    async def sync_all(self) -> int:
        """Open every enabled database from Postgres and the config; close the rest."""
        async with self._lock:
            records: dict[str, GeoDatabaseRecord] = {}
            for record in self._config_records():
                records[record.id] = record
            if self._session_factory is not None:
                try:
                    async with self._session_factory() as session:
                        for record in await GeoDatabaseRepository(session).get_all():
                            records[record.id] = record
                except Exception as exc:
                    logger.warning("Could not list IP databases", error=str(exc))
            for database_id in list(self._entries):
                if database_id not in records or not records[database_id].enabled:
                    self._close(database_id)
            for record in records.values():
                if record.enabled:
                    await self._ensure_open(record)
            logger.info("IP databases synced", loaded=len(self._entries), errors=len(self._load_errors))
            return len(self._entries)

    async def reload_one(self, database_id: str, op: str | None) -> None:
        """Apply a cross-instance change for one stored database."""
        async with self._lock:
            if op == "removed" or self._session_factory is None:
                self._close(database_id)
                return
            async with self._session_factory() as session:
                record = await GeoDatabaseRepository(session).get_by_id(database_id)
            if record is None or not record.enabled:
                self._close(database_id)
                return
            await self._ensure_open(record)

    async def apply_record(self, record: GeoDatabaseRecord, blob: bytes | None = None) -> None:
        """Open a record this instance just wrote, without re-reading Postgres."""
        async with self._lock:
            if not record.enabled:
                self._close(record.id)
                return
            if blob is not None:
                path = self._cache_path(record)
                await asyncio.to_thread(atomic_write, path, blob, mode=0o600)
            await self._ensure_open(record)

    def remove(self, database_id: str) -> None:
        self._close(database_id)

    def close_all(self) -> None:
        for database_id in list(self._entries):
            self._close(database_id)

    # --- helpers -------------------------------------------------------------------

    def _config_records(self) -> list[GeoDatabaseRecord]:
        records: list[GeoDatabaseRecord] = []
        for item in self._config_databases:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            database_id = path_database_id(path)
            try:
                priority = int(item.get("priority", 50))  # type: ignore[call-overload]
            except (TypeError, ValueError):
                priority = 50
            try:
                stat = Path(path).stat()
                stamp = (stat.st_size, stat.st_mtime)
            except OSError as exc:
                self._load_errors[database_id] = str(exc)
                logger.warning("Configured IP database unreadable", path=path, error=str(exc))
                continue
            cached = self._path_cache.get(path)
            if cached is not None and cached[0] == stamp:
                records.append(cached[1])
                continue
            try:
                info = inspect_file(path)
            except GeoDatabaseError as exc:
                self._load_errors[database_id] = str(exc)
                logger.warning("Configured IP database unreadable", path=path, error=str(exc))
                continue
            self._path_cache[path] = (
                stamp,
                GeoDatabaseRecord(
                    id=database_id,
                    name=str(item.get("name") or Path(path).name),
                    vendor=info.vendor,
                    kind=info.kind,
                    format=info.format,
                    source=GeoDatabaseSource.PATH,
                    enabled=bool(item.get("enabled", True)),
                    priority=priority,
                    path=path,
                    sha256=info.sha256,
                    size_bytes=info.size_bytes,
                    database_type=info.database_type,
                    build_epoch=info.build_epoch,
                    record_count=info.record_count,
                    ip_version=info.ip_version,
                    languages=info.languages,
                    description=info.description,
                    attribution=info.attribution,
                ),
            )
            records.append(self._path_cache[path][1])
        return records

    def _cache_path(self, record: GeoDatabaseRecord) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"{record.id}.{_EXTENSIONS.get(record.format, 'db')}"

    async def _ensure_open(self, record: GeoDatabaseRecord) -> None:
        """Open ``record`` if it is not open at this checksum yet."""
        current = self._entries.get(record.id)
        if current is not None and current.record.sha256 == record.sha256 and current.record.sha256:
            # Same file; only the row's settings may have moved (name, priority).
            current.record = record
            return
        if record.source == GeoDatabaseSource.PATH:
            path = Path(record.path or "")
        else:
            path = self._cache_path(record)
            if not await self._cached_matches(path, record.sha256) and not await self._fetch_blob(record, path):
                return
        try:
            reader = await asyncio.to_thread(open_reader, path, record.format)
        except GeoDatabaseError as exc:
            self._load_errors[record.id] = str(exc)
            logger.error("IP database failed to open", database_id=record.id, path=str(path), error=str(exc))
            return
        self._close(record.id)
        self._entries[record.id] = _Entry(record=record, reader=reader, path=path)
        self._load_errors.pop(record.id, None)
        logger.info(
            "IP database loaded",
            database_id=record.id,
            name=record.name,
            vendor=record.vendor.value,
            kind=record.kind.value,
            path=str(path),
        )

    async def _cached_matches(self, path: Path, sha256: str) -> bool:
        if not sha256 or not path.exists():
            return False

        def digest() -> str:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        return await asyncio.to_thread(digest) == sha256

    async def _fetch_blob(self, record: GeoDatabaseRecord, path: Path) -> bool:
        if self._session_factory is None:
            self._load_errors[record.id] = "no database connection to fetch the file from"
            return False
        try:
            async with self._session_factory() as session:
                blob = await GeoDatabaseRepository(session).get_blob(record.id)
        except Exception as exc:
            self._load_errors[record.id] = f"fetch failed: {exc}"
            logger.warning("Could not fetch IP database bytes", database_id=record.id, error=str(exc))
            return False
        if blob is None:
            self._load_errors[record.id] = "stored file is missing"
            logger.warning("IP database row has no stored file", database_id=record.id)
            return False
        await asyncio.to_thread(atomic_write, path, blob, mode=0o600)
        logger.info("IP database cached", database_id=record.id, bytes=len(blob), path=str(path))
        return True

    def _close(self, database_id: str) -> None:
        entry = self._entries.pop(database_id, None)
        if entry is not None:
            with contextlib.suppress(Exception):
                entry.reader.close()
        self._load_errors.pop(database_id, None)
