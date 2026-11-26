# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit reputation records (``exit_ip_reputation``, ``connector_site_reputation``) and their history: the moves of
the dynamic connectors' factors (``connector_factor_moves``) and of the exit IPs' avoids (``exit_ip_avoid_moves``), in
the caller's session; the caller commits."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple, cast

from sqlalchemy import (
    ColumnElement,
    Date,
    Row,
    Select,
    Table,
    and_,
    case,
    delete,
    false,
    func,
    literal,
    literal_column,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.stats import RequestCount
from api.db.filters import any_of, rows_of
from api.db.geo_repository import COUNT_CAP
from api.db.models import (
    ConnectorFactorMoveModel,
    ConnectorModel,
    ConnectorSiteReputationModel,
    ExitIpAvoidMoveModel,
    ExitIpReputationModel,
    ProjectModel,
)
from api.models.project import ExitReputationMode
from api.reputation.record import (
    MAX_EXIT_SITES,
    ConnectorChange,
    ConnectorSiteKey,
    ConnectorSiteReputation,
    ExitIpKey,
    ExitIpReputation,
    IpChange,
    ReportCounts,
    ReputationChanges,
    ReputationPolicy,
    ReputationRecord,
    ReputationStatus,
)
from api.reputation.site import canonical_ip


class ReputationSort(StrEnum):
    """How a page of a project's exit IP records is ordered: the one seen last first, or the most reported."""

    LAST_SEEN = "last_seen"
    REPORTS = "reports"


class ReputationSummary(NamedTuple):
    """One IP's records in a project over every site: its reports, the sites it has a record for, and those avoiding
    it now with the latest of their ends."""

    ip: str
    good_reports: int
    bad_reports: int
    sites: int
    avoided_sites: int
    avoided_until: datetime | None


class Recording(NamedTuple):
    """What a batch of exit reports may count for: its projects that record, and the (project, connector) pairs whose
    project records and whose connector is that project's."""

    projects: frozenset[str]
    connectors: frozenset[tuple[str, str]]


class _Settings(NamedTuple):
    """What the repository needs of a project that records: how it weighs evidence, and whether it routes by it."""

    policy: ReputationPolicy
    avoids: bool


class _RecordTable[R: ReputationRecord]:
    """One reputation table as the repository writes it: its key, the columns evidence changes (all but the key and
    ``first_seen_at``, set once), and the record class its rows are."""

    def __init__(self, model: type[Any], key: tuple[str, ...], record: Callable[..., R]) -> None:
        self.table = cast("Table", model.__table__)
        self.key = key
        self.record = record
        self.columns = tuple(column.name for column in self.table.c)
        self.changing = tuple(name for name in self.columns if name not in key and name != "first_seen_at")

    def named(self, names: Sequence[str]) -> dict[str, Any]:
        """The columns ``names``, by name, as ``rows_of`` takes them."""
        return {name: self.table.c[name] for name in names}

    def to_record(self, row: Row[Any]) -> R:
        """A record as its row holds it: the columns are the record's fields, by name."""
        return self.record(**dict(row._mapping))

    def values(self, record: R, names: Sequence[str]) -> tuple[Any, ...]:
        """``record``'s values of the columns ``names``."""
        return tuple(getattr(record, name) for name in names)

    def lock_order(self) -> list[ColumnElement[Any]]:
        """The order rows are locked in by everyone who locks them: by key in Python's order (code points, the "C"
        collation), whatever the database's own collation says."""
        return [self.table.c[name].collate("C") for name in self.key]

    def matching(self, other: Any) -> ColumnElement[bool]:
        """The table's key equal to ``other``'s columns of the same names."""
        return and_(*(self.table.c[name] == other.c[name] for name in self.key))


_IPS = _RecordTable(ExitIpReputationModel, tuple(ExitIpKey._fields), ExitIpReputation)
_CONNECTORS = _RecordTable(ConnectorSiteReputationModel, tuple(ConnectorSiteKey._fields), ConnectorSiteReputation)
_MOVES = cast("Table", ConnectorFactorMoveModel.__table__)
# The columns of a factor move, in the order :meth:`ExitReputationRepository._log_moves` writes them.
_MOVE_COLUMNS = (
    "project_id", "site", "place", "connector_id", "at", "old_factor", "new_factor", "requests", "blocked_reports"
)
_AVOID_MOVES = cast("Table", ExitIpAvoidMoveModel.__table__)
# The columns of an avoid move, in the order :meth:`ExitReputationRepository._log_avoid_moves` writes them.
_AVOID_MOVE_COLUMNS = ("project_id", "site", "ip", "at", "old_avoid_until", "new_avoid_until")
# Each project's moves made more than its Keep data for days before :now, none of a project at 0, from a history table.
# A cutoff that differs per project gives the planner no range on the time, so a join of the projects scans the whole
# table every hour; the lateral subquery reads one project's expired rows on its (project_id, at) index instead, its
# OFFSET keeping it from being flattened into that join, and the rows go by their address.
_DELETE_HISTORY_PAST = """
DELETE FROM {table} WHERE ctid = ANY(ARRAY(
    SELECT expired.ctid FROM projects p
    CROSS JOIN LATERAL (
        SELECT m.ctid FROM {table} m
        WHERE m.project_id = p.id
          AND m.at < CAST(:now AS timestamp) - make_interval(days => p.exit_reputation_retention_days)
        OFFSET 0
    ) expired
    WHERE p.exit_reputation_retention_days > 0
))"""


class ExitReputationRepository:
    """The exit reputation records: evidence applied a batch at a time, and the records read back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Writing evidence ------------------------------------------------------------------

    async def apply_reports(self, ips: Mapping[ExitIpKey, ReportCounts], now: datetime) -> ReputationChanges:
        """Apply a batch of reports to the exit IP records as of ``now``, per exit IP and site; what it did to each
        record's avoid.

        A record of a project that is gone or records nothing (``off``) is skipped. A new record is inserted with its
        final values; one already stored is locked by the same statement, in key order, then updated, so two writers
        with overlapping batches wait for each other instead of deadlocking, and neither loses the other's evidence.
        Each avoid whose end changed is kept as an avoid move, by one more statement. Constant statements whatever the
        batch's size.
        """
        if not ips:
            return ReputationChanges()
        await self._plan_for_the_values()
        settings = await self._settings({key.project_id for key in ips})
        changes = await self._apply(
            _IPS,
            sorted(key for key in ips if key.project_id in settings),
            ExitIpReputation.new,
            lambda record: record.apply(ips[record.key], now, settings[record.project_id].policy),
            now,
        )
        await self._log_avoid_moves([change for change in changes if change.old_avoid_until != change.new_avoid_until])
        return ReputationChanges(ips=tuple(changes), avoiding=self._avoiding(settings))

    async def recording_connectors(self, pairs: Collection[tuple[str, str | None]]) -> Recording:
        """The projects of ``pairs`` that record, and the (project, connector) pairs among ``pairs`` whose project records
        and whose connector is that project's: what a report is taken for. A pair without a connector asks after its
        project alone. Each project and connector held against a delete until the transaction ends."""
        settings = await self._settings({project_id for project_id, _connector in pairs})
        owned = await self._owned(
            {(project_id, connector_id) for project_id, connector_id in pairs if connector_id and project_id in settings}
        )
        return Recording(frozenset(settings), frozenset(owned))

    async def recording_projects(self) -> set[str]:
        """The ids of the projects whose exit reputation records, read without a lock: whether a metrics flush has any
        evidence to weigh."""
        result = await self._session.execute(
            select(ProjectModel.id).where(ProjectModel.exit_reputation != ExitReputationMode.OFF.value)
        )
        return set(result.scalars().all())

    async def apply_evidence(
        self,
        evidence: Mapping[ConnectorSiteKey, RequestCount],
        dynamic: Collection[str],
        now: datetime,
        window: timedelta,
    ) -> ReputationChanges:
        """Apply a metrics flush's requests and reports, counted over the ``window`` before ``now``, to the connector
        records, each record decided once; what it did to the factors of the ``dynamic`` connectors, the only ones
        routing weighs by a factor.

        A key with reports makes its record or locks the stored one; a key with requests alone locks the record only
        when one is stored, since requests never make one. A record of a project that is gone or records nothing, or of
        a connector that is gone or another project's, is skipped. The records are made or locked by one statement in
        key order and updated by one: the flush is their one writer, besides a connector's delete and the retention
        sweep, which skips what a writer holds. Each dynamic connector's factor that moved is kept as a factor move, by
        one more statement. Constant statements whatever the number of keys.
        """
        counted = {key: counts for key, counts in evidence.items() if counts.requests or counts.blocked or counts.good}
        if not counted:
            return ReputationChanges()
        settings = await self._settings({key.project_id for key in counted})
        owned = await self._owned({(key.project_id, key.connector_id) for key in counted if key.project_id in settings})
        keys = sorted(key for key in counted if (key.project_id, key.connector_id) in owned)
        if not keys:
            return ReputationChanges(avoiding=self._avoiding(settings))
        await self._plan_for_the_values()
        reported = {key for key in keys if counted[key].blocked or counted[key].good}
        # Read unlocked, so the one statement below still takes every row in key order: a record another flush makes
        # meanwhile misses these requests, and one the sweep deletes meanwhile is made again by them.
        stored = await self._stored_keys(_CONNECTORS, [key for key in keys if key not in reported])
        changes = await self._apply(
            _CONNECTORS,
            [key for key in keys if key in reported or key in stored],
            ConnectorSiteReputation.new,
            lambda record: record.apply_evidence(
                counted[record.key].requests,
                counted[record.key].blocked,
                counted[record.key].good,
                now,
                window,
                settings[record.project_id].policy,
            ),
            now,
        )
        told = tuple(change for change in changes if change.key.connector_id in dynamic)
        await self._log_moves([change for change in told if change.moved])
        return ReputationChanges(connectors=told, avoiding=self._avoiding(settings))

    async def _log_moves(self, moved: Sequence[ConnectorChange]) -> None:
        """Keep each of ``moved`` (dynamic connectors' factors that moved) as a factor move, one statement; a move kept
        already for its key and time stays as it is."""
        if not moved:
            return
        moves = rows_of(
            "moves",
            {name: _MOVES.c[name] for name in _MOVE_COLUMNS},
            (
                (
                    change.key.project_id,
                    change.key.site,
                    change.key.place,
                    change.key.connector_id,
                    change.at,
                    change.old_factor,
                    change.new_factor,
                    change.requests,
                    change.blocked_reports,
                )
                for change in moved
            ),
        )
        await self._session.execute(
            pg_insert(_MOVES)
            .from_select(list(_MOVE_COLUMNS), select(*(moves.c[name] for name in _MOVE_COLUMNS)))
            .on_conflict_do_nothing()
        )

    async def _log_avoid_moves(self, changed: Sequence[IpChange]) -> None:
        """Keep each of ``changed`` (exit IPs' avoids whose end changed) as an avoid move, one statement; a move kept
        already for its key and time stays as it is."""
        if not changed:
            return
        moves = rows_of(
            "moves",
            {name: _AVOID_MOVES.c[name] for name in _AVOID_MOVE_COLUMNS},
            (
                (
                    change.key.project_id,
                    change.key.site,
                    change.key.ip,
                    change.at,
                    change.old_avoid_until,
                    change.new_avoid_until,
                )
                for change in changed
            ),
        )
        await self._session.execute(
            pg_insert(_AVOID_MOVES)
            .from_select(list(_AVOID_MOVE_COLUMNS), select(*(moves.c[name] for name in _AVOID_MOVE_COLUMNS)))
            .on_conflict_do_nothing()
        )

    async def _apply[R: ReputationRecord, C](
        self,
        records: _RecordTable[R],
        keys: Sequence[Any],
        new: Callable[[Any, datetime], R],
        apply: Callable[[R], C],
        now: datetime,
    ) -> list[C]:
        """Apply evidence to the records of ``keys`` (in key order): each decided as a new record and inserted so, or,
        when one is stored, that one locked by the same statement, read, decided and updated."""
        if not keys:
            return []
        fresh = [new(key, now) for key in keys]
        decided = {record.key: apply(record) for record in fresh}
        made = await self._make_or_lock(records, fresh)
        stored = await self._read(records, [key for key in keys if key not in made])
        for record in stored:
            decided[record.key] = apply(record)
        await self._update(records, stored)
        return [decided[key] for key in keys]

    async def _make_or_lock[R: ReputationRecord](self, records: _RecordTable[R], fresh: Sequence[R]) -> set[tuple[Any, ...]]:
        """Insert each of ``fresh`` (in key order) as it stands, or lock the row stored under its key; the keys inserted.

        One statement, a row at a time in the order given, the same for every writer: a writer of an overlapping batch
        waits at the first key the two share until this one commits. The update's condition is never true, so a stored
        row is locked and never written; setting a key column makes that lock the one ``FOR UPDATE`` takes.
        """
        columns = records.columns
        made = rows_of("made", records.named(columns), (records.values(record, columns) for record in fresh))
        inserting = pg_insert(records.table).from_select(list(columns), select(*(made.c[name] for name in columns)))
        first = records.key[0]
        result = await self._session.execute(
            inserting.on_conflict_do_update(
                index_elements=list(records.key),
                set_={first: inserting.excluded[first]},
                where=false(),
            ).returning(*(records.table.c[name] for name in records.key))
        )
        return {tuple(row) for row in result}

    async def _read[R: ReputationRecord](self, records: _RecordTable[R], keys: Sequence[Any]) -> list[R]:
        """The stored records of ``keys``, in key order; the caller holds their rows."""
        if not keys:
            return []
        wanted = rows_of("wanted", records.named(records.key), keys)
        result = await self._session.execute(select(records.table).join(wanted, records.matching(wanted)))
        return sorted((records.to_record(row) for row in result), key=lambda record: record.key)

    async def _stored_keys[R: ReputationRecord](
        self, records: _RecordTable[R], keys: Sequence[Any]
    ) -> set[tuple[Any, ...]]:
        """Those of ``keys`` a record is stored for, read without a lock."""
        if not keys:
            return set()
        wanted = rows_of("wanted", records.named(records.key), keys)
        result = await self._session.execute(
            select(*(records.table.c[name] for name in records.key)).join(wanted, records.matching(wanted))
        )
        return {tuple(row) for row in result}

    async def _update[R: ReputationRecord](self, records: _RecordTable[R], decided: Sequence[R]) -> None:
        """Write the changing columns of ``decided`` over their stored rows, one statement."""
        if not decided:
            return
        columns = (*records.key, *records.changing)
        values = rows_of("decided", records.named(columns), (records.values(record, columns) for record in decided))
        await self._session.execute(
            update(records.table)
            .where(records.matching(values))
            .values({name: values.c[name] for name in records.changing})
        )

    async def _plan_for_the_values(self) -> None:
        """Plan the transaction's further statements for their own arrays and filters, never from a cached generic plan:
        a generic plan cannot tell a batch of three keys from one of thousands, nor an IP's start from no filter."""
        await self._session.execute(select(func.set_config("plan_cache_mode", "force_custom_plan", True)))

    async def _settings(self, project_ids: Collection[str]) -> dict[str, _Settings]:
        """The settings of each project among ``project_ids`` that exists and records, its row held against a delete
        until the transaction ends."""
        if not project_ids:
            return {}
        p = ProjectModel
        result = await self._session.execute(
            select(
                p.id,
                p.exit_reputation,
                p.exit_reputation_avoid_minutes,
                p.exit_reputation_half_life_hours,
                p.exit_reputation_connector_half_life_requests,
                p.exit_reputation_connector_half_life_minutes,
                p.exit_reputation_bad_reports,
                p.exit_reputation_bad_share,
                p.exit_reputation_strength,
            )
            .where(any_of(p.id, sorted(project_ids)))
            .with_for_update(read=True, key_share=True)
        )
        settings: dict[str, _Settings] = {}
        for row in result:
            mode = ExitReputationMode(row.exit_reputation or "off")
            if mode.records:
                policy = ReputationPolicy.of_settings(
                    avoid_minutes=row.exit_reputation_avoid_minutes,
                    half_life_hours=row.exit_reputation_half_life_hours,
                    connector_half_life_requests=row.exit_reputation_connector_half_life_requests,
                    connector_half_life_minutes=row.exit_reputation_connector_half_life_minutes,
                    bad_reports=row.exit_reputation_bad_reports,
                    bad_share=row.exit_reputation_bad_share,
                    strength=row.exit_reputation_strength,
                )
                settings[row.id] = _Settings(policy, mode.avoids)
        return settings

    async def _owned(self, pairs: Collection[tuple[str, str]]) -> set[tuple[str, str]]:
        """The (project, connector) pairs among ``pairs`` whose connector exists in that project, each connector held
        against a delete until the transaction ends.

        Taken before any record is locked: a connector's delete holds the connector and then takes its records, so a
        writer that waits for it holds none of them.
        """
        if not pairs:
            return set()
        c = ConnectorModel
        result = await self._session.execute(
            select(c.project_id, c.id)
            .where(any_of(c.id, sorted({connector_id for _project, connector_id in pairs})))
            .with_for_update(read=True, key_share=True)
        )
        return {(project_id, connector_id) for project_id, connector_id in result.all()} & set(pairs)

    @staticmethod
    def _avoiding(settings: Mapping[str, _Settings]) -> frozenset[str]:
        return frozenset(project_id for project_id, setting in settings.items() if setting.avoids)

    # --- Retention -----------------------------------------------------------------------------

    async def delete_stale(self, now: datetime) -> tuple[int, int]:
        """Delete each project's records last seen on a day more than its Keep data for days before ``now``, none of a
        project at 0 days, an exit IP's only with no avoid ahead of ``now``; how many went from each table (exit IPs,
        connectors).

        A record another transaction holds is skipped, never waited for, so the sweep and a writer cannot deadlock; it
        goes with a later sweep. One statement per table, joined to the projects for their days.
        """
        days = ProjectModel.exit_reputation_retention_days
        fewest = (await self._session.execute(select(func.min(days)).where(days > 0))).scalar_one_or_none()
        if fewest is None:
            return 0, 0
        # The cutoff of the project with the fewest days, as a constant, gives the planner a range on the day's index;
        # the join then applies each project's own days.
        before = now.date() - timedelta(days=fewest)
        # An end that is not set compares as unknown: not being ahead is not being true.
        ips = await self._delete_stale(_IPS, now, before, (_IPS.table.c.avoid_until > now).is_not(True))
        connectors = await self._delete_stale(_CONNECTORS, now, before)
        return ips, connectors

    async def _delete_stale[R: ReputationRecord](
        self, records: _RecordTable[R], now: datetime, before: date, *also: ColumnElement[bool]
    ) -> int:
        """Delete ``records``' rows last seen before ``before`` and past their project's Keep data for that also match
        ``also``, locking only the rows themselves; how many went."""
        table = records.table
        key = tuple_(*(table.c[name] for name in records.key))
        days = ProjectModel.exit_reputation_retention_days
        stale = (
            select(*(table.c[name] for name in records.key))
            .join(ProjectModel, ProjectModel.id == table.c.project_id)
            .where(
                days > 0,
                table.c.last_seen_day < before,
                table.c.last_seen_day < literal(now.date(), Date) - days,
                *also,
            )
            .with_for_update(of=table, skip_locked=True)
        )
        result = await self._session.execute(delete(table).where(key.in_(stale)))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_history_past(self, now: datetime) -> tuple[int, int]:
        """Delete each project's factor moves and avoid moves made more than its Keep data for days before ``now``, none
        of a project at 0 days; how many went (factor moves, avoid moves). One statement per table, reading each
        project's expired rows alone (:data:`_DELETE_HISTORY_PAST`)."""
        deleted: list[int] = []
        for table in (_MOVES, _AVOID_MOVES):
            result = await self._session.execute(text(_DELETE_HISTORY_PAST.format(table=table.name)), {"now": now})
            deleted.append(int(result.rowcount or 0))  # type: ignore[attr-defined]
        return deleted[0], deleted[1]

    # --- Reads ------------------------------------------------------------------------------------

    async def ip_record(self, key: ExitIpKey) -> ExitIpReputation | None:
        """The exit IP record of ``key``, or None when nothing was reported for it."""
        return await self._get(_IPS, key)

    async def connector_record(self, key: ConnectorSiteKey) -> ConnectorSiteReputation | None:
        """The connector record of ``key``, or None when nothing was reported for it."""
        return await self._get(_CONNECTORS, key)

    async def _get[R: ReputationRecord](self, records: _RecordTable[R], key: tuple[str, ...]) -> R | None:
        table = records.table
        result = await self._session.execute(
            select(table).where(*(table.c[name] == value for name, value in zip(records.key, key, strict=True)))
        )
        row = result.one_or_none()
        return records.to_record(row) if row is not None else None

    async def ip_avoids(
        self,
        exits: Mapping[str, Collection[str]],
        now: datetime,
        *,
        per_project: int,
        per_ip: int = MAX_EXIT_SITES,
    ) -> list[ExitIpReputation]:
        """The records of each project's exit IPs in ``exits`` whose avoid is still ahead of ``now``: what the rows of
        projects that avoid are kept away from sites by. At most ``per_ip`` sites of one IP and ``per_project`` records
        of one project, the avoids ending last first.

        One statement whatever the number of IPs, on the first two columns of the primary key.
        """
        wanted_rows = [(project_id, ip) for project_id in sorted(exits) for ip in sorted(exits[project_id])]
        if not wanted_rows:
            return []
        await self._plan_for_the_values()
        table = _IPS.table
        c = table.c
        wanted = rows_of("wanted", _IPS.named(("project_id", "ip")), wanted_rows)
        latest_first = (c.avoid_until.desc(), c.site)
        matched = (
            select(
                table,
                func.row_number().over(partition_by=(c.project_id, c.ip), order_by=latest_first).label("ip_rank"),
            )
            .join(wanted, and_(c.project_id == wanted.c.project_id, c.ip == wanted.c.ip))
            .where(c.avoid_until > now)
            .subquery("matched")
        )
        m = matched.c
        kept = (
            select(
                *(m[column.name] for column in c),
                func.row_number()
                .over(partition_by=m.project_id, order_by=(m.avoid_until.desc(), m.ip, m.site))
                .label("project_rank"),
            )
            .where(m.ip_rank <= per_ip)
            .subquery("kept")
        )
        k = kept.c
        result = await self._session.execute(
            select(*(k[column.name] for column in c)).where(k.project_rank <= per_project)
        )
        return [_IPS.to_record(row) for row in result]

    async def connector_factors(self, connector_ids: Collection[str]) -> list[ConnectorSiteReputation]:
        """The records of ``connector_ids`` that store a factor, in key order: every site and place level whose record
        holds enough requests to tell. One statement."""
        if not connector_ids:
            return []
        await self._plan_for_the_values()
        table = _CONNECTORS.table
        result = await self._session.execute(
            select(table)
            .where(any_of(table.c.connector_id, sorted(set(connector_ids))), table.c.factor.is_not(None))
            .order_by(*_CONNECTORS.lock_order())
        )
        return [_CONNECTORS.to_record(row) for row in result]

    async def factor_moves(
        self, project_id: str, site: str, places: Collection[str], connector_ids: Collection[str], since: datetime
    ) -> list[ConnectorChange]:
        """The moves of ``connector_ids``' factors for ``site`` at each of ``places`` (levels of a requested place) made
        at ``since`` or later, the earliest first. One statement on the key."""
        if not places or not connector_ids:
            return []
        await self._plan_for_the_values()
        c = _MOVES.c
        result = await self._session.execute(
            select(_MOVES)
            .where(
                c.project_id == project_id,
                c.site == site,
                any_of(c.place, sorted(set(places))),
                any_of(c.connector_id, sorted(set(connector_ids))),
                c.at >= since,
            )
            .order_by(c.at, c.connector_id, c.place)
        )
        return [_move(row) for row in result]

    async def last_moves_before(
        self, project_id: str, site: str, places: Collection[str], connector_ids: Collection[str], before: datetime
    ) -> list[ConnectorChange]:
        """The last move before ``before`` of each of ``connector_ids``' factors for ``site`` at each of ``places``, where
        there is one: the counts the first move from ``before`` on is told against. One statement, one step back on the
        key per connector and level."""
        if not places or not connector_ids:
            return []
        await self._plan_for_the_values()
        c = _MOVES.c
        wanted = rows_of(
            "wanted",
            {"place": c.place, "connector_id": c.connector_id},
            [(place, connector_id) for place in sorted(set(places)) for connector_id in sorted(set(connector_ids))],
        )
        last = (
            select(_MOVES)
            .where(
                c.project_id == project_id,
                c.site == site,
                c.place == wanted.c.place,
                c.connector_id == wanted.c.connector_id,
                c.at < before,
            )
            .order_by(c.at.desc())
            .limit(1)
            .lateral("last")
        )
        result = await self._session.execute(select(last).select_from(wanted).join(last, true()))
        return sorted((_move(row) for row in result), key=lambda move: (move.at, move.key))

    async def avoid_moves(
        self, project_id: str, site: str, ips: Collection[str], since: datetime
    ) -> list[IpChange]:
        """The moves of ``ips``' avoids for ``site`` made at ``since`` or later, the earliest first. One statement on the
        key."""
        if not ips:
            return []
        await self._plan_for_the_values()
        c = _AVOID_MOVES.c
        result = await self._session.execute(
            select(_AVOID_MOVES)
            .where(c.project_id == project_id, c.site == site, any_of(c.ip, sorted(set(ips))), c.at >= since)
            .order_by(c.at, c.ip)
        )
        return [
            IpChange(ExitIpKey(row.project_id, row.ip, row.site), row.at, row.old_avoid_until, row.new_avoid_until)
            for row in result
        ]

    async def avoid_ends(self, project_id: str, site: str, ips: Collection[str]) -> dict[str, datetime]:
        """The end of the last avoid of each of ``ips`` that has one for ``site``, past or ahead. One statement on the
        key."""
        if not ips:
            return {}
        await self._plan_for_the_values()
        c = _IPS.table.c
        result = await self._session.execute(
            select(c.ip, c.avoid_until).where(
                c.project_id == project_id, c.site == site, any_of(c.ip, sorted(set(ips))), c.avoid_until.is_not(None)
            )
        )
        return {row.ip: row.avoid_until for row in result}

    async def first_move_at(self, project_id: str) -> datetime | None:
        """When the project's earliest move kept was made, a factor's or an avoid's; None before any. One statement, a
        step on each table's index of the project's times."""
        first: datetime | None = (
            await self._session.execute(
                select(
                    func.least(
                        select(func.min(_MOVES.c.at)).where(_MOVES.c.project_id == project_id).scalar_subquery(),
                        select(func.min(_AVOID_MOVES.c.at))
                        .where(_AVOID_MOVES.c.project_id == project_id)
                        .scalar_subquery(),
                    )
                )
            )
        ).scalar_one()
        return first

    async def avoided_ips(self, project_id: str, sites: Collection[str], now: datetime) -> dict[str, frozenset[str]]:
        """Each of ``sites`` that avoids exit IPs of the project at ``now``, with those IPs. One statement."""
        if not sites:
            return {}
        await self._plan_for_the_values()
        table = _IPS.table
        c = table.c
        result = await self._session.execute(
            select(c.site, c.ip).where(
                c.project_id == project_id, any_of(c.site, sorted(set(sites))), self._avoided(table, now)
            )
        )
        avoided: dict[str, set[str]] = {}
        for site, ip in result:
            avoided.setdefault(site, set()).add(ip)
        return {site: frozenset(ips) for site, ips in avoided.items()}

    async def connector_records(
        self,
        project_id: str,
        sites: Collection[str],
        places: Collection[str] | None,
        *,
        connector_ids: Collection[str] | None = None,
    ) -> list[ConnectorSiteReputation]:
        """The project's connector records for each of ``sites`` at each of ``places`` (levels of a requested place, ""
        for none; None for every place), of ``connector_ids`` alone when given, in key order. One statement; with the
        connectors named it reads a prefix of the key."""
        if not sites or (places is not None and not places) or (connector_ids is not None and not connector_ids):
            return []
        await self._plan_for_the_values()
        table = _CONNECTORS.table
        c = table.c
        clauses = [c.project_id == project_id, any_of(c.site, sorted(set(sites)))]
        if connector_ids is not None:
            clauses.append(any_of(c.connector_id, sorted(set(connector_ids))))
        if places is not None:
            clauses.append(any_of(c.place, sorted(set(places))))
        result = await self._session.execute(
            select(table).where(*clauses).order_by(*_CONNECTORS.lock_order())
        )
        return [_CONNECTORS.to_record(row) for row in result]

    async def busiest_sites(self, project_id: str, *, limit: int | None = None) -> list[tuple[str, int]]:
        """The sites the project's connector records are kept for, each with the reports made on it, the most reported
        first; at most ``limit`` (None: every one).

        A report naming a connector is kept at every level of its place, so it is counted at the coarsest alone (its
        country, or "" for none): those levels' counts add up to the reports.
        """
        c = _CONNECTORS.table.c
        reports = func.sum(c.blocked_reports + c.good_reports)
        result = await self._session.execute(
            select(c.site, reports)
            .where(c.project_id == project_id, c.place.not_like("%/%"))
            .group_by(c.site)
            .order_by(reports.desc(), c.site)
            .limit(limit)
        )
        return [(site, int(count)) for site, count in result]

    async def recorded_sites(self, project_id: str, connector_ids: Collection[str]) -> set[str]:
        """The sites any of ``connector_ids`` holds a record for in the project. One statement on a prefix of the key,
        at the coarsest level alone: a record at a finer one comes with it."""
        if not connector_ids:
            return set()
        await self._plan_for_the_values()
        c = _CONNECTORS.table.c
        result = await self._session.execute(
            select(c.site)
            .distinct()
            .where(
                c.project_id == project_id,
                any_of(c.connector_id, sorted(set(connector_ids))),
                c.place.not_like("%/%"),
            )
        )
        return set(result.scalars().all())

    async def recent_sites(self, project_id: str, since: datetime, *, limit: int) -> list[str]:
        """The sites with a connector record seen since ``since``, the most reported first; at most ``limit``. One
        statement.

        Counted at the coarsest level alone, as :meth:`busiest_sites` counts them.
        """
        c = _CONNECTORS.table.c
        reports = func.sum(c.blocked_reports + c.good_reports)
        result = await self._session.execute(
            select(c.site)
            .where(c.project_id == project_id, c.place.not_like("%/%"), c.last_seen_at >= since)
            .group_by(c.site)
            .order_by(reports.desc(), c.site)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def coarse_records(
        self, project_id: str, sites: Collection[str], since: datetime
    ) -> list[ConnectorSiteReputation]:
        """The project's connector records for ``sites`` at a country or no place, seen since ``since``. One statement."""
        if not sites:
            return []
        await self._plan_for_the_values()
        table = _CONNECTORS.table
        c = table.c
        result = await self._session.execute(
            select(table)
            .where(
                c.project_id == project_id,
                any_of(c.site, sorted(set(sites))),
                c.place.not_like("%/%"),
                c.last_seen_at >= since,
            )
            .order_by(*_CONNECTORS.lock_order())
        )
        return [_CONNECTORS.to_record(row) for row in result]

    # The same reads for several projects at once, as the alerts job reads every project's: one statement each.

    async def recent_sites_of(
        self, project_ids: Collection[str], since: datetime, *, limit: int
    ) -> dict[str, list[str]]:
        """Each project's sites with a connector record seen since ``since``, the most reported first, at most ``limit``
        each; counted at the coarsest level as :meth:`recent_sites` counts them. A project with none is absent."""
        if not project_ids:
            return {}
        c = _CONNECTORS.table.c
        reports = func.sum(c.blocked_reports + c.good_reports)
        ranked = (
            select(
                c.project_id,
                c.site,
                func.row_number().over(partition_by=c.project_id, order_by=(reports.desc(), c.site)).label("rank"),
            )
            .where(any_of(c.project_id, sorted(set(project_ids))), c.place.not_like("%/%"), c.last_seen_at >= since)
            .group_by(c.project_id, c.site)
            .subquery("ranked")
        )
        result = await self._session.execute(
            select(ranked.c.project_id, ranked.c.site)
            .where(ranked.c.rank <= limit)
            .order_by(ranked.c.project_id, ranked.c.rank)
        )
        sites: dict[str, list[str]] = {}
        for project_id, site in result:
            sites.setdefault(project_id, []).append(site)
        return sites

    async def avoided_ips_of(
        self, sites: Collection[tuple[str, str]], now: datetime
    ) -> dict[tuple[str, str], frozenset[str]]:
        """Each (project, site) of ``sites`` that avoids exit IPs at ``now``, with those IPs."""
        if not sites:
            return {}
        await self._plan_for_the_values()
        table = _IPS.table
        c = table.c
        wanted = rows_of("wanted", _IPS.named(("project_id", "site")), sorted(set(sites)))
        result = await self._session.execute(
            select(c.project_id, c.site, c.ip)
            .join(wanted, and_(c.project_id == wanted.c.project_id, c.site == wanted.c.site))
            .where(self._avoided(table, now))
        )
        avoided: dict[tuple[str, str], set[str]] = {}
        for project_id, site, ip in result:
            avoided.setdefault((project_id, site), set()).add(ip)
        return {key: frozenset(ips) for key, ips in avoided.items()}

    async def connector_records_of(
        self, sites: Collection[tuple[str, str]], places: Collection[str]
    ) -> list[ConnectorSiteReputation]:
        """The connector records of each (project, site) of ``sites`` at each of ``places``, in key order."""
        if not sites or not places:
            return []
        await self._plan_for_the_values()
        table = _CONNECTORS.table
        c = table.c
        wanted = rows_of("wanted", _CONNECTORS.named(("project_id", "site")), sorted(set(sites)))
        result = await self._session.execute(
            select(table)
            .join(wanted, and_(c.project_id == wanted.c.project_id, c.site == wanted.c.site))
            .where(any_of(c.place, sorted(set(places))))
            .order_by(*_CONNECTORS.lock_order())
        )
        return [_CONNECTORS.to_record(row) for row in result]

    async def coarse_records_of(
        self, sites: Collection[tuple[str, str]], since: datetime
    ) -> list[ConnectorSiteReputation]:
        """The connector records of each (project, site) of ``sites`` at a country or no place, seen since ``since``."""
        if not sites:
            return []
        await self._plan_for_the_values()
        table = _CONNECTORS.table
        c = table.c
        wanted = rows_of("wanted", _CONNECTORS.named(("project_id", "site")), sorted(set(sites)))
        result = await self._session.execute(
            select(table)
            .join(wanted, and_(c.project_id == wanted.c.project_id, c.site == wanted.c.site))
            .where(c.place.not_like("%/%"), c.last_seen_at >= since)
            .order_by(*_CONNECTORS.lock_order())
        )
        return [_CONNECTORS.to_record(row) for row in result]

    async def busiest_places(
        self, project_id: str, site: str | None, *, limit: int | None = None
    ) -> list[tuple[str, int]]:
        """The requested places the project's connector records are kept at, for ``site`` or for any, each with the
        reports made there, the most reported first; at most ``limit`` (None: every one), and never "" (no place)."""
        c = _CONNECTORS.table.c
        reports = func.sum(c.blocked_reports + c.good_reports)
        clauses = [c.project_id == project_id, c.place != ""]
        if site is not None:
            clauses.append(c.site == site)
        result = await self._session.execute(
            select(c.place, reports).where(*clauses).group_by(c.place).order_by(reports.desc(), c.place).limit(limit)
        )
        return [(place, int(count)) for place, count in result]

    async def page(
        self,
        project_id: str,
        now: datetime,
        *,
        ip: str | None = None,
        site: str | None = None,
        status: ReputationStatus | None = None,
        sort: ReputationSort = ReputationSort.LAST_SEEN,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ExitIpReputation], int, bool]:
        """One page of a project's exit IP records under the filters, how many match, and whether that count is exact.

        ``ip`` is a whole IP, matched exactly, or the start of one; ``site`` any part of the site's name; ``status``
        what holds at ``now``. The total is counted no further than ``COUNT_CAP`` rows past the page.
        """
        await self._plan_for_the_values()
        table = _IPS.table
        clauses = self._filters(table, project_id, now, ip=ip, site=site, status=status)
        result = await self._session.execute(
            select(table).where(*clauses).order_by(*self._order(table, sort)).offset(offset).limit(limit)
        )
        found = [_IPS.to_record(row) for row in result]
        total, exact = await self._capped_total(
            select(literal_column("1")).select_from(table).where(*clauses),
            offset=offset,
            limit=limit,
            found=len(found),
        )
        return found, total, exact

    async def of_ip(self, project_id: str, ip: str, now: datetime, *, limit: int) -> list[ExitIpReputation]:
        """Up to ``limit`` of the sites' records of ``ip`` (canonical) in the project: those avoiding it at ``now``
        first, then the one seen last first.

        An IP that served a wide crawl may have a record for many sites; the ones in force come first.
        """
        table = _IPS.table
        c = table.c
        result = await self._session.execute(
            select(table)
            .where(c.project_id == project_id, c.ip == ip)
            .order_by(case((self._avoided(table, now), 0), else_=1), c.last_seen_at.desc(), c.site)
            .limit(limit)
        )
        return [_IPS.to_record(row) for row in result]

    async def summaries(self, project_id: str, ips: Collection[str], now: datetime) -> dict[str, ReputationSummary]:
        """Each IP among ``ips`` (canonical) that has a record in the project, summed over its sites as of ``now``.

        One statement whatever the number of IPs, on the first two columns of the primary key.
        """
        if not ips:
            return {}
        await self._plan_for_the_values()
        table = _IPS.table
        avoided = self._avoided(table, now)
        result = await self._session.execute(
            select(
                table.c.ip,
                func.sum(table.c.good_reports),
                func.sum(table.c.bad_reports),
                func.count(),
                func.count().filter(avoided),
                func.max(table.c.avoid_until).filter(avoided),
            )
            .where(table.c.project_id == project_id, any_of(table.c.ip, sorted(ips)))
            .group_by(table.c.ip)
        )
        return {row[0]: ReputationSummary(row[0], int(row[1]), int(row[2]), row[3], row[4], row[5]) for row in result}

    @classmethod
    def _filters(
        cls,
        table: Table,
        project_id: str,
        now: datetime,
        *,
        ip: str | None,
        site: str | None,
        status: ReputationStatus | None,
    ) -> list[ColumnElement[bool]]:
        """The clauses of :meth:`page`'s filters."""
        clauses = [table.c.project_id == project_id]
        if ip:
            whole = canonical_ip(ip)
            clauses.append(
                table.c.ip == whole
                if whole is not None
                else table.c.ip.startswith(ip.strip().lower(), autoescape=True)
            )
        if site:
            clauses.append(table.c.site.contains(_site_text(site), autoescape=True))
        if status is ReputationStatus.AVOIDED:
            clauses.append(cls._avoided(table, now))
        elif status is ReputationStatus.NONE:
            # An end that is not set compares as unknown, not false: not being avoided is not being true.
            clauses.append(cls._avoided(table, now).is_not(True))
        return clauses

    @staticmethod
    def _avoided(table: Table, now: datetime) -> ColumnElement[bool]:
        """The records whose IP is avoided for the site at ``now``."""
        return table.c.avoid_until > now

    @staticmethod
    def _order(table: Table, sort: ReputationSort) -> list[ColumnElement[Any]]:
        """The order of :meth:`page`: ``sort``'s, then the one seen last first, then by IP and site, so pages never
        overlap."""
        c = table.c
        settled: list[ColumnElement[Any]] = [c.last_seen_at.desc(), c.ip, c.site]
        if sort is ReputationSort.REPORTS:
            return [(c.good_reports + c.bad_reports).desc(), *settled]
        return settled

    async def _capped_total(self, matching: Select[Any], *, offset: int, limit: int, found: int) -> tuple[int, bool]:
        """How many rows ``matching`` selects, counted at most ``COUNT_CAP`` past ``offset``, and whether that is exact,
        as the exit IPs view counts.

        Short of exact the total is ``offset + COUNT_CAP``, a lower bound that always leaves a next page; a short page
        is the end and counts nothing, and a page past the end is recounted from the start.
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


def _move(row: Row[Any]) -> ConnectorChange:
    """A factor move as its row holds it."""
    return ConnectorChange(
        ConnectorSiteKey(row.project_id, row.connector_id, row.site, row.place),
        row.at,
        row.old_factor,
        row.new_factor,
        row.requests,
        row.blocked_reports,
    )


def _site_text(text: str) -> str:
    """Part of a site's name as records keep sites: lower case, with Unicode labels in their ASCII form."""
    name = text.strip().lower()
    if name.isascii():
        return name
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name
