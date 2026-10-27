# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repositories for IP attribution: databases, observations, aggregates and runtime settings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import structlog
from sqlalchemy import (
    Boolean,
    DateTime,
    Select,
    String,
    Table,
    and_,
    case,
    column,
    delete,
    func,
    literal_column,
    null,
    or_,
    select,
    text,
    tuple_,
    union_all,
    update,
    values,
)
from sqlalchemy import (
    cast as cast_sql,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from api.core import utc_now
from api.db.models import (
    ConnectorExitIpModel,
    ConnectorModel,
    GeoDatabaseBlobModel,
    GeoDatabaseModel,
    GeoSettingsModel,
    IpObservationModel,
)
from api.geo.models import (
    JUDGEMENT_SOURCE,
    ExitJudgement,
    GeoDatabaseFormat,
    GeoDatabaseKind,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    GeoVendor,
    IpObservation,
)

logger = structlog.get_logger()

# A page's total is counted no further than this past the page: enough to keep
# paging without meeting an end, and a short index read however large the
# history grows. The exact total is a separate count, asked for explicitly.
COUNT_CAP = 10_000
# An exact count under a filter no index serves reads the whole table, and a
# log page under a value with no recent rows reads all the newer history; past
# this either is called off rather than left to load the database.
READ_TIMEOUT_MS = 30_000
_QUERY_CANCELED = "57014"


def _verdict_clauses(m: type[IpObservationModel] | type[ConnectorExitIpModel], verdict: str | None) -> list[Any]:
    """The rows a verdict label selects: the country badge the tables show, from the country claim and its verdict.

    The state and city verdicts do not move it: an exit right at the country is usable there whatever its city.
    """
    if verdict == "contradicted":
        return [m.country_conflict.is_(True)]
    if verdict == "confirmed":
        return [m.country_conflict.is_(False)]
    if verdict == "open":
        return [m.claimed_country.is_not(None), m.country_conflict.is_(None)]
    if verdict == "no_claim":
        return [m.claimed_country.is_(None)]
    return []


@dataclass
class ExitSighting:
    """One batch's worth of sightings of one (connector, IP), ready to upsert.

    ``count`` is hand-outs only; the remaining fields are the state of the
    newest observation in the batch, whatever its source.
    """

    first_seen: datetime
    last_seen: datetime
    count: int = 1
    country: str | None = None
    proxy_id: str | None = None
    source: str | None = None
    claimed_country: str | None = None
    resolved_source: str | None = None
    country_conflict: bool | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    resolved_state: str | None = None
    resolved_city: str | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None

    @classmethod
    def fold(cls, sightings: Iterable[tuple[IpObservation, bool]]) -> dict[tuple[str, str], ExitSighting]:
        """Observations, each with whether it is a hand-out, as one exit per (connector, IP); one without a connector has none.

        Only hand-outs move the count, and the newest observation sets the exit's latest state.
        """
        exits: dict[tuple[str, str], ExitSighting] = {}
        for o, hand_out in sightings:
            if not o.connector_id:
                continue
            key = (o.connector_id, o.ip)
            current = exits.get(key)
            if current is None:
                current = exits[key] = cls(first_seen=o.observed_at, last_seen=o.observed_at, count=1 if hand_out else 0)
                current.observe(o)
                continue
            if hand_out:
                current.count += 1
            if o.observed_at < current.first_seen:
                current.first_seen = o.observed_at
            if o.observed_at >= current.last_seen:
                current.observe(o)
        return exits

    def observe(self, o: IpObservation) -> None:
        """Take ``o``'s state as the exit's latest: when it was seen, who held it, what was claimed and the verdict."""
        self.last_seen = o.observed_at
        self.country = o.resolved_country or self.country
        self.proxy_id = o.proxy_id
        self.source = o.source.value
        self.claimed_country = o.claimed_country
        self.resolved_source = o.resolved_source.value if o.resolved_source else None
        self.country_conflict = o.country_conflict
        self.claimed_state = o.claimed_state
        self.claimed_city = o.claimed_city
        self.resolved_state = o.resolved_state
        self.resolved_city = o.resolved_city
        self.state_conflict = o.state_conflict
        self.city_conflict = o.city_conflict


class GeoSettingsRepository:
    """The single ``geo_settings`` row (see ``GeoSettingsModel``)."""

    _COLUMNS = (
        "default_conflict_rule",
        "default_city_radius_km",
        "echo_url",
        "echo_ip_path",
        "echo_country_path",
        "echo_state_path",
        "echo_city_path",
        "echo_timeout_seconds",
        "health_check_attribution",
        "preflight_session_ttl_seconds",
        "preflight_max_attempts",
        "observation_retention_days",
        "exit_ip_retention_days",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> GeoSettings | None:
        result = await self._session.execute(
            select(GeoSettingsModel).where(GeoSettingsModel.id == 1)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        values: dict[str, Any] = {name: getattr(model, name) for name in self._COLUMNS}
        return GeoSettings(**values)

    async def save(self, settings: GeoSettings, updated_by: str | None = None) -> None:
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["default_conflict_rule"] = settings.default_conflict_rule.value
        values["updated_by"] = updated_by
        values["updated_at"] = utc_now()
        statement = pg_insert(GeoSettingsModel).values(id=1, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[GeoSettingsModel.id], set_=values
        )
        await self._session.execute(statement)
        await self._session.flush()


class GeoDatabaseRepository:
    """Rows of ``geo_databases`` plus the bytes in ``geo_database_blobs``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[GeoDatabaseRecord]:
        result = await self._session.execute(
            select(GeoDatabaseModel).order_by(
                GeoDatabaseModel.priority, GeoDatabaseModel.created_at
            )
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_id(self, database_id: str) -> GeoDatabaseRecord | None:
        result = await self._session.execute(
            select(GeoDatabaseModel).where(GeoDatabaseModel.id == database_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model is not None else None

    async def create(self, record: GeoDatabaseRecord, blob: bytes | None) -> GeoDatabaseRecord:
        model = GeoDatabaseModel(
            **self._columns(record), created_at=record.created_at, updated_at=record.updated_at
        )
        self._session.add(model)
        # Two flushes: without a mapped relationship the unit of work does not
        # know the blob depends on the row, so it must land first.
        await self._session.flush()
        if blob is not None:
            self._session.add(GeoDatabaseBlobModel(database_id=record.id, data=blob))
            await self._session.flush()
        return record

    async def update(
        self, record: GeoDatabaseRecord, blob: bytes | None = None
    ) -> GeoDatabaseRecord:
        result = await self._session.execute(
            select(GeoDatabaseModel).where(GeoDatabaseModel.id == record.id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return record
        for key, value in self._columns(record).items():
            if key == "id":
                continue
            setattr(model, key, value)
        model.version = model.version + 1
        model.updated_at = utc_now()
        record.version = model.version
        record.updated_at = model.updated_at
        if blob is not None:
            await self._session.execute(
                delete(GeoDatabaseBlobModel).where(GeoDatabaseBlobModel.database_id == record.id)
            )
            self._session.add(GeoDatabaseBlobModel(database_id=record.id, data=blob))
        await self._session.flush()
        return record

    async def delete(self, database_id: str) -> bool:
        result = await self._session.execute(
            delete(GeoDatabaseModel).where(GeoDatabaseModel.id == database_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def blob_ids(self) -> set[str]:
        """Ids of the rows that have their bytes stored (without reading them)."""
        result = await self._session.execute(select(GeoDatabaseBlobModel.database_id))
        return set(result.scalars().all())

    async def get_blob(self, database_id: str) -> bytes | None:
        result = await self._session.execute(
            select(GeoDatabaseBlobModel.data).where(GeoDatabaseBlobModel.database_id == database_id)
        )
        data = result.scalar_one_or_none()
        return bytes(data) if data is not None else None

    @staticmethod
    def _columns(record: GeoDatabaseRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "vendor": record.vendor.value,
            "kind": record.kind.value,
            "format": record.format.value,
            "source": record.source.value,
            "enabled": record.enabled,
            "priority": record.priority,
            "path": record.path,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "database_type": record.database_type,
            "build_epoch": record.build_epoch,
            "record_count": record.record_count,
            "ip_version": record.ip_version,
            "languages": list(record.languages),
            "description": record.description,
            "attribution": record.attribution,
            "update_url": record.update_url,
            "update_interval_hours": record.update_interval_hours,
            "update_auth": dict(record.update_auth),
            "update_paused": record.update_paused,
            "last_update_at": record.last_update_at,
            "last_update_error": record.last_update_error,
            "uploaded_by": record.uploaded_by,
            "version": record.version,
        }

    @staticmethod
    def _to_domain(model: GeoDatabaseModel) -> GeoDatabaseRecord:
        return GeoDatabaseRecord(
            id=model.id,
            name=model.name,
            vendor=GeoVendor(model.vendor),
            kind=GeoDatabaseKind(model.kind),
            format=GeoDatabaseFormat(model.format),
            source=GeoDatabaseSource(model.source),
            enabled=model.enabled,
            priority=model.priority,
            path=model.path,
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            database_type=model.database_type,
            build_epoch=model.build_epoch,
            record_count=model.record_count,
            ip_version=model.ip_version,
            languages=list(model.languages or []),
            description=model.description,
            attribution=model.attribution,
            update_url=model.update_url,
            update_interval_hours=model.update_interval_hours,
            update_auth=dict(model.update_auth or {}),
            update_paused=model.update_paused,
            last_update_at=model.last_update_at,
            last_update_error=model.last_update_error,
            uploaded_by=model.uploaded_by,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


# --- blob storage housekeeping ----------------------------------------------------------
#
# Postgres never overwrites in place: a replaced or removed database file stays on
# disk as dead TOAST tuples, and autovacuum only makes that space reusable, so the
# table would settle at twice the files it holds. Every ``geo_update`` job sweeps
# it and rewrites it when it holds well more than its files. It has one
# row per database and is read only when an instance syncs a file, so the
# exclusive lock of a second or two goes unnoticed, and a table left bloated until
# the next sweep costs nothing but disk.
#
# A rewrite keeps every row version a transaction still running anywhere in the
# database could see, so a file dropped moments before a sweep can survive it; the
# next sweep, an hour on, takes it. The sweep runs before the tick's downloads for
# the same reason: right after one replaced a file, it would copy the old one over.

_BLOB_TABLE = GeoDatabaseBlobModel.__tablename__
# Rewritten when the table holds a tenth more than its files (and at least a MiB
# more); a compact one carries about 4% of page and TOAST overhead.
_BLOAT_FRACTION = 0.1
_BLOAT_FLOOR_BYTES = 1024 * 1024

# The rewrite, then a plain VACUUM. The rewrite leaves the TOAST table's dead-row
# count as it was and never sets last_vacuum, so the system page would keep
# reporting the bloat it removed; the plain pass resets both, and on a table just
# rewritten it has nothing to do.
_RECLAIM_SQL = (text(f"VACUUM FULL {_BLOB_TABLE}"), text(f"VACUUM (ANALYZE) {_BLOB_TABLE}"))
# The table on disk against the files in it; pg_column_size reads the TOAST
# pointer and fetches nothing.
_BLOB_BYTES_SQL = text(
    f"SELECT pg_total_relation_size('{_BLOB_TABLE}'), "
    f"(SELECT coalesce(sum(pg_column_size(data)), 0) FROM {_BLOB_TABLE})"
)


async def reclaim_blob_space(conn: AsyncConnection) -> None:
    """Rewrite the blob table if it holds far more than its files, logging what it gave back.

    ``conn`` must be in autocommit, since VACUUM cannot run in a transaction.
    """
    stored, files = (await conn.execute(_BLOB_BYTES_SQL)).one()
    if not _bloated(stored, files):
        return
    for statement in _RECLAIM_SQL:
        await conn.execute(statement)
    after, files_after = (await conn.execute(_BLOB_BYTES_SQL)).one()
    if _bloated(after, files_after):
        # Rewritten again on every sweep until whatever kept the old bytes lets go.
        logger.warning(
            "IP database storage still bloated after a rewrite: a long-running transaction holds the old files,"
            " or the database role does not own the table",
            before_bytes=stored,
            after_bytes=after,
            file_bytes=files_after,
        )
    else:
        logger.info("IP database storage reclaimed", before_bytes=stored, after_bytes=after, file_bytes=files_after)


def _bloated(stored: int, files: int) -> bool:
    return stored - files > files * _BLOAT_FRACTION + _BLOAT_FLOOR_BYTES


class ObservationRepository:
    """Raw ``ip_observations`` rows and the ``connector_exit_ips`` aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, observations: list[IpObservation]) -> int:
        """Bulk insert one batch, each session and instance ID cut to its column's width; the caller commits."""
        if not observations:
            return 0
        # The columns' widths: a client's -sessid- can be any length, and one
        # value too long would fail its whole batch at every delivery.
        columns = IpObservationModel.__table__.c
        session_width = cast(String, columns.session_id.type).length
        instance_width = cast(String, columns.instance_id.type).length
        rows = [
            {
                "observed_at": o.observed_at,
                "proxy_id": o.proxy_id,
                "connector_id": o.connector_id,
                "project_id": o.project_id,
                "session_id": o.session_id[:session_width] if o.session_id is not None else None,
                "source": o.source.value,
                "ip": o.ip,
                "claimed_country": o.claimed_country,
                "endpoint_country": o.endpoint_country,
                "resolved_country": o.resolved_country,
                "resolved_source": o.resolved_source.value if o.resolved_source else None,
                "country_conflict": o.country_conflict,
                "claimed_state": o.claimed_state,
                "claimed_city": o.claimed_city,
                "resolved_state": o.resolved_state,
                "resolved_city": o.resolved_city,
                "state_conflict": o.state_conflict,
                "city_conflict": o.city_conflict,
                "candidates": o.candidates,
                "instance_id": o.instance_id[:instance_width],
            }
            for o in observations
        ]
        await self._session.execute(pg_insert(IpObservationModel), rows)
        return len(rows)

    @staticmethod
    def _filtered(
        query: Any,
        *,
        connector_id: str | None = None,
        proxy_id: str | None = None,
        project_id: str | None = None,
        source: str | None = None,
        ip: str | None = None,
        claimed_country: str | None = None,
        resolved_country: str | None = None,
        verdict: str | None = None,
        conflicts_only: bool = False,
        until: datetime | None = None,
    ) -> Any:
        """The observation filters the history page offers, applied server side.

        The table can hold millions of rows, so each filter narrows the query itself; ``verdict`` is the label the page
        shows (see :func:`_verdict_clauses`), and ``until`` starts the history at that moment instead of now.
        """
        m = IpObservationModel
        if connector_id:
            query = query.where(m.connector_id == connector_id)
        if proxy_id:
            query = query.where(m.proxy_id == proxy_id)
        if project_id:
            query = query.where(m.project_id == project_id)
        if source:
            query = query.where(m.source == source)
        if ip:
            query = query.where(m.ip == ip)
        if claimed_country:
            query = query.where(m.claimed_country == claimed_country.upper())
        if resolved_country:
            query = query.where(m.resolved_country == resolved_country.upper())
        if until is not None:
            query = query.where(m.observed_at <= until)
        query = query.where(*_verdict_clauses(m, "contradicted" if conflicts_only else verdict))
        return query

    async def _capped_total(self, matching: Select[Any], *, offset: int, limit: int, found: int) -> tuple[int, bool]:
        """How many rows ``matching`` selects, counted at most COUNT_CAP past ``offset``, and whether that is exact.

        Short of exact the total is ``offset + COUNT_CAP``, a lower bound that always leaves a next page; a short page
        is the end and counts nothing, and a page past the end is recounted from the start.

        Args:
            found: how many rows the page at ``offset`` and ``limit`` held.
        """
        if found < limit:
            if found or not offset:
                return offset + found, True
        else:
            window = matching.offset(offset).limit(COUNT_CAP + 1).subquery()
            seen = int((await self._session.execute(select(func.count()).select_from(window))).scalar_one())
            if seen > COUNT_CAP:
                return offset + COUNT_CAP, False
            if seen or not offset:
                return offset + seen, True
        head = matching.limit(offset).subquery()
        return int((await self._session.execute(select(func.count()).select_from(head))).scalar_one()), True

    async def _time_limited[T](self, read: Callable[[], Awaitable[T]]) -> T | None:
        """What ``read`` returns, or None when one of its statements ran past ``READ_TIMEOUT_MS``.

        The time limit lives in a savepoint that is always rolled back, so the caller's transaction is left as it was.
        """
        savepoint = await self._session.begin_nested()
        try:
            await self._session.execute(select(func.set_config("statement_timeout", str(READ_TIMEOUT_MS), True)))
            result = await read()
        except DBAPIError as exc:
            await savepoint.rollback()
            if getattr(exc.orig, "sqlstate", None) != _QUERY_CANCELED:
                raise
            return None
        await savepoint.rollback()
        return result

    async def _exact_total(self, matching: Select[Any]) -> int | None:
        """How many rows ``matching`` selects, or None when counting ran past ``READ_TIMEOUT_MS``."""

        async def count() -> int:
            return int((await self._session.execute(select(func.count()).select_from(matching.subquery()))).scalar_one())

        return await self._time_limited(count)

    async def _plan_for_the_values(self) -> None:
        """Plan the transaction's further queries for their own filter values, never from a cached generic plan.

        A generic plan picks one way for every value: it would sort all of a busy proxy's rows, or walk the whole history for a rare one.
        """
        await self._session.execute(select(func.set_config("plan_cache_mode", "force_custom_plan", True)))

    async def recent(
        self, *, limit: int = 100, offset: int = 0, **filters: Any
    ) -> tuple[list[dict[str, Any]], int, bool] | None:
        """One page of observations, newest first, how many match, and whether that count is exact; None past ``READ_TIMEOUT_MS``.

        The count stops ``COUNT_CAP`` rows past the page.
        """
        await self._plan_for_the_values()
        return await self._time_limited(lambda: self._page(limit=limit, offset=offset, **filters))

    async def _page(self, *, limit: int, offset: int, **filters: Any) -> tuple[list[dict[str, Any]], int, bool]:
        m = IpObservationModel
        page = self._filtered(select(m), **filters).order_by(m.observed_at.desc(), m.id.desc()).offset(offset).limit(limit)
        found = (await self._session.execute(page)).scalars().all()
        total, exact = await self._capped_total(
            self._filtered(select(literal_column("1")).select_from(m), **filters), offset=offset, limit=limit, found=len(found)
        )
        rows = [
            {
                "id": o.id,
                "observed_at": o.observed_at,
                "proxy_id": o.proxy_id,
                "connector_id": o.connector_id,
                "project_id": o.project_id,
                "session_id": o.session_id,
                "source": o.source,
                "ip": o.ip,
                "claimed_country": o.claimed_country,
                "endpoint_country": o.endpoint_country,
                "resolved_country": o.resolved_country,
                "resolved_source": o.resolved_source,
                "country_conflict": o.country_conflict,
                "claimed_state": o.claimed_state,
                "claimed_city": o.claimed_city,
                "resolved_state": o.resolved_state,
                "resolved_city": o.resolved_city,
                "state_conflict": o.state_conflict,
                "city_conflict": o.city_conflict,
                "candidates": list(o.candidates or []),
                "instance_id": o.instance_id,
            }
            for o in found
        ]
        return rows, total, exact

    async def count_matching(self, **filters: Any) -> int | None:
        """The exact number of observations under ``filters`` (see :meth:`_exact_total`)."""
        await self._plan_for_the_values()
        return await self._exact_total(self._filtered(select(literal_column("1")).select_from(IpObservationModel), **filters))

    async def exit_accuracy(
        self, *, connector_ids: list[str] | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Per connector: distinct exits seen in the window and how their vendor claims were judged, per level.

        Each exit counts once, with the verdict of its latest observation.
        """
        m = ConnectorExitIpModel

        def counted(condition: Any) -> Any:
            return func.sum(case((condition, 1), else_=0))

        totals = select(
            m.connector_id,
            func.count().label("exits"),
            counted(m.claimed_country.is_not(None)).label("claimed"),
            counted(m.country_conflict.is_(False)).label("confirmed"),
            counted(m.country_conflict.is_(True)).label("contradicted"),
            counted(and_(m.claimed_country.is_not(None), m.country_conflict.is_(None))).label("open"),
            counted(m.claimed_state.is_not(None)).label("state_claimed"),
            counted(m.state_conflict.is_(False)).label("state_confirmed"),
            counted(m.state_conflict.is_(True)).label("state_contradicted"),
            counted(and_(m.claimed_state.is_not(None), m.state_conflict.is_(None))).label("state_open"),
            counted(m.claimed_city.is_not(None)).label("city_claimed"),
            counted(m.city_conflict.is_(False)).label("city_confirmed"),
            counted(m.city_conflict.is_(True)).label("city_contradicted"),
            counted(and_(m.claimed_city.is_not(None), m.city_conflict.is_(None))).label("city_open"),
        ).group_by(m.connector_id)
        wrong_by_level = {
            "country": (
                select(m.connector_id, m.claimed_country.label("claimed"), m.country.label("observed"), func.count().label("exits"))
                .where(m.country_conflict.is_(True))
                .group_by(m.connector_id, m.claimed_country, m.country)
            ),
            "state": (
                select(m.connector_id, m.claimed_state.label("claimed"), m.resolved_state.label("observed"), func.count().label("exits"))
                .where(m.state_conflict.is_(True))
                .group_by(m.connector_id, m.claimed_state, m.resolved_state)
            ),
            "city": (
                select(m.connector_id, m.claimed_city.label("claimed"), m.resolved_city.label("observed"), func.count().label("exits"))
                .where(m.city_conflict.is_(True))
                .group_by(m.connector_id, m.claimed_city, m.resolved_city)
            ),
        }
        if connector_ids is not None:
            if not connector_ids:
                return []
            totals = totals.where(m.connector_id.in_(connector_ids))
            wrong_by_level = {level: q.where(m.connector_id.in_(connector_ids)) for level, q in wrong_by_level.items()}
        if since is not None:
            totals = totals.where(m.last_seen >= since)
            wrong_by_level = {level: q.where(m.last_seen >= since) for level, q in wrong_by_level.items()}
        rows = {
            row.connector_id: {
                "connector_id": row.connector_id,
                "exits": int(row.exits or 0),
                "country": {
                    "claimed": int(row.claimed or 0),
                    "confirmed": int(row.confirmed or 0),
                    "contradicted": int(row.contradicted or 0),
                    "open": int(row.open or 0),
                },
                "state": {
                    "claimed": int(row.state_claimed or 0),
                    "confirmed": int(row.state_confirmed or 0),
                    "contradicted": int(row.state_contradicted or 0),
                    "open": int(row.state_open or 0),
                },
                "city": {
                    "claimed": int(row.city_claimed or 0),
                    "confirmed": int(row.city_confirmed or 0),
                    "contradicted": int(row.city_contradicted or 0),
                    "open": int(row.city_open or 0),
                },
                "breakdown": [],
            }
            for row in (await self._session.execute(totals)).all()
        }
        for level, query in wrong_by_level.items():
            for row in (await self._session.execute(query)).all():
                rows[row.connector_id]["breakdown"].append(
                    {"level": level, "claimed": row.claimed, "observed": row.observed, "exits": int(row.exits)}
                )
        for entry in rows.values():
            entry["breakdown"].sort(key=lambda b: -b["exits"])
        return list(rows.values())

    async def delete_older_than(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(IpObservationModel).where(IpObservationModel.observed_at < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def existing_connector_ids(self, connector_ids: set[str]) -> set[str]:
        """Which of ``connector_ids`` still exist. Aggregates cascade with connectors, so
        sightings of a connector deleted since they were made must not be written."""
        if not connector_ids:
            return set()
        result = await self._session.execute(
            select(ConnectorModel.id).where(ConnectorModel.id.in_(sorted(connector_ids)))
        )
        return {row[0] for row in result.all()}

    # --- distinct exits per connector -----------------------------------------------------

    _LATEST_STATE = (
        "proxy_id", "source", "claimed_country", "resolved_source", "country_conflict",
        "claimed_state", "claimed_city", "resolved_state", "resolved_city", "state_conflict", "city_conflict",
    )
    # Rows per statement. asyncpg takes at most 32767 bound values per
    # statement: an exit upsert row binds 17 (under 1928 rows fit), a
    # judgement 14 (under 2341).
    _EXIT_UPSERT_ROWS = 1000
    _JUDGEMENT_ROWS = 1000

    async def add_exit_ips(self, exits: dict[tuple[str, str], ExitSighting]) -> None:
        """Upsert (connector, ip) rows: first sighting kept, last sighting and count advanced, in the caller's transaction.

        The latest-state columns move only for a batch whose newest sighting is at least as new: out of order, nothing rolls back.
        """
        if not exits:
            return
        # In key order, so two workers writing overlapping exits lock them in the same order: one waits for the
        # other, where opposite orders would each hold a row the other wants, a deadlock Postgres ends by failing one.
        rows = [
            {
                "connector_id": connector_id,
                "ip": ip,
                "first_seen": sighting.first_seen,
                "last_seen": sighting.last_seen,
                "sightings": sighting.count,
                "country": sighting.country,
                **{name: getattr(sighting, name) for name in self._LATEST_STATE},
            }
            for (connector_id, ip), sighting in sorted(exits.items(), key=lambda item: item[0])
        ]
        for start in range(0, len(rows), self._EXIT_UPSERT_ROWS):
            await self._upsert_exit_rows(rows[start:start + self._EXIT_UPSERT_ROWS])

    async def _upsert_exit_rows(self, rows: list[dict[str, Any]]) -> None:
        statement = pg_insert(ConnectorExitIpModel).values(rows)
        newer = statement.excluded.last_seen >= ConnectorExitIpModel.last_seen
        statement = statement.on_conflict_do_update(
            index_elements=[ConnectorExitIpModel.connector_id, ConnectorExitIpModel.ip],
            set_={
                "first_seen": func.least(
                    ConnectorExitIpModel.first_seen, statement.excluded.first_seen
                ),
                "last_seen": func.greatest(
                    ConnectorExitIpModel.last_seen, statement.excluded.last_seen
                ),
                "sightings": ConnectorExitIpModel.sightings + statement.excluded.sightings,
                "country": func.coalesce(statement.excluded.country, ConnectorExitIpModel.country),
                **{
                    name: case(
                        (newer, getattr(statement.excluded, name)),
                        else_=getattr(ConnectorExitIpModel, name),
                    )
                    for name in self._LATEST_STATE
                },
            },
        )
        await self._session.execute(statement)

    async def apply_judgements(self, judgements: list[ExitJudgement]) -> int:
        """Rewrite the latest verdict, with its claim, of exits already on record; how many rows changed.

        Only the row the judged proxy holds, only when no older than its last sighting; no row is made and no sighting moves.
        """
        newest: dict[tuple[str, str, str], ExitJudgement] = {}
        for j in sorted(judgements, key=lambda j: j.judged_at):
            if j.connector_id:
                newest[(j.connector_id, j.ip, j.proxy_id)] = j
        data = [
            (
                j.connector_id,
                j.ip,
                j.proxy_id,
                j.judged_at,
                j.claimed_country,
                j.resolved_country,
                j.resolved_source.value if j.resolved_source else None,
                j.country_conflict,
                j.claimed_state,
                j.claimed_city,
                j.resolved_state,
                j.resolved_city,
                j.state_conflict,
                j.city_conflict,
            )
            # In key order, like the exit upserts in the same transaction.
            for _key, j in sorted(newest.items(), key=lambda item: item[0])
        ]
        rewritten = 0
        for start in range(0, len(data), self._JUDGEMENT_ROWS):
            rewritten += await self._apply_judgement_rows(data[start:start + self._JUDGEMENT_ROWS])
        return rewritten

    async def _apply_judgement_rows(self, data: list[tuple[Any, ...]]) -> int:
        judged = values(
            column("connector_id", String),
            column("ip", String),
            column("proxy_id", String),
            column("judged_at", DateTime),
            column("claimed_country", String),
            column("country", String),
            column("resolved_source", String),
            column("country_conflict", Boolean),
            column("claimed_state", String),
            column("claimed_city", String),
            column("resolved_state", String),
            column("resolved_city", String),
            column("state_conflict", Boolean),
            column("city_conflict", Boolean),
            name="judged",
        ).data(data)
        table = cast("Table", ConnectorExitIpModel.__table__)
        statement = (
            update(table)
            .where(
                table.c.connector_id == judged.c.connector_id,
                table.c.ip == judged.c.ip,
                table.c.last_seen <= judged.c.judged_at,
                or_(table.c.proxy_id.is_(None), table.c.proxy_id == judged.c.proxy_id),
            )
            .values(
                source=JUDGEMENT_SOURCE,
                claimed_country=judged.c.claimed_country,
                country=func.coalesce(judged.c.country, table.c.country),
                resolved_source=judged.c.resolved_source,
                # A VALUES column that is NULL in every row is untyped text to
                # Postgres; the casts keep an all-NULL batch assignable.
                country_conflict=cast_sql(judged.c.country_conflict, Boolean),
                claimed_state=cast_sql(judged.c.claimed_state, String),
                claimed_city=cast_sql(judged.c.claimed_city, String),
                resolved_state=cast_sql(judged.c.resolved_state, String),
                resolved_city=cast_sql(judged.c.resolved_city, String),
                state_conflict=cast_sql(judged.c.state_conflict, Boolean),
                city_conflict=cast_sql(judged.c.city_conflict, Boolean),
            )
        )
        result = await self._session.execute(statement)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def exit_holders(self, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
        """For each (connector, ip) already on record, the proxy that last held it (None if unknown).

        The ingest asks this only for proxies whose last counted exit Redis does not know, to tell an exit counted
        before an upgrade or a Redis loss from a genuinely new one.
        """
        if not keys:
            return {}
        m = ConnectorExitIpModel
        query = select(m.connector_id, m.ip, m.proxy_id).where(tuple_(m.connector_id, m.ip).in_(list(keys)))
        return {(row.connector_id, row.ip): row.proxy_id for row in (await self._session.execute(query)).all()}

    async def exit_summary(
        self, *, connector_ids: list[str] | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Per connector: distinct exits ever, distinct exits first seen in the window, reuse figures."""
        window = (
            func.sum(case((ConnectorExitIpModel.first_seen >= since, 1), else_=0))
            if since is not None
            else func.count(ConnectorExitIpModel.ip)
        )
        query = select(
            ConnectorExitIpModel.connector_id,
            func.count(ConnectorExitIpModel.ip).label("unique_total"),
            window.label("unique_in_window"),
            func.sum(ConnectorExitIpModel.sightings).label("sightings"),
            func.sum(case((ConnectorExitIpModel.sightings > 1, 1), else_=0)).label("reused"),
            func.max(ConnectorExitIpModel.sightings).label("max_sightings"),
            func.max(ConnectorExitIpModel.last_seen).label("last_seen"),
        ).group_by(ConnectorExitIpModel.connector_id)
        if connector_ids is not None:
            if not connector_ids:
                return []
            query = query.where(ConnectorExitIpModel.connector_id.in_(connector_ids))
        result = await self._session.execute(query)
        return [
            {
                "connector_id": row.connector_id,
                "unique_total": int(row.unique_total or 0),
                "unique_in_window": int(row.unique_in_window or 0),
                "sightings": int(row.sightings or 0),
                "reused": int(row.reused or 0),
                "max_sightings": int(row.max_sightings or 0),
                "last_seen": row.last_seen,
            }
            for row in result.all()
        ]

    async def exit_sharing(self, *, connector_ids: list[str], since: datetime) -> dict[str, Any] | None:
        """How the exits ``connector_ids`` handed out since ``since`` overlap; None past ``READ_TIMEOUT_MS``.

        Per connector its distinct exits, how many were handed out more than once (ever) and how many another of the
        connectors also handed out in the window; per pair the exits both did; and the distinct exits across all.
        """
        if not connector_ids:
            return {"exits": 0, "shared": 0, "connectors": [], "pairs": []}
        m = ConnectorExitIpModel

        def counted(condition: Any) -> Any:
            return func.sum(case((condition, 1), else_=0))

        seen = (
            select(m.connector_id, m.ip, m.sightings)
            .where(m.connector_id.in_(connector_ids), m.last_seen >= since)
            .cte("seen")
        )
        holders = select(seen.c.ip, func.count().label("holders")).group_by(seen.c.ip).cte("holders")
        # One statement, so the window's rows are read once and the time limit covers the whole comparison.
        totals = select(
            literal_column("'total'").label("kind"),
            null().label("connector_id"),
            null().label("other_id"),
            func.count().label("exits"),
            literal_column("0").label("reused"),
            counted(holders.c.holders > 1).label("shared"),
        ).select_from(holders)
        per_connector = (
            select(
                literal_column("'connector'"),
                seen.c.connector_id,
                null(),
                func.count(),
                counted(seen.c.sightings > 1),
                counted(holders.c.holders > 1),
            )
            .select_from(seen)
            .join(holders, holders.c.ip == seen.c.ip)
            .group_by(seen.c.connector_id)
        )
        other = seen.alias("other")
        pairs = (
            select(
                literal_column("'pair'"),
                seen.c.connector_id,
                other.c.connector_id,
                literal_column("0"),
                literal_column("0"),
                func.count(),
            )
            .select_from(seen)
            .join(other, and_(other.c.ip == seen.c.ip, other.c.connector_id > seen.c.connector_id))
            .group_by(seen.c.connector_id, other.c.connector_id)
        )

        async def read() -> dict[str, Any]:
            sharing: dict[str, Any] = {"exits": 0, "shared": 0, "connectors": [], "pairs": []}
            for row in (await self._session.execute(union_all(totals, per_connector, pairs))).all():
                if row.kind == "total":
                    sharing["exits"], sharing["shared"] = int(row.exits or 0), int(row.shared or 0)
                elif row.kind == "connector":
                    sharing["connectors"].append(
                        {
                            "connector_id": row.connector_id,
                            "exits": int(row.exits),
                            "reused": int(row.reused or 0),
                            "shared": int(row.shared or 0),
                        }
                    )
                else:
                    sharing["pairs"].append(
                        {"connector_id": row.connector_id, "other_id": row.other_id, "shared": int(row.shared)}
                    )
            return sharing

        return await self._time_limited(read)

    @staticmethod
    def _exit_ips_filtered(
        query: Any,
        *,
        connector_ids: list[str] | None = None,
        ip: str | None = None,
        proxy_id: str | None = None,
        country: str | None = None,
        claimed_country: str | None = None,
        verdict: str | None = None,
    ) -> Any:
        """The exit IPs view's filters, applied server side, each mirroring a column of the view but ``connector_ids``.

        Args:
            connector_ids: the connectors to show, one or a project's, or None for all (the caller answers an empty list itself).
        """
        m = ConnectorExitIpModel
        if connector_ids is not None:
            query = query.where(m.connector_id.in_(connector_ids))
        if ip:
            query = query.where(m.ip == ip)
        if proxy_id:
            query = query.where(m.proxy_id == proxy_id)
        if country:
            query = query.where(m.country == country.upper())
        if claimed_country:
            query = query.where(m.claimed_country == claimed_country.upper())
        return query.where(*_verdict_clauses(m, verdict))

    async def exit_ips(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> tuple[list[dict[str, Any]], int, bool]:
        """One page of distinct exit IPs, most recently seen first, how many match and whether that count is exact.

        Each row carries its latest state; ``filters`` are those of :meth:`_exit_ips_filtered`, and the count is taken
        as for observations (:meth:`_capped_total`).
        """
        if filters.get("connector_ids") == []:
            return [], 0, True
        m = ConnectorExitIpModel
        page = self._exit_ips_filtered(select(m), **filters).order_by(m.last_seen.desc(), m.connector_id, m.ip).offset(offset).limit(limit)
        found = (await self._session.execute(page)).scalars().all()
        total, exact = await self._capped_total(
            self._exit_ips_filtered(select(literal_column("1")).select_from(m), **filters), offset=offset, limit=limit, found=len(found)
        )
        rows = [
            {
                "connector_id": row.connector_id,
                "ip": row.ip,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
                "sightings": row.sightings,
                "country": row.country,
                **{name: getattr(row, name) for name in self._LATEST_STATE},
            }
            for row in found
        ]
        return rows, total, exact

    async def count_exit_ips(self, **filters: Any) -> int | None:
        """The exact number of exit IPs under ``filters`` (see :meth:`_exact_total`)."""
        if filters.get("connector_ids") == []:
            return 0
        return await self._exact_total(self._exit_ips_filtered(select(literal_column("1")).select_from(ConnectorExitIpModel), **filters))

    async def delete_exit_ips_last_seen_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(ConnectorExitIpModel).where(ConnectorExitIpModel.last_seen < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def estimated_count(self) -> int:
        """Rows in ``ip_observations`` as the planner last measured them.

        Free where ``count(*)`` reads the whole table and kept within a few percent by autovacuum; a table never
        analysed reports -1, so a new one is counted outright.
        """
        result = await self._session.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE oid = 'ip_observations'::regclass")
        )
        estimate = int(result.scalar() or 0)
        if estimate >= 0:
            return estimate
        result = await self._session.execute(text("SELECT count(*) FROM ip_observations"))
        return int(result.scalar() or 0)
