# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Scheduled downloads of IP databases from their vendors.

Leader-elected. Every tick the holder looks for stored databases with an
``update_url`` whose interval has elapsed, downloads each, unpacks the
container the vendor uses (MaxMind ships ``.tar.gz``, DB-IP ``.mmdb.gz``,
IPinfo a bare ``.mmdb``), validates the file by opening it, and only when the
checksum moved writes the new bytes to Postgres and tells every instance.

Download URLs are admin-configured, so they pass the same egress guard as
descriptor API calls: HTTPS only, no private addresses, DNS pinned.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.workers import LeaseName, WorkerName
from api.db.geo_repository import GeoDatabaseRepository
from api.db.redis import RedisClient
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.readers import GeoDatabaseError, inspect_file
from api.geo.store import GeoDatabaseStore, SessionFactory
from api.providers.sdk.egress import EgressDeniedError, EgressGuard, EgressPolicy

logger = structlog.get_logger()

_LEASE_RETRY_SECONDS = 60.0
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600.0

Publisher = Callable[[str, str], Awaitable[None]]
# Runs after a scheduled refresh stored a new file: the runtime re-attributes every proxy.
AfterUpdate = Callable[[], Awaitable[None]]


class DownloadError(Exception):
    """The vendor download failed or did not yield a database."""


def unpack(data: bytes, url: str) -> bytes:
    """Return the database bytes inside whatever container the vendor used."""
    lowered = url.lower().split("?", 1)[0]
    if data[:2] == b"\x1f\x8b":
        if lowered.endswith((".tar.gz", ".tgz")) or _looks_like_tar(data):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.isfile() and member.name.lower().endswith((".mmdb", ".bin")):
                        extracted = archive.extractfile(member)
                        if extracted is not None:
                            return extracted.read()
            raise DownloadError("archive contains no .mmdb or .BIN file")
        return gzip.decompress(data)
    return data


def _looks_like_tar(gz: bytes) -> bool:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(gz)) as handle:
            head = handle.read(512)
    except (OSError, EOFError):
        return False
    return len(head) == 512 and head[257:262] == b"ustar"


def _auth(record: GeoDatabaseRecord) -> tuple[httpx.Auth | None, dict[str, str]]:
    """Basic auth (MaxMind account id + license key) or a bearer/header token."""
    auth = record.update_auth or {}
    username = auth.get("username")
    password = auth.get("password")
    headers: dict[str, str] = {}
    if auth.get("header") and auth.get("token"):
        headers[str(auth["header"])] = str(auth["token"])
    elif auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    if username or password:
        return httpx.BasicAuth(str(username or ""), str(password or "")), headers
    return None, headers


MAX_REDIRECTS = 5


