# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install-wide lines of Needs attention, which every signed-in user sees on the Projects page in plain words.

Read from the heartbeats and stream positions in Redis, cheap enough for a poll every 30 seconds, and a handful of IP
database rows from Postgres; never the System page's full read. A line carries no deployment detail: no instance, host,
version, size or raw error, so it reads the same for every role.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog
from redis.exceptions import RedisError

from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind, Role
from api.db.geo_repository import GeoDatabaseRepository
from api.db.redis import CHANGES_STREAM_KEY, FEED_STREAM_KEY, JOB_STREAM_KEY, StreamId, StreamInfo
from api.geo.updater import next_update_at
from api.models.attention import InstallAttention, InstallAttentionLine, Severity

if TYPE_CHECKING:
    from api.core.system_stats import ClusterReader, ClusterReading
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.geo.models import GeoDatabaseRecord
    from api.models.system import (
        ControllerSnapshot,
        GatewaySnapshot,
        InstanceInfo,
        WorkStream,
        WorkStreams,
    )

logger = structlog.get_logger()

type _AdminPage = Literal["system", "geo"]

# How long the Redis reads may take before the live data store counts as unreachable.
INSTALL_READ_TIMEOUT_SECONDS = 5.0
# A gateway this young may still be starting: no line says none serves before every one is older.
GATEWAY_START_SECONDS = 120.0
# Changes, or a feed publish a gateway missed, this old have not taken effect.
CHANGES_LATE_SECONDS = 60.0
# The scheduled producers append the index hygiene job every minute; this long without one, scheduling stopped.
SCHEDULING_SILENT_SECONDS = 180.0
# A work stream nobody takes is behind once its work waits this long.
NO_TAKER_SECONDS = 90.0
# Runs that fail in a row before a job kind counts as behind.
FAILING_RUNS = 3
# A gateway's IP database counts as not loaded once the gateway and the database's record are this old.
DATABASE_LOAD_SECONDS = 300.0
# What a gateway's readiness reasons say while its node is not serving (api.roles.node._Phase).
_NOT_SERVING = frozenset({"not started", "starting", "stopping", "stopped"})
_GATEWAY_ROLES = frozenset({Role.GATEWAY.value, Role.ALL.value})
_POSTGRES_UNREACHABLE = "Postgres unreachable"
_HYGIENE_STREAM = JOB_STREAM_KEY.format(kind=JobKind.INDEX_HYGIENE)


@dataclass(frozen=True)
class _Area:
    """A part of the background work as the line names it, and when its streams are behind."""

    name: str
    # Its work streams by name (WORK_STREAMS); the job kind of each, None for an ingest stream any worker takes.
    streams: tuple[tuple[str, JobKind | None], ...]
    # Its oldest entry waiting this long is behind.
    oldest_seconds: float
    # Whether a job kind failing its runs in a row is behind; a vendor refusing a sync fails it on purpose.
    counts_failures: bool = True
    # Whether a stopped scheduling leaves it behind: the leader's producers append its work.
    scheduled: bool = False


_AREAS: tuple[_Area, ...] = (
    _Area("proxy health checks", (("jobs.health", JobKind.HEALTH),), 180.0, scheduled=True),
    _Area("vendor proxy lists", (("jobs.sync", JobKind.SYNC),), 900.0, counts_failures=False, scheduled=True),
    _Area("cloud scaling", (("jobs.scale", JobKind.SCALE),), 900.0, scheduled=True),
    _Area("new countries", (("jobs.provision", JobKind.PROVISION),), 120.0),
    _Area("traffic limits", (("jobs.traffic", JobKind.TRAFFIC),), 180.0, scheduled=True),
    _Area("charts", (("jobs.metrics_flush", JobKind.METRICS_FLUSH),), 300.0, scheduled=True),
    _Area(
        "location checks",
        (
            ("jobs.reattribute", JobKind.REATTRIBUTE),
            ("jobs.enrich", JobKind.ENRICH),
            ("sightings", None),
            ("observations", None),
        ),
        600.0,
    ),
    _Area("exit reports", (("exit_reports", None),), 300.0, counts_failures=False),
)

_TEXTS = {
    "no_gateway_serving": "No gateway is accepting proxy connections right now, so requests through Octoprox fail.",
    "live_data": (
        "Octoprox cannot reach its live data store. Requests still go through but may be slower, and live numbers may "
        "be missing."
    ),
    "database": (
        "Octoprox cannot reach its database. Proxy traffic keeps flowing, but changes cannot be saved and proxy health "
        "and charts are not updating."
    ),
    "changes_since": "Changes since {time} have not taken effect yet. Requests still follow the earlier setup.",
    "changes": "Recent changes have not taken effect yet. Requests still follow the earlier setup.",
    "ip_load": "An IP database could not be loaded, so location checks may be less accurate.",
    "ip_update": "An IP database could not be updated, so location data may be getting older.",
    "ip_both": "IP databases could not be loaded or updated, so location checks may be less accurate.",
}


