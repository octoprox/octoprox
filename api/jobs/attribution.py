# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Attribution on the workers: the Postgres store attributing jobs write through, the sighting ingest and two jobs.

A store writes each row of a batch only while it holds the ``location_checked_at`` stamp it was read with.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from api.core.consumers import GroupConsumer, decode_items
from api.core.logging import error_text
from api.core.roles import JobKind
from api.core.workers import WorkerName
from api.db.redis import GEO_PREFLIGHT_KEY, SIGHTING_STREAM_KEY
from api.db.repository import (
    ConnectorRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.streams import ChangeKind
from api.feed.sections import resolve_credentials
from api.geo.attributor import ProxyAttributor
from api.geo.models import (
    META_ENDPOINT_CITY,
    META_ENDPOINT_COUNTRY,
    META_ENDPOINT_STATE,
    LocationCandidate,
    ObservationSource,
)
from api.jobs.job import Job
from api.jobs.messages import SightingMessage
from api.models.location import LocationTarget
from api.models.proxy import metadata_diff
from api.providers.sdk.strategies import META_DISCOVERED_IP

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog, StreamEntry
    from api.geo.service import GeoService
    from api.jobs.job import JobBatch
    from api.models.connector import Connector
    from api.models.credential import Credential
    from api.models.project import Project
    from api.models.proxy import Proxy
    from api.services.proxies import ProxyService

logger = structlog.get_logger()

# Sightings one ingest batch reads and attributes together. A batch costs three
# reads and one write transaction whatever its size, and holds the rows it
# names in memory until the write: 500 keeps that transaction to a second or
# two, and a burst's backlog still drains in a few batches.
SIGHTING_BATCH_SIZE = 500
# Re-attribution keys one batch reads together: a burst of asks after a
# database or settings change runs as one pass, not one per ask.
REATTRIBUTE_BATCH_SIZE = 100
# Static proxies one worker looks up at once. Each lookup is a request through
# the proxy that may wait the echo endpoint's whole timeout; five is what the
# 5.x attributor ran per instance.
ENRICH_CONCURRENCY = 5
# The bound a row read without a ``location_checked_at`` stamp is patched
# with: no stamp is older, so the write applies only while the row still has
# none, and the first two judgements of a new row never mix.
_UNSTAMPED = datetime.min

# Keys of the reattribute kind: every proxy, or one project's, or one connector's.
REATTRIBUTE_ALL = "all"
_PROJECT_SCOPE = "project:"
_CONNECTOR_SCOPE = "connector:"


class ReattributeScope(NamedTuple):
    """The proxies a reattribute key covers: one connector's, one project's, or every one (both None)."""

    connector_id: str | None = None
    project_id: str | None = None

    @property
    def key(self) -> str:
        """The scope's reattribute key: ``connector:<id>``, ``project:<id>``, or ``all`` for every proxy."""
        if self.connector_id is not None:
            return f"{_CONNECTOR_SCOPE}{self.connector_id}"
        if self.project_id is not None:
            return f"{_PROJECT_SCOPE}{self.project_id}"
        return REATTRIBUTE_ALL

    @classmethod
    def parse(cls, key: str) -> ReattributeScope | None:
        """The scope a reattribute key covers; None for a key of none of the three forms."""
        if key == REATTRIBUTE_ALL:
            return cls()
        if key.startswith(_CONNECTOR_SCOPE) and len(key) > len(_CONNECTOR_SCOPE):
            return cls(connector_id=key.removeprefix(_CONNECTOR_SCOPE))
        if key.startswith(_PROJECT_SCOPE) and len(key) > len(_PROJECT_SCOPE):
            return cls(project_id=key.removeprefix(_PROJECT_SCOPE))
        return None


# --- The store ---------------------------------------------------------------------------


@dataclass
class _Save:
    """How a saved row is written: judged from a sighting at ``checked_at``, then rotated or its verdict dropped."""

    checked_at: datetime | None
    rotate: bool
    drop_verdict: bool


class PostgresProxyStore:
    """The rows one batch of attribution works on, read from Postgres, and the writes it leaves behind.

    It implements the attributor's ``ProxyStore`` and ``ProxyPages``, whose docstrings are the contract; a job builds one
    per batch (:meth:`Attribution.store`).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        proxies: ProxyService,
        redis_client: RedisClient,
        *,
        changes: ChangeLog,
    ) -> None:
        self._session_factory = session_factory
        self._proxies = proxies
        self._redis = redis_client
        self._changes = changes
        # The batch: each row with its credentials resolved, as the attributor sees and changes it.
        self._rows: dict[str, Proxy] = {}
        # Each row's metadata as loaded: a save writes only what differs from it.
        self._loaded: dict[str, dict[str, Any]] = {}
        # Each row's ``location_checked_at`` as loaded: a save is written only while the row still holds it.
        self._stamps: dict[str, datetime | None] = {}
        self._connectors: dict[str, Connector] = {}
        self._credentials: dict[str, Credential] = {}
        self._projects: dict[str, Project] = {}
        self._pending: dict[str, _Save] = {}

    # --- reads -------------------------------------------------------------------------

    async def load(self, proxy_ids: Iterable[str]) -> None:
        """Add the named rows to the batch, with their connectors and projects; one query each.

        A row the batch holds already is kept as it is, changes included; a
        row that is gone is simply not loaded.
        """
        wanted = sorted(set(proxy_ids) - set(self._rows))
        if not wanted:
            return
        async with self._session_factory() as session:
            rows = await ProxyRepository(session).get_many(wanted)
            await self._adopt(session, rows)

    async def load_page(
        self, after: str | None, limit: int, *, connector_id: str | None, project_id: str | None
    ) -> list[Proxy]:
        """Make the next ``limit`` rows after id ``after`` the batch, in id order: a connector's, a project's or all."""
        self._clear()
        async with self._session_factory() as session:
            rows = await ProxyRepository(session).page(after, limit, connector_id=connector_id, project_id=project_id)
            await self._adopt(session, rows)
        return [self._rows[row.id] for row in rows if row.id in self._rows]

    async def _adopt(self, session: AsyncSession, rows: Sequence[Proxy]) -> None:
        """Read the connectors and projects of ``rows`` the batch lacks, then take the rows in, credentials filled."""
        connector_ids = {row.connector_id for row in rows} - set(self._connectors)
        for connector, credential in await ConnectorRepository(session).get_many_with_credentials(connector_ids):
            self._connectors[connector.id] = connector
            self._credentials[connector.id] = credential
        project_ids = {self._connectors[cid].project_id for cid in connector_ids if cid in self._connectors}
        for project in await ProjectRepository(session).get_many(project_ids - set(self._projects)):
            self._projects[project.id] = project
        for row in rows:
            owner = self._connectors.get(row.connector_id)
            if owner is None:
                # Deleted between the two reads, and the row with it.
                continue
            self._loaded[row.id] = copy.deepcopy(row.metadata)
            self._stamps[row.id] = ProxyAttributor.checked_at(row)
            self._rows[row.id] = resolve_credentials(row, self._credentials.get(owner.id), owner)

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        """The loaded row, its credentials filled in; None when it is not in the batch or was saved to rotate."""
        save = self._pending.get(proxy_id)
        if save is not None and save.rotate:
            return None
        return self._rows.get(proxy_id)

    def get_connector(self, connector_id: str) -> Connector | None:
        """A loaded row's connector."""
        return self._connectors.get(connector_id)

    def get_project(self, project_id: str) -> Project | None:
        """A loaded connector's project."""
        return self._projects.get(project_id)

    # --- writes ------------------------------------------------------------------------

    def save(
        self, proxy: Proxy, *, checked_at: datetime | None, rotate: bool = False, drop_verdict: bool = False
    ) -> None:
        """Queue the row as the attributor left it; the last save of a row in the batch decides how it is written."""
        self._pending[proxy.id] = _Save(checked_at, rotate, drop_verdict)

    async def flush(self) -> None:
        """Write the batch in one transaction, then announce it, drop verdicts and rotate for the rows whose write applied.

        Nothing after the commit fails it (the change entry is kept until Redis takes it, the rest is best effort), and
        the store is empty after.
        """
        pending, self._pending = self._pending, {}
        try:
            won = await self._write(pending)
            if won:
                # Once committed, so whatever the entry wakes reads the write.
                await self._changes.record(ChangeKind.PROXY, *won)
            await self._drop_verdicts([proxy_id for proxy_id in won if pending[proxy_id].drop_verdict])
            await self._rotate([proxy_id for proxy_id in won if pending[proxy_id].rotate])
        finally:
            self._clear()

    async def _write(self, pending: dict[str, _Save]) -> list[str]:
        """Patch every saved row in one transaction, in id order, each bounded by the stamp it was read with; the rows written.

        A row another attribution wrote since refuses its patch, and a save judged from a sighting older than the row as
        read is refused before it is sent.
        """
        won: list[str] = []
        refused: list[str] = []
        async with self._session_factory() as session:
            repository = ProxyRepository(session)
            for proxy_id in sorted(pending):
                save = pending[proxy_id]
                row, loaded = self._rows.get(proxy_id), self._loaded.get(proxy_id)
                if row is None or loaded is None:
                    continue
                read = self._stamps.get(proxy_id)
                if save.checked_at is not None and read is not None and save.checked_at < read:
                    refused.append(proxy_id)
                    continue
                changed, removed = metadata_diff(loaded, row.metadata)
                # An unchanged row is written only to learn whether it may still
                # be rotated or its verdict dropped: the bound decides.
                if not (changed or removed or save.rotate or save.drop_verdict):
                    continue
                if await repository.patch_metadata(proxy_id, changed, removed, _UNSTAMPED if read is None else read):
                    won.append(proxy_id)
                else:
                    refused.append(proxy_id)
            await session.commit()
        if refused:
            logger.debug("Stale attribution writes refused", count=len(refused))
        return won

    async def _drop_verdicts(self, proxy_ids: Sequence[str]) -> None:
        """Delete the cached preflight verdicts of rows whose exit moved without preflight seeing it."""
        keys = [
            GEO_PREFLIGHT_KEY.format(project_id=self._connectors[row.connector_id].project_id, proxy_id=row.id)
            for row in (self._rows[proxy_id] for proxy_id in proxy_ids)
            if row.connector_id in self._connectors
        ]
        if not keys:
            return
        try:
            await self._redis.client.delete(*keys)
        except Exception as exc:
            logger.warning("Could not drop preflight verdicts", count=len(keys), error=error_text(exc))

    async def _rotate(self, proxy_ids: list[str]) -> None:
        """Delete the refused slots in one go, which asks each syncing connector for one sync to provision fresh sessions."""
        if not proxy_ids:
            return
        try:
            await self._proxies.delete_many(proxy_ids)
        except Exception as exc:
            logger.warning("Could not rotate refused session slots", count=len(proxy_ids), error=error_text(exc))

    def _clear(self) -> None:
        self._rows.clear()
        self._loaded.clear()
        self._stamps.clear()
        self._pending.clear()


# --- Attributing a batch -----------------------------------------------------------------


class Sighting(NamedTuple):
    """One exit seen behind a proxy, in the terms :meth:`ProxyAttributor.observe` takes it."""

    proxy_id: str
    ip: str
    source: ObservationSource
    # The place the endpoint that saw the exit reported, if any.
    endpoint: LocationTarget | None = None
    # The client session it was seen for (preflight).
    session_id: str | None = None
    # What the databases said about ``ip`` where it was seen; None looks it up here.
    candidates: Sequence[LocationCandidate] | None = None
    # When and where it was seen; None for now and this process.
    observed_at: datetime | None = None
    instance_id: str | None = None

    @classmethod
    def of(cls, message: SightingMessage) -> Sighting:
        """A gateway's sighting as the attributor takes it, judged with the database answers the gateway had."""
        return cls(
            proxy_id=message.proxy_id,
            ip=message.ip,
            source=message.source,
            endpoint=LocationTarget.reported(message.endpoint_country, message.endpoint_state, message.endpoint_city),
            session_id=message.session_id,
            candidates=message.candidates,
            observed_at=message.observed_at,
            instance_id=message.instance_id,
        )

    @classmethod
    def discovered(cls, proxy: Proxy) -> Sighting | None:
        """The exit a sync's discovery recorded on a row, with the place its endpoint reported; None when it recorded none."""
        ip = proxy.metadata.get(META_DISCOVERED_IP)
        if not isinstance(ip, str) or not ip:
            return None
        endpoint = LocationTarget.reported(
            proxy.metadata.get(META_ENDPOINT_COUNTRY),
            proxy.metadata.get(META_ENDPOINT_STATE),
            proxy.metadata.get(META_ENDPOINT_CITY),
        )
        return cls(proxy.id, ip, ObservationSource.DISCOVERY, endpoint=endpoint)


class Attribution:
    """What a worker job attributes with: the process's attributor, and a Postgres store per batch."""

    def __init__(
        self,
        attributor: ProxyAttributor,
        session_factory: SessionFactory,
        proxies: ProxyService,
        redis_client: RedisClient,
        *,
        changes: ChangeLog,
    ) -> None:
        self._attributor = attributor
        self._session_factory = session_factory
        self._proxies = proxies
        self._redis = redis_client
        self._changes = changes

    @property
    def attributor(self) -> ProxyAttributor:
        """The process's attributor, which judges and records and holds no proxy state."""
        return self._attributor

    def store(self) -> PostgresProxyStore:
        """An empty store for one batch."""
        return PostgresProxyStore(self._session_factory, self._proxies, self._redis, changes=self._changes)

    async def observe(self, sightings: Sequence[Sighting]) -> None:
        """Attribute ``sightings`` as one batch, in the order given: load their rows, observe each, write once.

        Every sighting of a row that is still there is recorded, one observation each; rows judged differently are written.
        """
        if not sightings:
            return
        store = self.store()
        await store.load(sighting.proxy_id for sighting in sightings)
        for sighting in sightings:
            self._attributor.observe(
                store,
                sighting.proxy_id,
                sighting.ip,
                source=sighting.source,
                endpoint=sighting.endpoint,
                session_id=sighting.session_id,
                candidates=sighting.candidates,
                observed_at=sighting.observed_at,
                instance_id=sighting.instance_id,
            )
        await store.flush()


class SightingIngest(GroupConsumer):
    """Attributes the preflight sightings gateways append to the sightings stream, a batch at a time in the order they were seen.

    A row is written only while it is as the batch read it, so a sighting that arrives late never overwrites a newer one;
    this is the only place these sightings are recorded.
    """

    def __init__(self, redis_client: RedisClient, attribution: Attribution) -> None:
        super().__init__(redis_client, SIGHTING_STREAM_KEY, name=WorkerName.SIGHTING_INGEST, count=SIGHTING_BATCH_SIZE)
        self._attribution = attribution

    async def _handle(self, entries: list[StreamEntry]) -> None:
        """Attribute the batch's sightings in ``observed_at`` order; unreadable entries are logged and completed with it."""
        messages, malformed = decode_items(entries, SightingMessage.model_validate_json)
        if malformed:
            logger.warning("Unreadable sightings skipped", count=len(malformed))
        messages.sort(key=lambda message: message.observed_at)
        await self._attribution.observe([Sighting.of(message) for message in messages])


class ReattributeJob(Job):
    """Re-judges where proxies exit after a location, settings or IP database change: the ``reattribute`` kind's job.

    A batch reads what was committed before it was asked for, then runs one pass per scope its keys name, ``all``
    standing for every other.
    """

    kind = JobKind.REATTRIBUTE
    batch_size = REATTRIBUTE_BATCH_SIZE

    def __init__(self, attribution: Attribution, geo_service: GeoService) -> None:
        self._attribution = attribution
        self._geo_service = geo_service

    async def run(self, batch: JobBatch) -> None:
        """Read the settings and the database list again, then re-judge each scope of the batch, page by page."""
        scopes = self._scopes(batch.keys)
        if not scopes:
            return
        await self._geo_service.resync()
        for scope in scopes:
            await self._attribution.attributor.reattribute(
                self._attribution.store(), connector_id=scope.connector_id, project_id=scope.project_id
            )

    @staticmethod
    def _scopes(keys: Sequence[str]) -> list[ReattributeScope]:
        """The scopes the keys name, in order, or every proxy's alone when one names it; an unknown key is logged."""
        scopes: list[ReattributeScope] = []
        for key in keys:
            scope = ReattributeScope.parse(key)
            if scope is None:
                logger.warning("Unknown re-attribution key dropped", key=key)
            else:
                scopes.append(scope)
        everything = ReattributeScope()
        return [everything] if everything in scopes else scopes


class EnrichJob(Job):
    """Looks up the exit of a newly added static proxy through it, then attributes it: the ``enrich`` kind's job.

    A proxy that cannot be reached is left for its health checks, which see its exit too.
    """

    kind = JobKind.ENRICH
    # One proxy a run, as every job's batch is unless it says otherwise, so a
    # lookup waiting out its timeout holds up no other.
    concurrency = ENRICH_CONCURRENCY

    def __init__(self, attribution: Attribution) -> None:
        self._attribution = attribution

    async def run(self, batch: JobBatch) -> None:
        """Look up and attribute each proxy of the batch that still wants a lookup."""
        store = self._attribution.store()
        await store.load(batch.keys)
        attributor = self._attribution.attributor
        wanted = [
            proxy_id
            for proxy_id in batch.keys
            if (proxy := store.get_proxy(proxy_id)) is not None and attributor.wants_lookup(store, proxy)
        ]
        await asyncio.gather(*(attributor.enrich(store, proxy_id) for proxy_id in wanted))
        await store.flush()