async def download(record: GeoDatabaseRecord, guard: EgressGuard) -> bytes:
    """Fetch and unpack ``record.update_url``. Raises DownloadError.

    Vendors hand the file off to object storage with a redirect (MaxMind
    answers 302 to R2), so redirects are followed here, by hand: every hop is
    vetted by the egress guard like the first, connects to its own pinned
    address with its own Host and SNI, and the vendor's credentials stay with
    the vendor's host. Letting httpx follow would reuse the first hop's Host
    and SNI against the storage host and skip the guard entirely.
    """
    if not record.update_url:
        raise DownloadError("no update URL")
    auth, vendor_headers = _auth(record)
    origin_host = httpx.URL(record.update_url).host
    url = record.update_url
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    target = await guard.resolve(url)
                except EgressDeniedError as exc:
                    raise DownloadError(f"download blocked by egress policy: {exc}") from exc
                same_origin = httpx.URL(url).host == origin_host
                headers = dict(vendor_headers) if same_origin else {}
                extensions: dict[str, Any] = {}
                if target.url.host != target.hostname:
                    headers["Host"] = target.hostname
                    extensions["sni_hostname"] = target.hostname
                async with client.stream(
                    "GET", target.url, headers=headers, extensions=extensions, auth=auth if same_origin else None
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError(f"vendor returned HTTP {response.status_code} without a location")
                        url = str(httpx.URL(url).join(location))
                        continue
                    if response.status_code != 200:
                        raise DownloadError(f"vendor returned HTTP {response.status_code}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise DownloadError("download exceeds the size limit")
                        chunks.append(chunk)
                    break
            else:
                raise DownloadError(f"more than {MAX_REDIRECTS} redirects")
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed: {exc}") from exc
    try:
        return unpack(b"".join(chunks), record.update_url)
    except (tarfile.TarError, gzip.BadGzipFile, OSError) as exc:
        raise DownloadError(f"could not unpack download: {exc}") from exc


def validate_bytes(data: bytes, suffix: str = ".mmdb") -> Any:
    """Open the bytes as a database (in a temp file) and return its DatabaseInfo."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        temp = Path(handle.name)
    try:
        return inspect_file(temp)
    finally:
        temp.unlink(missing_ok=True)


class GeoDatabaseUpdater:
    """Leader loop that refreshes scheduled databases."""

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        instance_id: str,
        store: GeoDatabaseStore,
        publish: Publisher,
        *,
        after_update: AfterUpdate | None = None,
        interval: float = 3600.0,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._instance_id = instance_id
        self._store = store
        self._publish = publish
        self._after_update = after_update
        self._interval = interval
        self._guard = EgressGuard(egress_policy)
        self._running = False

    async def run(self) -> None:
        self._running = True
        job_stats.declare_interval(WorkerName.GEO_DATABASE_UPDATER, self._interval)
        lease = Lease(self._redis, name=LeaseName.GEO_DATABASE_UPDATER, owner_id=self._instance_id)
        try:
            while self._running:
                try:
                    if not lease.is_held and not await lease.try_acquire():
                        await asyncio.sleep(_LEASE_RETRY_SECONDS)
                        continue
                    await asyncio.sleep(self._interval)
                    if lease.is_held and self._running:
                        with job_stats.track(WorkerName.GEO_DATABASE_UPDATER) as run:
                            if await self.tick() == 0:
                                run.idle()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("IP database updater error", error=str(exc))
        finally:
            await lease.release()

    async def tick(self) -> int:
        """Refresh every database whose schedule is due. Returns how many were attempted."""
        async with self._session_factory() as session:
            records = await GeoDatabaseRepository(session).get_all()
        due = [r for r in records if self._is_due(r)]
        for record in due:
            await self.refresh(record)
        return len(due)

    @staticmethod
    def _is_due(record: GeoDatabaseRecord) -> bool:
        if record.source != GeoDatabaseSource.URL or not record.update_url or record.update_interval_hours <= 0:
            return False
        if record.last_update_at is None:
            return True
        return utc_now() - record.last_update_at >= timedelta(hours=record.update_interval_hours)

    async def refresh(self, record: GeoDatabaseRecord) -> GeoDatabaseRecord:
        """Download ``record`` now and store it if it changed. Errors land on the row."""
        try:
            data = await download(record, self._guard)
            info = await asyncio.to_thread(validate_bytes, data, ".BIN" if record.format.value.endswith("bin") else ".mmdb")
        except (DownloadError, GeoDatabaseError) as exc:
            record.last_update_error = str(exc)
            record.last_update_at = utc_now()
            async with self._session_factory() as session:
                await GeoDatabaseRepository(session).update(record)
                await session.commit()
            logger.warning("IP database update failed", database_id=record.id, name=record.name, error=str(exc))
            return record

        record.last_update_error = None
        record.last_update_at = utc_now()
        unchanged = info.sha256 == record.sha256
        if not unchanged:
            record.sha256 = info.sha256
            record.size_bytes = info.size_bytes
            record.database_type = info.database_type
            record.build_epoch = info.build_epoch
            record.record_count = info.record_count
            record.ip_version = info.ip_version
            record.languages = info.languages
            record.description = info.description
            record.attribution = info.attribution
            record.vendor = info.vendor
            record.kind = info.kind
            record.format = info.format
        async with self._session_factory() as session:
            await GeoDatabaseRepository(session).update(record, blob=None if unchanged else data)
            await session.commit()
        if unchanged:
            logger.info("IP database is current", database_id=record.id, name=record.name)
            return record
        await self._store.apply_record(record, data)
        await self._publish(record.id, "updated")
        logger.info(
            "IP database updated", database_id=record.id, name=record.name, build=str(record.build_epoch), bytes=len(data)
        )
        if self._after_update is not None:
            # A new build moves ranges; verdicts computed with the old one are
            # stale until every proxy is re-judged, as the admin routes do.
            try:
                await self._after_update()
            except Exception as exc:
                logger.warning("Re-attribution after database update failed", database_id=record.id, error=str(exc))
        return record

    def stop(self) -> None:
        self._running = False