@dataclass(frozen=True)
class _Positions:
    """What the second round trip read: Redis's clock, the index hygiene stream, the oldest change after the leader's
    position and the feed stream's entries (each None when not asked for)."""

    now_ms: int
    hygiene: StreamInfo | None
    next_change: str | None
    feed: list[tuple[str, str]] | None


class InstallAttentionReader:
    """The install-wide lines every signed-in user sees on the Projects page, read from the heartbeats in Redis.

    Args:
        geo_updater_interval: seconds between the ``geo_update`` job's looks, past which a due download is late.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        session_factory: SessionFactory,
        cluster: ClusterReader,
        *,
        geo_updater_interval: float,
    ) -> None:
        self._redis_client = redis_client
        self._session_factory = session_factory
        self._cluster = cluster
        self._geo_updater_interval = geo_updater_interval

    async def read(self) -> InstallAttention:
        """The lines now, problems first; Redis not answering is itself a line, alone."""
        now = utc_now()
        try:
            async with asyncio.timeout(INSTALL_READ_TIMEOUT_SECONDS):
                reading, streams = await asyncio.gather(self._cluster.read(), self._cluster.streams())
                if reading.error is not None or streams.error is not None:
                    raise ConnectionError(reading.error or streams.error)
                positions = await self._positions(reading)
        except (TimeoutError, RedisError, OSError) as exc:
            logger.warning("Install attention unavailable: Redis not answering", error=error_text(exc))
            return InstallAttention(generated_at=now, lines=[_line("store_unreachable:live_data", _TEXTS["live_data"])])
        lines = _Lines(reading, streams, positions)
        database_down = lines.database_unreachable()
        records = None if database_down else await self._databases()
        found: list[InstallAttentionLine] = []
        if lines.no_gateway_serving():
            found.append(_line("no_gateway_serving", _TEXTS["no_gateway_serving"], Severity.PROBLEM))
        if database_down:
            found.append(_line("store_unreachable:database", _TEXTS["database"]))
        else:
            found.extend(lines.changes_not_applied())
            found.extend(lines.background_behind())
        if records is not None:
            found.extend(lines.ip_databases(records, now, self._geo_updater_interval))
        return InstallAttention(generated_at=now, lines=found)

    async def _positions(self, reading: ClusterReading) -> _Positions:
        """Redis's clock, the index hygiene stream, and where the leader and the gateways stand on the changes and feed
        streams, in one pipeline; raises when Redis does not answer."""
        # The leader's position on the changes stream, when entries came after it.
        after = _compiled_behind(reading)
        pipe = self._redis_client.client.pipeline(transaction=False)
        pipe.time()
        pipe.xinfo_stream(_HYGIENE_STREAM)
        if after is not None:
            pipe.xrange(CHANGES_STREAM_KEY, min=f"({after}", count=1)
        missed = reading.head is not None and any(
            gateway.feed_digest != reading.head.digest for gateway in _serving(reading.instances)
        )
        if missed:
            pipe.xrange(FEED_STREAM_KEY, "-", "+")
        replies = await pipe.execute(raise_on_error=False)
        clock, hygiene, *rest = replies
        if isinstance(clock, Exception):
            raise clock
        seconds, microseconds = clock
        next_change: str | None = None
        if after is not None:
            entries = _checked(rest.pop(0))
            next_change = str(entries[0][0]) if entries else None
        feed: list[tuple[str, str]] | None = None
        if missed:
            feed = [(str(entry_id), str(fields.get("digest", ""))) for entry_id, fields in _checked(rest.pop(0))]
        return _Positions(
            now_ms=int(seconds) * 1000 + int(microseconds) // 1000,
            hygiene=StreamInfo.of(hygiene),
            next_change=next_change,
            feed=feed,
        )

    async def _databases(self) -> list[GeoDatabaseRecord] | None:
        """The stored IP databases; None, logged, when Postgres does not answer."""
        try:
            async with self._session_factory() as session:
                return await GeoDatabaseRepository(session).get_all()
        except Exception as exc:
            logger.warning("Install attention: IP databases not read", error=error_text(exc))
            return None


class _Lines:
    """The rules over one reading of the cluster."""

    def __init__(self, reading: ClusterReading, streams: WorkStreams, positions: _Positions) -> None:
        self._reading = reading
        self._streams = streams
        self._positions = positions
        self._holder = _leader_instance(reading)
        self._leader = _leader(self._holder)

    def database_unreachable(self) -> bool:
        """A live heartbeat says Postgres does not answer."""
        return any(
            instance.snapshot is not None and _POSTGRES_UNREACHABLE in instance.snapshot.reasons
            for instance in self._reading.instances
        )

    def no_gateway_serving(self) -> bool:
        """No gateway serves, and none might be about to: none whose snapshot cannot be read (an upgrade under way),
        and none started under two minutes ago."""
        gateways = [instance for instance in self._reading.instances if instance.role in _GATEWAY_ROLES]
        if _serving(gateways) or any(instance.snapshot is None for instance in gateways):
            return False
        return all(
            instance.snapshot is not None and instance.snapshot.runtime.uptime_seconds >= GATEWAY_START_SECONDS
            for instance in gateways
        )

    def changes_not_applied(self) -> list[InstallAttentionLine]:
        """No leader, its compile or publish failing, or changes it has not compiled, or a gateway has not applied, for
        a minute or more."""
        leader = self._leader
        stalled = self._holder is None or (
            leader is not None and (leader.compile_error is not None or leader.publish_error is not None)
        )
        late = [at for at in (self._changes_waiting(), self._feed_missed()) if at is not None]
        if late:
            at = _datetime(min(late))
            return [_line("changes_not_applied", _TEXTS["changes_since"], at=at)]
        return [_line("changes_not_applied", _TEXTS["changes"])] if stalled else []

    def background_behind(self) -> list[InstallAttentionLine]:
        """Each area of background work stopped or far behind, named in one line."""
        scheduling_stopped = self._scheduling_stopped()
        behind = [area.name for area in _AREAS if (scheduling_stopped and area.scheduled) or self._behind(area)]
        if not behind:
            return []
        verb = "is" if len(behind) == 1 else "are"
        return [_line("background_behind", f"Background work is behind: {_joined(behind)} {verb} not up to date.")]

    def ip_databases(
        self, records: Sequence[GeoDatabaseRecord], now: datetime, updater_interval: float
    ) -> list[InstallAttentionLine]:
        """An IP database a gateway up five minutes cannot load, or one whose scheduled update fails or is late."""
        by_id = {record.id: record for record in records}
        settled = now - timedelta(seconds=DATABASE_LOAD_SECONDS)
        load = False
        for gateway, uptime in _serving_with_uptime(self._reading.instances):
            if uptime < DATABASE_LOAD_SECONDS:
                continue
            for database_id in {*gateway.geo_database_errors, *gateway.geo_databases_missing}:
                record = by_id.get(database_id)
                # A database of the config file has no record: the gateway's uptime alone says it had time.
                if record is None or (record.enabled and record.updated_at <= settled):
                    load = True
        late = now - timedelta(seconds=updater_interval) - timedelta(hours=1)
        update = any(
            record.enabled
            and (due := next_update_at(record)) is not None
            and (record.last_update_error is not None or due < late)
            for record in records
        )
        if load and update:
            return [_line("ip_databases", _TEXTS["ip_both"], admin_page="geo")]
        if load:
            return [_line("ip_databases", _TEXTS["ip_load"], admin_page="geo")]
        if update:
            return [_line("ip_databases", _TEXTS["ip_update"], admin_page="geo")]
        return []

    # --- Changes --------------------------------------------------------------------------------

    def _changes_waiting(self) -> int | None:
        """When the oldest change the leader has not compiled was announced, in ms, once a minute old; None otherwise."""
        if self._reading.head is None or self._reading.head_error is not None:
            return None
        entry = self._positions.next_change
        if entry is None:
            return None
        at = StreamId.of(entry).ms
        return at if self._positions.now_ms - at >= CHANGES_LATE_SECONDS * 1000 else None

    def _feed_missed(self) -> int | None:
        """When the first feed publish a serving gateway of the leader's release missed was made, in ms, the earliest
        such once a minute old; None otherwise."""
        head, feed, leader = self._reading.head, self._positions.feed, self._holder
        if head is None or self._reading.head_error is not None or not feed or leader is None:
            return None
        version = leader.snapshot.runtime.version if leader.snapshot is not None else None
        missed: list[int] = []
        for instance in self._reading.instances:
            snapshot = instance.snapshot
            gateway = snapshot.gateway if snapshot is not None else None
            if (
                snapshot is None
                or gateway is None
                or not _is_serving(instance)
                or snapshot.runtime.version != version
                or gateway.feed_digest == head.digest
            ):
                continue
            last = max((i for i, (_entry, digest) in enumerate(feed) if digest == gateway.feed_digest), default=None)
            if last is None:
                first = feed[0][0]
            elif last + 1 < len(feed):
                first = feed[last + 1][0]
            else:
                continue
            at = StreamId.of(first).ms
            if self._positions.now_ms - at >= CHANGES_LATE_SECONDS * 1000:
                missed.append(at)
        return min(missed, default=None)

    # --- Background work ------------------------------------------------------------------------

    def _scheduling_stopped(self) -> bool:
        """The leader's producers have not appended the minutely index hygiene job for three minutes, or there is no
        stream and no leader to append it."""
        hygiene = self._positions.hygiene
        if hygiene is None:
            return self._holder is None
        return self._positions.now_ms - StreamId.of(hygiene.last_id).ms >= SCHEDULING_SILENT_SECONDS * 1000

    def _behind(self, area: _Area) -> bool:
        """Whether any of the area's streams waits on nobody, waits too long, or its job kind keeps failing."""
        for name, kind in area.streams:
            stream = self._streams.by_name.get(name)
            if stream is None:
                continue
            takers = self._takers(kind)
            if not takers and _waiting(stream):
                return True
            if stream.oldest_seconds is not None and stream.oldest_seconds >= area.oldest_seconds:
                return True
            if (
                area.counts_failures
                and kind is not None
                and any(_failing(taker, kind) >= FAILING_RUNS for taker in takers)
            ):
                return True
        return False

    def _takers(self, kind: JobKind | None) -> list[InstanceInfo]:
        """The live workers taking ``kind``; every live worker for an ingest stream (None)."""
        return [
            instance
            for instance in self._reading.instances
            if instance.snapshot is not None
            and instance.snapshot.worker is not None
            and (kind is None or kind in instance.snapshot.worker.jobs)
        ]


