# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repositories for IP attribution: databases, observations, aggregates and runtime settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
    GeoDatabaseFormat,
    GeoDatabaseKind,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    GeoVendor,
    IpObservation,
)


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
    conflict: bool = False
    disagreement: bool = False


class GeoSettingsRepository:
    """The single ``geo_settings`` row (see ``GeoSettingsModel``)."""

    _COLUMNS = (
        "default_conflict_rule",
        "echo_url",
        "echo_ip_path",
        "echo_country_path",
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
        values["default_sources"] = list(model.default_sources or [])
        return GeoSettings(**values)

    async def save(self, settings: GeoSettings, updated_by: str | None = None) -> None:
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["default_conflict_rule"] = settings.default_conflict_rule.value
        values["default_sources"] = [kind.value for kind in settings.default_sources]
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
            last_update_at=model.last_update_at,
            last_update_error=model.last_update_error,
            uploaded_by=model.uploaded_by,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ObservationRepository:
    """Raw ``ip_observations`` rows and the ``connector_exit_ips`` aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(self, observations: list[IpObservation]) -> int:
        """Bulk insert one batch; the caller commits."""
        if not observations:
            return 0
        rows = [
            {
                "observed_at": o.observed_at,
                "proxy_id": o.proxy_id,
                "connector_id": o.connector_id,
                "project_id": o.project_id,
                "session_id": o.session_id,
                "source": o.source.value,
                "ip": o.ip,
                "claimed_country": o.claimed_country,
                "endpoint_country": o.endpoint_country,
                "resolved_country": o.resolved_country,
                "resolved_source": o.resolved_source.value if o.resolved_source else None,
                "conflict": o.conflict,
                "disagreement": o.disagreement,
                "candidates": o.candidates,
                "instance_id": o.instance_id,
            }
            for o in observations
        ]
        await self._session.execute(pg_insert(IpObservationModel), rows)
        return len(rows)

    @staticmethod
    def _filtered(
        query: Any,
        *,
        connector_id: str | None,
        proxy_id: str | None,
        project_id: str | None,
        source: str | None,
        ip: str | None,
        claimed_country: str | None,
        resolved_country: str | None,
        verdict: str | None,
        conflicts_only: bool,
    ) -> Any:
        """The observation filters the history page offers, applied server side.

        The table can hold millions of rows, so the browser never sees more
        than one page; every filter must therefore narrow the query itself.
        ``verdict`` is the label the page shows, derived from three columns.
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
        if verdict == "contradicted" or conflicts_only:
            query = query.where(m.conflict.is_(True))
        elif verdict == "uncertain":
            query = query.where(m.conflict.is_(False), m.disagreement.is_(True))
        elif verdict == "confirmed":
            query = query.where(
                m.conflict.is_(False), m.disagreement.is_(False), m.claimed_country.is_not(None)
            )
        elif verdict == "no_claim":
            query = query.where(m.claimed_country.is_(None))
        return query

    async def recent(
        self,
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
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of observations, newest first, and the size of the whole match.

        The total rides on every row as ``count(*) OVER ()``, so page and count
        are one query and always agree. A page past the end has no rows to
        carry it and reports zero; the caller starts over from the first page.
        """
        query = self._filtered(
            select(IpObservationModel, func.count().over().label("total")),
            connector_id=connector_id,
            proxy_id=proxy_id,
            project_id=project_id,
            source=source,
            ip=ip,
            claimed_country=claimed_country,
            resolved_country=resolved_country,
            verdict=verdict,
            conflicts_only=conflicts_only,
        )
        query = (
            query.order_by(IpObservationModel.observed_at.desc(), IpObservationModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = (await self._session.execute(query)).all()
        total = int(result[0].total) if result else 0
        rows = [
            {
                "id": m.id,
                "observed_at": m.observed_at,
                "proxy_id": m.proxy_id,
                "connector_id": m.connector_id,
                "project_id": m.project_id,
                "session_id": m.session_id,
                "source": m.source,
                "ip": m.ip,
                "claimed_country": m.claimed_country,
                "endpoint_country": m.endpoint_country,
                "resolved_country": m.resolved_country,
                "resolved_source": m.resolved_source,
                "conflict": m.conflict,
                "disagreement": m.disagreement,
                "candidates": list(m.candidates or []),
                "instance_id": m.instance_id,
            }
            for m, _total in result
        ]
        return rows, total

    async def exit_accuracy(
        self, *, connector_ids: list[str] | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Per connector: distinct exits seen in the window and how their vendor claims were judged.

        Each exit counts once, with the verdict of its latest observation, so
        re-checks of the same exit change nothing and a re-attribution after a
        database change simply rewrites the verdicts. ``breakdown`` lists the
        contradicted (claimed, resolved) pairs with the number of exits each.
        """
        m = ConnectorExitIpModel
        claimed = m.claimed_country.is_not(None)
        confirmed = and_(claimed, m.conflict.is_(False), m.disagreement.is_(False))
        uncertain = and_(claimed, m.conflict.is_(False), m.disagreement.is_(True))
        totals = select(
            m.connector_id,
            func.count().label("exits"),
            func.sum(case((claimed, 1), else_=0)).label("claimed"),
            func.sum(case((confirmed, 1), else_=0)).label("confirmed"),
            func.sum(case((m.conflict.is_(True), 1), else_=0)).label("contradicted"),
            func.sum(case((uncertain, 1), else_=0)).label("uncertain"),
        ).group_by(m.connector_id)
        wrong = (
            select(m.connector_id, m.claimed_country, m.country, func.count().label("exits"))
            .where(m.conflict.is_(True))
            .group_by(m.connector_id, m.claimed_country, m.country)
        )
        if connector_ids is not None:
            if not connector_ids:
                return []
            totals = totals.where(m.connector_id.in_(connector_ids))
            wrong = wrong.where(m.connector_id.in_(connector_ids))
        if since is not None:
            totals = totals.where(m.last_seen >= since)
            wrong = wrong.where(m.last_seen >= since)
        rows = {
            row.connector_id: {
                "connector_id": row.connector_id,
                "exits": int(row.exits or 0),
                "claimed": int(row.claimed or 0),
                "confirmed": int(row.confirmed or 0),
                "contradicted": int(row.contradicted or 0),
                "uncertain": int(row.uncertain or 0),
                "breakdown": [],
            }
            for row in (await self._session.execute(totals)).all()
        }
        for row in (await self._session.execute(wrong)).all():
            rows[row.connector_id]["breakdown"].append(
                {"claimed_country": row.claimed_country, "observed_country": row.country, "exits": int(row.exits)}
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

    _LATEST_STATE = ("proxy_id", "source", "claimed_country", "resolved_source", "conflict", "disagreement")

    async def add_exit_ips(self, exits: dict[tuple[str, str], ExitSighting]) -> None:
        """Upsert (connector, ip) rows: first sighting kept, last sighting and count advanced.

        The latest-state columns are taken from the batch only when its
        newest sighting is at least as new as the row's, so batches flushed
        out of order by different instances cannot roll the state back.
        """
        if not exits:
            return
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
            for (connector_id, ip), sighting in exits.items()
        ]
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

    async def exit_ips(
        self,
        *,
        connector_ids: list[str] | None = None,
        ip: str | None = None,
        proxy_id: str | None = None,
        country: str | None = None,
        claimed_country: str | None = None,
        verdict: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of distinct exit IPs with their latest state, most recently seen first.

        ``connector_ids`` is the connectors to show: one, a project's, or None
        for all (an empty list matches nothing). The other filters mirror the
        view's columns. Returns the page and the size of the whole match,
        carried by ``count(*) OVER ()``.
        """
        m = ConnectorExitIpModel
        query = select(m, func.count().over().label("total"))
        if connector_ids is not None:
            if not connector_ids:
                return [], 0
            query = query.where(m.connector_id.in_(connector_ids))
        if ip:
            query = query.where(m.ip == ip)
        if proxy_id:
            query = query.where(m.proxy_id == proxy_id)
        if country:
            query = query.where(m.country == country.upper())
        if claimed_country:
            query = query.where(m.claimed_country == claimed_country.upper())
        if verdict == "contradicted":
            query = query.where(m.conflict.is_(True))
        elif verdict == "uncertain":
            query = query.where(m.conflict.is_(False), m.disagreement.is_(True))
        elif verdict == "confirmed":
            query = query.where(
                m.conflict.is_(False), m.disagreement.is_(False), m.claimed_country.is_not(None)
            )
        elif verdict == "no_claim":
            query = query.where(m.claimed_country.is_(None))
        query = query.order_by(m.last_seen.desc(), m.connector_id, m.ip).offset(offset).limit(limit)
        result = (await self._session.execute(query)).all()
        total = int(result[0].total) if result else 0
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
            for row, _total in result
        ]
        return rows, total

    async def delete_exit_ips_last_seen_before(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(ConnectorExitIpModel).where(ConnectorExitIpModel.last_seen < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def count(self) -> int:
        result = await self._session.execute(text("SELECT count(*) FROM ip_observations"))
        return int(result.scalar() or 0)
