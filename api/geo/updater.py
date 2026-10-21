# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Downloads of IP databases from their vendors: a worker's ``geo_update`` job ticks the schedule, and the api refreshes by hand.

A file is stored only when its checksum moved, and download URLs pass the egress guard descriptor API calls pass.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from api.core import utc_now
from api.db.geo_repository import GeoDatabaseRepository, reclaim_blob_space
from api.db.session import SessionFactory
from api.db.streams import ChangeKind, ChangeLog
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.readers import GeoDatabaseError, inspect_file
from api.geo.store import MAX_BLOB_BYTES, GeoDatabaseStore
from api.providers.sdk.egress import EgressDeniedError, EgressGuard, EgressPolicy

logger = structlog.get_logger()
DOWNLOAD_TIMEOUT = 600.0
# What an admin edits on a row. A refresh holds the row as it was read before a
# download that can run for minutes, so it takes these from the row as it is
# when it writes: putting back what it read would undo a Pause clicked meanwhile.
_ADMIN_FIELDS = (
    "name",
    "source",
    "enabled",
    "priority",
    "update_url",
    "update_interval_hours",
    "update_auth",
    "update_paused",
)

# Runs after a refresh stored a new file of an enabled database: the worker
# and the api ask for a re-attribution of every proxy.
AfterUpdate = Callable[[], Awaitable[object]]


class DownloadError(Exception):
    """The vendor download failed or did not yield a database."""


def unpack(data: bytes, url: str) -> bytes:
    """Return the database bytes inside whatever container the vendor used: MaxMind ships ``.tar.gz``, DB-IP ``.mmdb.gz``, IPinfo a bare ``.mmdb``."""
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


class _NotPublishedError(DownloadError):
    """The vendor answered 404: the file at this URL does not exist (yet)."""


# Placeholders an update URL may carry, filled in when the download runs.
# DB-IP names its monthly files by month and offers no "latest" alias.
URL_PLACEHOLDERS = ("{YYYY}", "{MM}")


def expand_update_url(template: str, when: datetime) -> str:
    """Fill ``{YYYY}`` and ``{MM}`` with the given month."""
    return template.replace("{YYYY}", f"{when.year:04d}").replace("{MM}", f"{when.month:02d}")


def candidate_urls(template: str, now: datetime | None = None) -> list[str]:
    """The URLs to try, in order: this month's, then last month's while the vendor has not published yet.

    A URL without placeholders is tried as it is, once.
    """
    if not any(p in template for p in URL_PLACEHOLDERS):
        return [template]
    now = now or utc_now()
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = first_of_month - timedelta(days=1)
    urls = [expand_update_url(template, now), expand_update_url(template, previous)]
    return list(dict.fromkeys(urls))


async def download(record: GeoDatabaseRecord, guard: EgressGuard) -> bytes:
    """Fetch and unpack ``record.update_url``, raising DownloadError.

    Date placeholders take this month, and on a 404 the previous month, since a vendor publishes on a day of its own;
    any other error stops at the first URL.
    """
    if not record.update_url:
        raise DownloadError("no update URL")
    last_error: DownloadError | None = None
    for url in candidate_urls(record.update_url):
        try:
            return await _download_url(url, record, guard)
        except _NotPublishedError as exc:
            last_error = exc
            # Logged by id, not URL: vendors put their tokens in it (IPinfo's ?token=).
            logger.info(
                "IP database not published at this URL, trying the previous month",
                database_id=record.id,
                name=record.name,
            )
    assert last_error is not None
    raise last_error


