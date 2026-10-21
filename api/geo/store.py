# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Keeps this process's open IP databases in step with the stored database list and the configured files.

Stored files are copied to ``geo_cache_dir`` and checked by sha256; configured files (``geo.databases``) open in place.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

import httpx
import structlog

from api.core.fileio import atomic_write
from api.core.logging import error_text
from api.core.loops import Loop
from api.core.workers import WorkerName
from api.db.geo_repository import GeoDatabaseRepository
from api.db.session import SessionFactory
from api.feed.codec import internal_token
from api.geo.models import (
    GeoDatabaseFormat,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSourceKind,
    LoadedDatabase,
    LocationCandidate,
)
from api.geo.places import Point
from api.geo.readers import GeoDatabaseError, GeoNamesReader, GeoReader, inspect_file, open_reader

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.feed.models import GeoDatabaseFeed

logger = structlog.get_logger()

# How long a database whose file could not be had waits before it is tried
# again, in seconds. A gateway that started before the api is serving loads
# its files within one retry of the api coming up, and one that cannot reach
# the api asks it no more than twice a minute per database.
GEO_BLOB_RETRY_SECONDS = 30.0
# How long the configured files go between looks on disk when nothing looks
# sooner, in seconds: the default reconcile_interval, which GeoStack.build
# passes instead. A look is a stat per file; one whose size or mtime moved is
# inspected again and reopened.
CONFIG_CHECK_SECONDS = 3600.0
# Largest file the HTTP source accepts, in bytes: the cap on uploads and
# vendor downloads, so nothing larger can be stored to begin with.
MAX_BLOB_BYTES = 512 * 1024 * 1024
# How long the HTTP source waits to connect, and for each chunk once
# connected, in seconds. A file streams for as long as chunks keep coming.
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Bytes per chunk written to disk off the event loop: a 512 MB file is 512
# thread hops, each hashing and writing one chunk.
_CHUNK_BYTES = 1024 * 1024

_EXTENSIONS = {
    GeoDatabaseFormat.MMDB: "mmdb",
    GeoDatabaseFormat.IP2LOCATION_BIN: "BIN",
    GeoDatabaseFormat.GEONAMES: "geonames",
}


class BlobMissingError(LookupError):
    """The source has no file at the checksum asked for: a 404, a row without its bytes, or bytes that differ."""


class BlobSource(Protocol):
    """Where a stored database's bytes come from when this process has no copy at its checksum."""

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        """Put the file with checksum ``sha256`` at ``path``, atomically.

        Raises :class:`BlobMissingError` when the source has no such file,
        and anything else when it cannot be reached; the store tries again
        later either way.
        """
        ...


class PostgresBlobSource:
    """Stored bytes straight from ``geo_database_blobs``, for the roles that have Postgres."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        """Read the stored bytes, check them against ``sha256`` and write them to ``path``."""
        async with self._session_factory() as session:
            blob = await GeoDatabaseRepository(session).get_blob(database_id)
        if blob is None:
            raise BlobMissingError("stored file is missing")
        if await asyncio.to_thread(_digest_of, blob) != sha256:
            raise BlobMissingError("stored file does not have the requested checksum")
        await asyncio.to_thread(atomic_write, path, blob, mode=0o600)


class HttpBlobSource:
    """Stored bytes from the api's internal endpoint, for a gateway, which has no Postgres.

    The body streams to a temporary file, hashed on the way, and lands only whole at the checksum asked for.

    Args:
        token: The internal token (``api.feed.codec.internal_token``), never logged.
    """

    def __init__(self, internal_api_url: str, token: str) -> None:
        self._base_url = internal_api_url.rstrip("/")
        self._token = token

    @classmethod
    def from_settings(cls, settings: Settings) -> HttpBlobSource:
        """A gateway's source: the api at ``internal_api_url``, with the token its internal secret derives."""
        return cls(settings.internal_api_url, internal_token(settings.internal_secret))

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        """Download the file at ``sha256`` to ``path``; a 404 is :class:`BlobMissingError`."""
        url = f"{self._base_url}/internal/v1/geo/databases/{database_id}/blob"
        fd, partial = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".part")
        hasher = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async with (
                    # The api is inside the cluster: an HTTP(S)_PROXY in the environment is not for it.
                    httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False) as client,
                    client.stream(
                        "GET", url, params={"sha256": sha256}, headers={"Authorization": f"Bearer {self._token}"}
                    ) as response,
                ):
                    if response.status_code == 404:
                        raise BlobMissingError("the api has no file at the requested checksum")
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                        received += len(chunk)
                        if received > MAX_BLOB_BYTES:
                            raise ValueError("the file is larger than any stored database can be")
                        await asyncio.to_thread(_append, handle, hasher, chunk)
                await asyncio.to_thread(_sync_file, handle)
            if hasher.hexdigest() != sha256:
                raise BlobMissingError("the file received does not have the requested checksum")
            os.chmod(partial, 0o600)
            os.replace(partial, path)
            partial = ""
        finally:
            if partial:
                with contextlib.suppress(OSError):
                    os.unlink(partial)