# --- Helpers ------------------------------------------------------------------------------------------


def _line(
    key: str,
    text: str,
    severity: Severity = Severity.WARNING,
    *,
    at: datetime | None = None,
    admin_page: _AdminPage = "system",
) -> InstallAttentionLine:
    return InstallAttentionLine(key=key, severity=severity, text=text, at=at, admin_page=admin_page)


def _leader_instance(reading: ClusterReading) -> InstanceInfo | None:
    """The live lease holder; None without one."""
    return next((instance for instance in reading.instances if instance.instance_id == reading.leader), None)


def _leader(holder: InstanceInfo | None) -> ControllerSnapshot | None:
    """The lease holder's controller section; None without a holder or a readable snapshot."""
    return holder.snapshot.controller if holder is not None and holder.snapshot is not None else None


def _compiled_behind(reading: ClusterReading) -> str | None:
    """How far the leader's last compile read the changes stream, when the stream holds entries after it; else None."""
    leader = _leader(_leader_instance(reading))
    if leader is None or leader.changes is None or reading.changes_head is None:
        return None
    return leader.changes if StreamId.of(leader.changes) < StreamId.of(reading.changes_head) else None


def _is_serving(instance: InstanceInfo) -> bool:
    """A gateway process listening and ready to proxy: only its own and the node's phase reasons count against it."""
    snapshot = instance.snapshot
    if instance.role not in _GATEWAY_ROLES or snapshot is None or snapshot.gateway is None:
        return False
    if not snapshot.gateway.listening:
        return False
    return not any(reason.startswith("gateway:") or reason in _NOT_SERVING for reason in snapshot.reasons)