async def _download_url(url: str, record: GeoDatabaseRecord, guard: EgressGuard) -> bytes:
    """Fetch and unpack one concrete URL, following the redirects to object storage by hand.

    Every hop passes the egress guard and connects to its own pinned address with its own Host and SNI, and the vendor's
    credentials stay with the vendor's host, which httpx following on its own would not do.
    """
    auth, vendor_headers = _auth(record)
    origin_host = httpx.URL(url).host
    requested = url
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
                    if response.status_code == 404:
                        raise _NotPublishedError("vendor returned HTTP 404")
                    if response.status_code != 200:
                        raise DownloadError(f"vendor returned HTTP {response.status_code}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_BLOB_BYTES:
                            raise DownloadError("download exceeds the size limit")
                        chunks.append(chunk)
                    break
            else:
                raise DownloadError(f"more than {MAX_REDIRECTS} redirects")
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed: {exc}") from exc
    try:
        return unpack(b"".join(chunks), requested)
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


def next_update_at(record: GeoDatabaseRecord) -> datetime | None:
    """When ``record``'s schedule is next due, from its creation if it never ran; None when nothing downloads it on its own.

    The ``geo_update`` job looks once per ``geo_updater_interval``, so the download runs at its first look after this
    time; a paused or disabled database is downloaded by hand only.
    """
    if (
        record.source != GeoDatabaseSource.URL
        or not record.update_url
        or record.update_interval_hours <= 0
        or record.update_paused
        or not record.enabled
    ):
        return None
    if record.last_update_at is None:
        return record.created_at
    return record.last_update_at + timedelta(hours=record.update_interval_hours)


class GeoDatabaseUpdater:
    """Downloads databases from their vendors: the ``geo_update`` job's tick on a worker, and the api's refresh by hand.

    Args:
        after_update: run once a refresh stored a new file of an enabled database; None for nothing.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        store: GeoDatabaseStore,
        *,
        changes: ChangeLog,
        after_update: AfterUpdate | None = None,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._changes = changes
        self._after_update = after_update
        self._guard = EgressGuard(egress_policy)

    async def tick(self) -> None:
        """Give back the disk replaced files hold, then refresh every database whose schedule is due.

        Due times come from the rows, so a tick catches up whatever fell due since the last one ran anywhere, and a
        tick right after it finds nothing due.
        """
        await self._sweep_storage()
        async with self._session_factory() as session:
            records = await GeoDatabaseRepository(session).get_all()
        now = utc_now()
        for record in records:
            if (at := next_update_at(record)) is not None and at <= now:
                await self.refresh(record)

    async def _sweep_storage(self) -> None:
        """Give back the disk replaced and removed files still hold (see geo_repository); a failure is logged."""
        try:
            async with self._session_factory() as session:
                conn = await session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
                await reclaim_blob_space(conn)
        except Exception as exc:
            logger.warning("Could not reclaim IP database storage", error=str(exc))

    async def refresh(self, record: GeoDatabaseRecord) -> GeoDatabaseRecord:
        """Download ``record`` now and store it if it changed, any error landing on the row.

        A new file opens in this process at once, and in every other once the ``geo_database`` entry after the commit
        reaches it: the api and the workers read the list again, and gateways get it through the feed.
        """
        try:
            data = await download(record, self._guard)
            info = await asyncio.to_thread(validate_bytes, data, ".BIN" if record.format.value.endswith("bin") else ".mmdb")
        except (DownloadError, GeoDatabaseError) as exc:
            record.last_update_error = str(exc)
            record.last_update_at = utc_now()
            await self._save(record)
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
        await self._save(record, None if unchanged else data)
        if unchanged:
            logger.info("IP database is current", database_id=record.id, name=record.name)
            return record
        await self._store.apply_record(record, data)
        logger.info(
            "IP database updated", database_id=record.id, name=record.name, build=str(record.build_epoch), bytes=len(data)
        )
        if self._after_update is not None and record.enabled:
            # A new build moves ranges; verdicts computed with the old one are
            # stale until every proxy is re-judged, as the admin routes do. A
            # disabled file is consulted by nothing, so nothing is stale.
            try:
                await self._after_update()
            except Exception as exc:
                logger.warning("Re-attribution after database update failed", database_id=record.id, error=str(exc))
        return record

    async def _save(self, record: GeoDatabaseRecord, blob: bytes | None = None) -> None:
        """Write a refresh's outcome onto the row as it is now, keeping what an admin changed meanwhile, then announce it.

        The entry goes after the commit, so a process reading on it finds the row written; one Redis does not take is
        sent again by the change log, never failing the write.
        """
        async with self._session_factory() as session:
            repo = GeoDatabaseRepository(session)
            current = await repo.get_by_id(record.id)
            if current is not None:
                for name in _ADMIN_FIELDS:
                    setattr(record, name, getattr(current, name))
            await repo.update(record, blob=blob)
            await session.commit()
        await self._changes.record(ChangeKind.GEO_DATABASE, record.id)