def _digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _append(handle: IO[bytes], hasher: hashlib._Hash, chunk: bytes) -> None:
    hasher.update(chunk)
    handle.write(chunk)


def _sync_file(handle: IO[bytes]) -> None:
    handle.flush()
    os.fsync(handle.fileno())


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


@dataclass(frozen=True)
class _Failure:
    """Why a database is not open, at which checksum, and whether trying again can help."""

    sha256: str
    reason: str
    # When it was last tried (time.monotonic()).
    at: float
    # A file that could not be had is tried again; one that was had and
    # failed to open is not, until its checksum changes.
    retryable: bool


def path_database_id(path: str) -> str:
    """Stable id for an operator-managed file: derived from its path, not its content."""
    return "path-" + hashlib.sha1(path.encode()).hexdigest()[:16]


class GeoDatabaseStore:
    """The databases this process has open, and how they got here.

    Args:
        session_factory: Postgres, for :meth:`sync_all`; None on a gateway and in echo.
        blob_source: Where a missing copy comes from; None in echo, which has config files only.
        config_check_interval: Seconds the configured files go without a look on disk before :meth:`wait_missing` asks for one.
    """

    def __init__(
        self,
        session_factory: SessionFactory | None,
        cache_dir: str | Path,
        config_databases: list[dict[str, object]] | None = None,
        *,
        blob_source: BlobSource | None = None,
        config_check_interval: float = CONFIG_CHECK_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._cache_dir = Path(cache_dir)
        self._config_databases = list(config_databases or [])
        self._blob_source = blob_source
        self._config_check_interval = config_check_interval
        self._entries: dict[str, _Entry] = {}
        # The stored databases as last listed (Postgres) or received (feed), enabled or not.
        self._records: dict[str, GeoDatabaseRecord] = {}
        self._failures: dict[str, _Failure] = {}
        self._lock = asyncio.Lock()
        # Set when a file could not be had, which wakes wait_missing(); the
        # waiter clears it before it waits, so a stale set wakes nobody.
        self._missing = asyncio.Event()
        # When the configured files were last looked at on disk (time.monotonic()),
        # by any sync or retry; from construction, since the process's first
        # read looks at them anyway.
        self._config_checked_at = time.monotonic()
        # Config-file databases are re-inspected (hashed) only when the file's
        # size or mtime moved; every sync and retry looks (a stat per file).
        # None caches a file that failed inspection at that size and mtime.
        self._path_cache: dict[str, tuple[tuple[int, float], GeoDatabaseRecord | None]] = {}

    @classmethod
    def echo(cls, settings: Settings) -> GeoDatabaseStore:
        """Echo's databases: the configured files alone, with no Postgres, no Redis and nothing to fetch."""
        return cls(None, settings.geo_cache_dir, settings.geo_databases)

    # --- state ---------------------------------------------------------------------

    @property
    def loaded(self) -> list[LoadedDatabase]:
        """Open databases in lookup order."""
        return [e.loaded() for e in self._ordered()]

    @property
    def load_errors(self) -> dict[str, str]:
        """Database id to the reason it is not open in this process."""
        return {database_id: failure.reason for database_id, failure in self._failures.items()}

    def is_loaded(self, database_id: str) -> bool:
        return database_id in self._entries

    def is_loaded_from_config(self, database_id: str) -> bool:
        """Whether ``database_id`` is open here as a config-file database, which only the config file removes."""
        entry = self._entries.get(database_id)
        return entry is not None and entry.record.source == GeoDatabaseSource.PATH

    def missing(self) -> list[str]:
        """Enabled stored databases this process has not opened and can still hope to, sorted: a gateway is ready once none is left.

        A file that fails to open, or none stored yet, does not count: waiting for it would keep a gateway out for nothing.
        """
        return sorted(
            database_id
            for database_id, record in self._records.items()
            if record.enabled
            and database_id not in self._entries
            and ((failure := self._failures.get(database_id)) is None or failure.retryable)
        )

    async def wait_missing(self) -> None:
        """Return once there is a file to look for again: at once while one could not be had, else when the configured ones are due.

        The caller then waits ``GEO_BLOB_RETRY_SECONDS`` and calls :meth:`retry_missing`.
        """
        while not any(failure.retryable for failure in self._failures.values()):
            due = self._config_check_due()
            if due is not None and due <= 0:
                return
            self._missing.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(due):
                    await self._missing.wait()

    def _config_check_due(self) -> float | None:
        """Seconds until the configured files are due a look on disk (none left: due); None when there are none."""
        if not self._config_databases:
            return None
        return self._config_checked_at + self._config_check_interval - time.monotonic()

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
            if not entry.record.enabled or isinstance(entry.reader, GeoNamesReader):
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

    @property
    def places_loaded(self) -> bool:
        """Whether an enabled GeoNames file is open, so city claims can be judged by distance."""
        return any(e.record.enabled and isinstance(e.reader, GeoNamesReader) for e in self._entries.values())

    def locate_city(self, country: str | None, state: str | None, city: str | None) -> Point | None:
        """Where ``city`` (a slug) in ``country`` is, from the first enabled GeoNames file that knows it."""
        for entry in self._ordered():
            if entry.record.enabled and isinstance(entry.reader, GeoNamesReader):
                point = entry.reader.locate(country, state, city)
                if point is not None:
                    return point
        return None

    # --- sync ----------------------------------------------------------------------

    async def sync_all(self) -> int:
        """Open every enabled database Postgres lists and the config names, and close the rest; how many are open.

        When the rows cannot be listed the stored databases stay as they are: a Postgres blip must not blind attribution.
        """
        async with self._lock:
            stored: list[GeoDatabaseRecord] | None = None
            if self._session_factory is not None:
                try:
                    async with self._session_factory() as session:
                        stored = await GeoDatabaseRepository(session).get_all()
                except Exception as exc:
                    logger.warning("Could not list IP databases", error=str(exc))
            await self._sync(stored)
            return len(self._entries)

    async def sync_records(self, records: Sequence[GeoDatabaseFeed]) -> None:
        """A gateway's sync: the stored databases become exactly ``records``, the feed's global manifest, and the configured ones stay.

        Files missing here are fetched before this returns, and one that cannot be had is left to :meth:`retry_missing`.
        """
        stored = [
            GeoDatabaseRecord.model_validate(entry.model_dump())
            for entry in records
            if entry.source != GeoDatabaseSource.PATH
        ]
        async with self._lock:
            await self._sync(stored)

    async def retry_missing(self) -> int:
        """Try again what this process should have open and has not, and reopen a configured file that changed; how many opened.

        Each database is tried at most every ``GEO_BLOB_RETRY_SECONDS``, one that failed to open not until its checksum changes.
        """
        async with self._lock:
            return await self._sync(None)

    async def apply_record(self, record: GeoDatabaseRecord, blob: bytes | None = None) -> None:
        """Open a record this process just wrote (an upload, a download), without reading it back from Postgres."""
        async with self._lock:
            self._records[record.id] = record
            self._failures.pop(record.id, None)
            if not record.enabled:
                self._close(record.id)
                return
            if blob is not None:
                path = self._cache_path(record)
                await asyncio.to_thread(atomic_write, path, blob, mode=0o600)
            await self._ensure_open(record)

    def remove(self, database_id: str) -> None:
        """Close and forget a stored database this process just deleted."""
        self._records.pop(database_id, None)
        self._close(database_id)

    def close_all(self) -> None:
        for database_id in list(self._entries):
            self._close(database_id)

    async def blob_file(self, database_id: str, sha256: str) -> Path | None:
        """This process's copy of a stored database at ``sha256``, fetched from the blob source if needed; None when there is none.

        What the api serves gateways: another checksum gets a 404, and the gateway asks again after its next sync.
        """
        record = self._records.get(database_id)
        if record is None or not sha256 or record.sha256 != sha256:
            return None
        path = self._cache_path(record)
        entry = self._entries.get(database_id)
        # A copy this process opened was checked when it was written or opened: served without hashing it again.
        if entry is not None and entry.record.sha256 == sha256 and path.exists():
            return path
        if await self._cached_matches(path, sha256):
            return path
        if self._blob_source is None:
            return None
        try:
            await self._blob_source.fetch(database_id, sha256, path)
        except Exception as exc:
            logger.warning("Could not fetch IP database bytes", database_id=database_id, error=error_text(exc))
            return None
        return path

    # --- helpers -------------------------------------------------------------------

    async def _sync(self, stored: list[GeoDatabaseRecord] | None) -> int:
        """Bring the open databases in line with ``stored`` (None: as last known) and the config; returns how many opened.

        Called with the lock held.
        """
        if stored is not None:
            self._records = {record.id: record for record in stored if record.source != GeoDatabaseSource.PATH}
        configured = await self._config_records()
        wanted = {**self._records, **{record.id: record for record in configured}}
        closed = 0
        for database_id in list(self._entries):
            record = wanted.get(database_id)
            if record is None or not record.enabled:
                # Gone from the list or the config, or disabled.
                self._close(database_id)
                closed += 1
        # A failure is news only while the database is wanted; a configured
        # file that cannot be read has no record, and keeps its failure.
        configured_ids = {path_database_id(str(item.get("path") or "").strip()) for item in self._config_databases}
        for database_id in list(self._failures):
            record = wanted.get(database_id)
            if (record is None and database_id not in configured_ids) or (record is not None and not record.enabled):
                del self._failures[database_id]
        opened = 0
        for record in wanted.values():
            if record.enabled and await self._ensure_open(record):
                opened += 1
        if opened or closed:
            logger.info(
                "IP databases synced", loaded=len(self._entries), opened=opened, closed=closed,
                errors=len(self._failures),
            )
        return opened

    async def _config_records(self) -> list[GeoDatabaseRecord]:
        """The configured files as records, each looked at on disk now and inspected again only when it moved."""
        self._config_checked_at = time.monotonic()
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
                self._path_cache.pop(path, None)
                if self._fail(database_id, "", str(exc), retryable=True):
                    logger.warning("Configured IP database unreadable", path=path, error=str(exc))
                continue
            cached = self._path_cache.get(path)
            if cached is not None and cached[0] == stamp:
                if cached[1] is not None:
                    records.append(cached[1])
                continue
            try:
                # Off the event loop: hashing an mmdb takes a moment, parsing a GeoNames table seconds.
                info = await asyncio.to_thread(inspect_file, path)
            except GeoDatabaseError as exc:
                # Not inspected again until the file changes.
                self._path_cache[path] = (stamp, None)
                self._fail(database_id, "", str(exc), retryable=False)
                logger.warning("Configured IP database unreadable", path=path, error=str(exc))
                continue
            record = GeoDatabaseRecord(
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
            )
            self._path_cache[path] = (stamp, record)
            records.append(record)
        return records

    def _cache_path(self, record: GeoDatabaseRecord) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"{record.id}.{_EXTENSIONS.get(record.format, 'db')}"

    def _due(self, record: GeoDatabaseRecord) -> bool:
        """Whether ``record`` may be tried now: never failed at this checksum, or failed retryably long enough ago."""
        failure = self._failures.get(record.id)
        if failure is None or failure.sha256 != record.sha256:
            return True
        return failure.retryable and time.monotonic() - failure.at >= GEO_BLOB_RETRY_SECONDS

    def _fail(self, database_id: str, sha256: str, reason: str, *, retryable: bool) -> bool:
        """Remember why a database is not open; True when that is news worth a warning (a new failure or reason).

        A file that could not be had wakes :meth:`wait_missing`.
        """
        previous = self._failures.get(database_id)
        self._failures[database_id] = _Failure(sha256=sha256, reason=reason, at=time.monotonic(), retryable=retryable)
        if retryable:
            self._missing.set()
        return previous is None or previous.reason != reason or previous.sha256 != sha256

    async def _ensure_open(self, record: GeoDatabaseRecord) -> bool:
        """Open ``record`` unless it is open at this checksum already or not due; True when it opened."""
        current = self._entries.get(record.id)
        if current is not None and current.record.sha256 == record.sha256 and current.record.sha256:
            # Same file; only the row's settings may have moved (name, priority).
            current.record = record
            return False
        if not self._due(record):
            return False
        if record.source == GeoDatabaseSource.PATH:
            path = Path(record.path or "")
        else:
            if not record.sha256:
                # A download that never succeeded leaves a row without a file; nothing to wait for.
                self._fail(record.id, "", "no file stored yet", retryable=False)
                return False
            path = self._cache_path(record)
            if not await self._cached_matches(path, record.sha256) and not await self._fetch(record, path):
                return False
        try:
            reader = await asyncio.to_thread(open_reader, path, record.format)
        except GeoDatabaseError as exc:
            self._fail(record.id, record.sha256, str(exc), retryable=False)
            logger.error("IP database failed to open", database_id=record.id, path=str(path), error=str(exc))
            return False
        self._close(record.id)
        self._entries[record.id] = _Entry(record=record, reader=reader, path=path)
        logger.info(
            "IP database loaded",
            database_id=record.id,
            name=record.name,
            vendor=record.vendor.value,
            kind=record.kind.value,
            path=str(path),
        )
        return True

    async def _cached_matches(self, path: Path, sha256: str) -> bool:
        if not sha256 or not path.exists():
            return False

        def digest() -> str:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        return await asyncio.to_thread(digest) == sha256

    async def _fetch(self, record: GeoDatabaseRecord, path: Path) -> bool:
        """Get the file of ``record`` from the blob source; False, with the failure remembered, when it cannot."""
        if self._blob_source is None:
            self._fail(record.id, record.sha256, "no source to fetch the file from", retryable=False)
            return False
        try:
            await self._blob_source.fetch(record.id, record.sha256, path)
        except BlobMissingError as exc:
            if self._fail(record.id, record.sha256, str(exc), retryable=True):
                logger.warning("IP database file not available", database_id=record.id, error=error_text(exc))
            return False
        except Exception as exc:
            if self._fail(record.id, record.sha256, f"fetch failed: {exc}", retryable=True):
                logger.warning("Could not fetch IP database bytes", database_id=record.id, error=str(exc))
            return False
        logger.info("IP database cached", database_id=record.id, path=str(path))
        return True

    def _close(self, database_id: str) -> None:
        entry = self._entries.pop(database_id, None)
        if entry is not None:
            with contextlib.suppress(Exception):
                entry.reader.close()
        self._failures.pop(database_id, None)


class DatabaseSync(Loop):
    """A gateway's IP databases brought to the list its routing feed carries, one list at a time, as each apply wakes it.

    A list stays offered until a sync of it ends well, so one that failed is synced again after the back-off unless a
    newer list replaced it; the files a sync could not have, :class:`DatabaseRetry` tries again.
    """

    def __init__(self, store: GeoDatabaseStore) -> None:
        super().__init__(name=WorkerName.GEO_DATABASE_SYNC)
        self._store = store
        # The list offered last, until a sync of it ends well; None for none.
        self._offered: list[GeoDatabaseFeed] | None = None

    def offer(self, listed: list[GeoDatabaseFeed]) -> None:
        """Sync the store to ``listed`` next, in place of any list offered before it."""
        self._offered = listed
        self.wake()

    def missing(self) -> list[str]:
        """The IP databases the gateway waits for, sorted: the store's own account, unless a list is offered.

        Then every enabled database of that list that the store has not opened counts, so readiness never latches on a
        list the store has not taken yet.
        """
        listed = self._offered
        if listed is None:
            return self._store.missing()
        return sorted(entry.id for entry in listed if entry.enabled and not self._store.is_loaded(entry.id))

    async def _cycle(self) -> bool:
        """Sync the store to the list offered last; whether there was one. A sync that fails raises: a failed run."""
        listed = self._offered
        if listed is None:
            return False
        await self._store.sync_records(listed)
        if self._offered is listed:
            self._offered = None
        return True


class DatabaseRetry(Loop):
    """Tries again every ``GEO_BLOB_RETRY_SECONDS`` the IP database files a store lacks and can still hope to have.

    One per process with IP databases, run by its node; it is idle while the store lacks nothing.
    """

    def __init__(self, store: GeoDatabaseStore) -> None:
        super().__init__(name=WorkerName.GEO_DATABASE_RETRY)
        self._store = store

    async def _wait(self) -> None:
        """Until the store has a file to look for again, then the retry spacing: what is missing was just tried."""
        await self._store.wait_missing()
        await asyncio.sleep(GEO_BLOB_RETRY_SECONDS)

    async def _cycle(self) -> bool:
        """Try again what the store lacks; whether a file opened."""
        return await self._store.retry_missing() > 0

    def _cadence(self) -> float:
        """The retry spacing."""
        return GEO_BLOB_RETRY_SECONDS