def _serving(instances: Iterable[InstanceInfo]) -> list[GatewaySnapshot]:
    """The gateway sections of the serving gateway processes."""
    return [gateway for gateway, _uptime in _serving_with_uptime(instances)]


def _serving_with_uptime(instances: Iterable[InstanceInfo]) -> list[tuple[GatewaySnapshot, float]]:
    """The serving gateway processes' gateway sections, each with its process's uptime."""
    return [
        (instance.snapshot.gateway, instance.snapshot.runtime.uptime_seconds)
        for instance in instances
        if _is_serving(instance) and instance.snapshot is not None and instance.snapshot.gateway is not None
    ]


def _waiting(stream: WorkStream) -> bool:
    """Work on the stream no taker has taken for a while: since the group last took an entry, or since the oldest one
    was added when it never took one."""
    age = stream.last_delivered_seconds if stream.last_delivered_seconds is not None else stream.oldest_seconds
    return stream.length > 0 and age is not None and age >= NO_TAKER_SECONDS


def _failing(instance: InstanceInfo, kind: JobKind) -> int:
    """The runs of ``kind`` the worker failed in a row."""
    assert instance.snapshot is not None and instance.snapshot.worker is not None
    stats = instance.snapshot.worker.jobs.get(kind)
    return stats.consecutive_failures if stats is not None else 0


def _checked(reply: Any) -> Any:
    """A pipelined reply read with ``raise_on_error=False``; the error Redis answered with is raised."""
    if isinstance(reply, Exception):
        raise reply
    return reply


def _datetime(ms: int) -> datetime:
    """A stream entry's milliseconds as naive UTC, as every API time."""
    return datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=None)


def _joined(names: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"
