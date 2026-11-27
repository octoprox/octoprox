# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The IP databases as admins manage them: stored rows and config-file databases, with where each is open.

A write that changes how exits are judged asks for every proxy to be re-judged; download URLs and credentials are never
shown, since anyone signed in may list the databases.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import structlog

from api.db.geo_repository import GeoDatabaseRepository
from api.db.streams import ChangeKind
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.readers import GeoDatabaseError
from api.geo.updater import DownloadError, next_update_at, validate_bytes
from api.models.geo import (
    GeoDatabaseFromUrl,
    GeoDatabaseListResponse,
    GeoDatabaseResponse,
    GeoDatabaseUpdate,
)
from api.services.errors import InvalidConfigError, NotFoundError, UpstreamError, WriteRejectedError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.geo.reattribution import ReattributionService
    from api.geo.store import GeoDatabaseStore
    from api.geo.updater import GeoDatabaseUpdater
    from api.models.system import GatewaySnapshot, GatewaySnapshots

logger = structlog.get_logger()

# What a masked part of a download URL reads as in an answer (``_shown_url``).
_MASKED = "***"


class GeoDatabaseService:
    """Lists, uploads, registers, edits, downloads and removes the IP databases, and inspects a file without storing it.

    Args:
        gateways: Every live gateway's heartbeat, read at call time; what each write answers and the list show.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        store: GeoDatabaseStore,
        updater: GeoDatabaseUpdater,
        gateways: GatewaySnapshots,
        changes: ChangeLog,
        reattribution: ReattributionService,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._updater = updater
        self._gateways = gateways
        self._changes = changes
        self._reattribution = reattribution

    async def every(self) -> GeoDatabaseListResponse:
        """Every database: stored rows plus operator-managed files from the config, with where each is open."""
        async with self._session_factory() as session:
            repository = GeoDatabaseRepository(session)
            records = await repository.get_all()
            stored_ids = await repository.blob_ids()
        gateways = await self._gateways()
        rows = [self._shown(r, gateways, stored_ids) for r in records]
        rows.extend(self._config_rows(gateways, {r.id for r in records}))
        rows.sort(key=lambda r: (r.priority, r.created_at))
        return GeoDatabaseListResponse(databases=rows)

    async def upload(
        self,
        raw: bytes,
        filename: str | None,
        name: str | None,
        priority: int,
        enabled: bool,
        actor: Actor,
    ) -> GeoDatabaseResponse:
        """Store an mmdb, IP2Location BIN or GeoNames file, refused when it does not open. An enabled one opens here
        at once, reaches the other processes with its change entry, and every proxy is re-judged with it."""
        if not raw:
            raise WriteRejectedError("Empty file")
        suffix = Path(filename or "").suffix or ".mmdb"
        try:
            info = await asyncio.to_thread(validate_bytes, raw, suffix)
        except GeoDatabaseError as exc:
            raise InvalidConfigError(f"Not a readable IP database: {exc}") from None

        record = _record_from_info(
            info,
            name=(name or "").strip() or Path(filename or "database").stem,
            priority=priority,
            enabled=enabled,
            uploaded_by=actor.name,
        )
        async with self._session_factory() as session:
            await GeoDatabaseRepository(session).create(record, raw)
            await actor.created(session, record)
            await session.commit()
        await self._changes.record(ChangeKind.GEO_DATABASE, record.id)
        await self._store.apply_record(record, raw)
        logger.info(
            "IP database uploaded",
            database_id=record.id,
            name=record.name,
            vendor=record.vendor.value,
            kind=record.kind.value,
            bytes=len(raw),
            admin=actor.name,
        )
        # A disabled database is consulted by nothing, so no verdict is stale.
        if record.enabled:
            await self._reattribution.request_all("database added")
        return await self._row(record)

    async def add_from_url(self, data: GeoDatabaseFromUrl, actor: Actor) -> GeoDatabaseResponse:
        """Register a vendor download URL and fetch it now; the row is announced first, so a failed download leaves
        it listed with its error, and the download's write and the re-judging are the updater's."""
        record = GeoDatabaseRecord(
            name=data.name.strip(),
            source=GeoDatabaseSource.URL,
            enabled=data.enabled,
            priority=data.priority,
            update_url=data.update_url.strip(),
            update_interval_hours=data.update_interval_hours,
            update_auth=data.update_auth,
            uploaded_by=actor.name,
        )
        async with self._session_factory() as session:
            await GeoDatabaseRepository(session).create(record, None)
            await actor.created(session, record)
            await session.commit()
        await self._changes.record(ChangeKind.GEO_DATABASE, record.id)
        refreshed = await self._updater.refresh(record)
        if refreshed.last_update_error:
            raise WriteRejectedError(f"Download failed: {refreshed.last_update_error}")
        logger.info(
            "IP database registered from URL",
            database_id=record.id,
            name=record.name,
            admin=actor.name,
        )
        return await self._row(refreshed)

    async def update(
        self, database_id: str, data: GeoDatabaseUpdate, actor: Actor
    ) -> GeoDatabaseResponse:
        """Change a database's name, priority, enabled flag or download schedule; switching one on or off, or moving
        an enabled one, re-judges every proxy, and a download URL sent back masked keeps the stored one."""
        async with self._session_factory() as session:
            repo = GeoDatabaseRepository(session)
            record = await repo.get_by_id(database_id)
            if record is None:
                raise NotFoundError("Database not found")
            # The record is changed in place below; its audit entry compares with this copy.
            before = record.model_copy(deep=True)
            if data.name is not None:
                record.name = data.name.strip()
            if data.enabled is not None:
                record.enabled = data.enabled
            if data.priority is not None:
                record.priority = data.priority
            if data.update_url is not None and data.update_url.strip() != _shown_url(
                record.update_url
            ):
                record.update_url = data.update_url.strip() or None
                if record.update_url and record.source == GeoDatabaseSource.UPLOAD:
                    record.source = GeoDatabaseSource.URL
            if data.update_interval_hours is not None:
                record.update_interval_hours = data.update_interval_hours
            if data.update_auth is not None:
                record.update_auth = data.update_auth
            if data.update_paused is not None:
                record.update_paused = data.update_paused
            await repo.update(record)
            await actor.updated(session, before, record)
            await session.commit()
        await self._changes.record(ChangeKind.GEO_DATABASE, database_id)
        await self._store.apply_record(record)
        if record.enabled != before.enabled or (
            record.enabled and record.priority != before.priority
        ):
            await self._reattribution.request_all("database order changed")
        return await self._row(record)

    async def refresh(self, database_id: str) -> GeoDatabaseResponse:
        """Download a scheduled database now, paused or disabled as it may be, once the download is done. A new file
        of an enabled one opens here and re-judges every proxy; the updater writes it, recording no History."""
        # A short session of its own: no connection is held for the download.
        async with self._session_factory() as session:
            record = await GeoDatabaseRepository(session).get_by_id(database_id)
        if record is None:
            raise NotFoundError("Database not found")
        if not record.update_url:
            raise WriteRejectedError("This database has no download URL")
        try:
            refreshed = await self._updater.refresh(record)
        except DownloadError as exc:
            raise UpstreamError(str(exc)) from None
        if refreshed.last_update_error:
            raise UpstreamError(refreshed.last_update_error)
        return await self._row(refreshed)

    async def delete(self, database_id: str, actor: Actor) -> None:
        """Remove a stored database everywhere. Config-file databases cannot be removed here."""
        async with self._session_factory() as session:
            repo = GeoDatabaseRepository(session)
            record = await repo.get_by_id(database_id)
            if record is None:
                if self._store.is_loaded_from_config(database_id):
                    raise WriteRejectedError(
                        "Operator-managed databases are removed from the config file"
                    )
                raise NotFoundError("Database not found")
            # A delete that lost a race with another finds the row gone: that one records it.
            if await repo.delete(database_id):
                await actor.deleted(session, record)
            await session.commit()
        await self._changes.record(ChangeKind.GEO_DATABASE, database_id)
        self._store.remove(database_id)
        logger.info(
            "IP database deleted", database_id=database_id, name=record.name, admin=actor.name
        )
        if record.enabled:
            await self._reattribution.request_all("database removed")

    async def inspect(self, raw: bytes, filename: str | None) -> dict[str, Any]:
        """Open a file and report what it is, without storing it."""
        suffix = Path(filename or "").suffix or ".mmdb"
        try:
            info = await asyncio.to_thread(validate_bytes, raw, suffix)
        except GeoDatabaseError as exc:
            raise InvalidConfigError(str(exc)) from None
        return {
            "format": info.format.value,
            "database_type": info.database_type,
            "vendor": info.vendor.value,
            "kind": info.kind.value,
            "build_epoch": info.build_epoch,
            "record_count": info.record_count,
            "ip_version": info.ip_version,
            "languages": info.languages,
            "description": info.description,
            "size_bytes": info.size_bytes,
            "sha256": info.sha256,
            "attribution": info.attribution,
        }

    async def _row(self, record: GeoDatabaseRecord) -> GeoDatabaseResponse:
        """The answer to a write of one database: the row as written, with where it is open now."""
        return self._shown(record, await self._gateways())

    def _shown(
        self,
        record: GeoDatabaseRecord,
        gateways: Mapping[str, GatewaySnapshot],
        stored_ids: set[str] | None = None,
    ) -> GeoDatabaseResponse:
        """One database row for the API, with where it is open; ``stored_ids`` names the rows whose bytes are stored,
        None right after a write that stored them."""
        # A backup restored without its files leaves rows with a checksum and no
        # bytes, to re-upload or refresh; right after a write that stored them the
        # checksum is proof enough, since a download never yet done has none.
        has_file = bool(record.sha256) if stored_ids is None else record.id in stored_ids
        return GeoDatabaseResponse(
            **record.model_dump(exclude={"update_auth", "update_url"}),
            update_url=_shown_url(record.update_url),
            has_update_auth=bool(record.update_auth),
            loaded_here=self._store.is_loaded(record.id),
            load_error=self._store.load_errors.get(record.id),
            has_file=has_file,
            next_update_at=next_update_at(record),
            gateways_loaded=sum(
                1 for gateway in gateways.values() if record.id in gateway.geo_databases_loaded
            ),
            gateways_total=len(gateways),
            gateway_errors={
                instance_id: gateway.geo_database_errors[record.id]
                for instance_id, gateway in gateways.items()
                if record.id in gateway.geo_database_errors
            },
        )

    def _config_rows(
        self, gateways: Mapping[str, GatewaySnapshot], known: set[str]
    ) -> list[GeoDatabaseResponse]:
        """Config-file databases, which have no row, shown from the store."""
        rows: list[GeoDatabaseResponse] = []
        for loaded in self._store.config_only(known):
            record = GeoDatabaseRecord(
                id=loaded.id,
                name=loaded.name,
                vendor=loaded.vendor,
                kind=loaded.kind,
                format=loaded.format,
                source=loaded.source,
                enabled=True,
                priority=loaded.priority,
                path=loaded.path,
                size_bytes=loaded.size_bytes,
                database_type=loaded.database_type,
                build_epoch=loaded.build_epoch,
                record_count=loaded.record_count,
            )
            # The file is the operator's, on disk: there are no stored bytes to miss.
            rows.append(self._shown(record, gateways, stored_ids={record.id}))
        return rows


def _shown_url(url: str | None) -> str | None:
    """A download URL as anyone signed in may see it, its user part and every query value masked where vendors take
    tokens; a PATCH that sends it back as shown keeps the stored URL."""
    # Vendors take a token there as often as in ``update_auth``: IPinfo's
    # ``?token=``, MaxMind's older ``license_key=``, a key as the URL's user.
    if not url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{_MASKED}@{netloc.rpartition('@')[2]}"
    query = "&".join(
        f"{name}={_MASKED if value else ''}" if equals else _MASKED
        for name, equals, value in (
            param.partition("=") for param in parts.query.split("&") if param
        )
    )
    fragment = _MASKED if parts.fragment else ""
    return urlunsplit(parts._replace(netloc=netloc, query=query, fragment=fragment))


def _record_from_info(
    info: Any, *, name: str, priority: int, enabled: bool, uploaded_by: str
) -> GeoDatabaseRecord:
    """A database row for an uploaded file, from what inspecting it found."""
    return GeoDatabaseRecord(
        name=name,
        vendor=info.vendor,
        kind=info.kind,
        format=info.format,
        source=GeoDatabaseSource.UPLOAD,
        enabled=enabled,
        priority=priority,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        database_type=info.database_type,
        build_epoch=info.build_epoch,
        record_count=info.record_count,
        ip_version=info.ip_version,
        languages=info.languages,
        description=info.description,
        attribution=info.attribution,
        uploaded_by=uploaded_by,
    )
