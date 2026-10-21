# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy writes: the admin's adds, edits and deletes, and the jobs' adds, deletes and status changes.

Each write is one transaction, announced after its commit when it changed a row, and the runs it needs are asked for after.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from api.core.roles import JobKind
from api.db.redis import PROXY_REQUESTS_KEY, QUARANTINE_KEY
from api.db.repository import ConnectorRepository, ProjectRepository, ProxyRepository
from api.db.streams import ChangeKind, RuntimeSignal
from api.geo.attributor import ProxyAttributor
from api.geo.models import MANUAL_SOURCE, META_COUNTRY_SOURCE
from api.models.credential import CredentialType
from api.models.location import set_manual_location
from api.models.proxy import (
    Proxy,
    ProxyCreate,
    ProxyProtocol,
    ProxyStatus,
    ProxyUpdate,
    metadata_diff,
)
from api.providers.sdk.strategies import META_COUNTRY
from api.services.errors import NotFoundError, WriteRejectedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.jobs.queue import JobQueue
    from api.models.connector import Connector
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()

# One proxy per line of an upload: protocol://[username:password@]host:port.
PROXY_URL_PATTERN = re.compile(
    r"^(?P<protocol>https?|socks[45])://"
    r"(?:(?P<username>[^:@]+):(?P<password>[^@]+)@)?"
    r"(?P<host>[^:]+):(?P<port>\d+)$",
    re.IGNORECASE,
)

# Statuses of a row no health check has judged yet: added rows in these get
# a health run at once rather than at their second of the next health period.
_AWAITING_FIRST_CHECK = frozenset({ProxyStatus.UNKNOWN, ProxyStatus.INITIALIZING})

# The editable definition fields a proxy edit copies as given; the exit
# country and the pinned place are applied into the metadata after them.
_EDITABLE = frozenset({"host", "port", "protocol", "username", "password", "tags", "metadata"})
# The definition fields a sync's refresh may change, written as the vendor
# left them (a credential a listed endpoint no longer has is cleared).
_SYNCED = ("host", "port", "protocol", "username", "password")


@dataclass(frozen=True)
class UploadError:
    """One line of an upload that did not become a proxy, and why."""

    line_number: int
    line: str
    error: str


@dataclass(frozen=True)
class ProxyUpload:
    """What an upload did: the proxies it added and the lines it could not read."""

    # Non-empty lines in the file.
    total_lines: int
    added: list[Proxy] = field(default_factory=list)
    errors: list[UploadError] = field(default_factory=list)


class StatusMove(NamedTuple):
    """A status a health check computed for a proxy, and the status it loaded the proxy with."""

    new: ProxyStatus
    expected: ProxyStatus


class SyncEdit(NamedTuple):
    """What a provider sync's refresh changed on one stored row: definition fields, and metadata keys."""

    proxy_id: str
    # Definition fields (host, port, protocol, username, password) to their new values, None clearing one.
    definition: Mapping[str, Any]
    # Metadata keys set, and keys dropped; merged key by key.
    changed: Mapping[str, Any]
    dropped: Collection[str]

    @classmethod
    def between(cls, before: Proxy, after: Proxy) -> SyncEdit:
        """What a refresh changed from ``before`` to ``after``: the definition fields, and only the metadata keys it touched.

        The metadata is merged key by key rather than written whole, so the attribution a sighting wrote while the
        vendor was asked is kept.
        """
        changed, dropped = metadata_diff(before.metadata, after.metadata)
        return cls(
            proxy_id=after.id,
            definition={name: getattr(after, name) for name in _SYNCED if getattr(after, name) != getattr(before, name)},
            changed=changed,
            dropped=dropped,
        )


class ProxyService:
    """Adds, edits, deletes and moves the status of proxies.

    Args:
        redis_client: Lifts a quarantine on unquarantine; its request deadline bounds every step after a commit.
        lookup_enabled: ``geo_lookup_enabled``: whether a static proxy added without a country gets an ``enrich`` run.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        jobs: JobQueue,
        redis_client: RedisClient,
        provider_registry: ProviderRegistry,
        *,
        changes: ChangeLog,
        lookup_enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs
        self._redis = redis_client
        self._registry = provider_registry
        self._changes = changes
        self._lookup_enabled = lookup_enabled

    # --- The api's writes -------------------------------------------------------------------

    async def create(self, project_id: str, data: ProxyCreate) -> Proxy:
        """Add a proxy by hand to a static connector of the project; the saved proxy.

        An exit country given here is the routing country, verified rather
        than overwritten by attribution; without one the exit is looked up
        through the proxy (``enrich``).
        """
        metadata = dict(data.metadata)
        if data.country:
            metadata[META_COUNTRY] = data.country
            metadata[META_COUNTRY_SOURCE] = MANUAL_SOURCE
        set_manual_location(metadata, state=data.state, city=data.city)
        proxy = Proxy(
            host=data.host,
            port=data.port,
            protocol=data.protocol,
            username=data.username,
            password=data.password,
            connector_id=data.connector_id,
            tags=data.tags,
            metadata=metadata,
        )
        async with self._session_factory() as session:
            connector = await self._static_connector(
                session,
                project_id,
                data.connector_id,
                "Proxies can only be manually added to STATIC_PROXY_PROVIDER connectors",
            )
            await ProxyRepository(session).create(proxy)
            await session.commit()
        await self._announce([proxy.id])
        await self._added([proxy], {connector.id: connector})
        return proxy

    async def upload(self, project_id: str, connector_id: str, text: str) -> ProxyUpload:
        """Add the proxies a file lists, one URL per line, to a static connector of the project.

        Lines that do not parse are reported and skipped; the rest are added
        in one transaction.
        """
        lines = text.strip().split("\n")
        proxies: list[Proxy] = []
        errors: list[UploadError] = []
        for line_number, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line:
                continue
            parsed = PROXY_URL_PATTERN.match(line)
            if parsed is None:
                errors.append(UploadError(
                    line_number, line, "Invalid proxy URL format. Expected: protocol://[username:password@]host:port"
                ))
                continue
            proxies.append(Proxy(
                host=parsed["host"],
                port=int(parsed["port"]),
                protocol=ProxyProtocol(parsed["protocol"].lower()),
                username=parsed["username"],
                password=parsed["password"],
                connector_id=connector_id,
            ))
        async with self._session_factory() as session:
            connector = await self._static_connector(
                session, project_id, connector_id, "Proxies can only be uploaded to STATIC_PROXY_PROVIDER connectors"
            )
            await ProxyRepository(session).bulk_create(proxies)
            await session.commit()
        await self._announce([proxy.id for proxy in proxies])
        await self._added(proxies, {connector.id: connector})
        return ProxyUpload(total_lines=sum(1 for line in lines if line.strip()), added=proxies, errors=errors)

    async def update(self, project_id: str, proxy_id: str, data: ProxyUpdate) -> Proxy:
        """Apply the fields ``data`` sets, never the status, to a proxy of the project; the saved proxy.

        A country, state or city set here is pinned for routing, and an empty string unpins it.
        """
        async with self._session_factory() as session:
            proxies = ProxyRepository(session)
            # Locked until the commit: the edit writes every definition column
            # back, the metadata included, so a write another process commits
            # meanwhile (attribution's patch, say) must land before the read.
            current = await proxies.get_in_project(project_id, proxy_id, for_update=True)
            if current is None:
                raise NotFoundError("Proxy not found")
            await proxies.update(self._edited(current, data))
            saved = await proxies.get_by_id(proxy_id)
            await session.commit()
        if saved is None:
            # Deleted between the read and the write.
            raise NotFoundError("Proxy not found")
        logger.info("Updated proxy", proxy_id=proxy_id, host=saved.host)
        await self._announce([proxy_id])
        return saved

    async def remove(self, project_id: str, proxy_id: str) -> None:
        """Delete a proxy of the project, as the api's delete does.

        Any proxy but a cloud one goes now (:meth:`delete_many`); a cloud one is marked terminating and a scale run
        asked for, since its instance must be terminated first.
        """
        async with self._session_factory() as session:
            proxy = await ProxyRepository(session).get_in_project(project_id, proxy_id)
            if proxy is None:
                raise NotFoundError("Proxy not found")
            connector = await ConnectorRepository(session).get_by_id(proxy.connector_id)
        if connector is None or not self._registry.is_cloud(connector.credential_type):
            await self.delete_many([proxy_id])
            return
        await self.terminate(proxy_id)
        # Asked for even when it was terminating already: a second delete is how an admin retries a stalled one.
        await self._jobs.request_now(JobKind.SCALE, [connector.id])

    async def unquarantine(self, project_id: str, proxy_id: str) -> bool:
        """Lift a proxy's quarantine and empty its rate window; False, touching nothing, when it was not quarantined.

        Both go in one MULTI with the runtime entry announcing them, so gateways route to the proxy again within moments.
        """
        async with self._session_factory() as session:
            if await ProxyRepository(session).get_in_project(project_id, proxy_id) is None:
                raise NotFoundError("Proxy not found")
        if await self._redis.client.zscore(QUARANTINE_KEY, proxy_id) is None:
            return False
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.zrem(QUARANTINE_KEY, proxy_id)
        pipe.delete(PROXY_REQUESTS_KEY.format(proxy_id=proxy_id))
        RuntimeSignal.QUARANTINE.append(pipe, proxy_id)
        await pipe.execute()
        logger.info("Proxy manually unquarantined", proxy_id=proxy_id)
        return True

    # --- The jobs' writes -------------------------------------------------------------------

    async def add(self, proxy: Proxy) -> None:
        """Store a proxy a job built, with the status it was built with; one whose ID is stored already is kept as stored."""
        await self.bulk_add([proxy])

    async def bulk_add(self, proxies: Sequence[Proxy]) -> list[Proxy]:
        """Store proxies a job built (a sync's additions, a provisioned group), each with its built status, in one transaction; the ones stored.

        A proxy whose ID is stored already is skipped, and only the stored
        ones are announced and asked their first checks.
        """
        if not proxies:
            return []
        async with self._session_factory() as session:
            connectors = await self._connectors(session, {proxy.connector_id for proxy in proxies})
            added = await ProxyRepository(session).bulk_create(proxies)
            await session.commit()
        await self._announce([proxy.id for proxy in added])
        await self._added(added, connectors)
        return added

    async def apply_sync(self, edits: Sequence[SyncEdit], removed: Collection[str]) -> None:
        """Write what a provider sync changed on the stored rows and delete the rows it dropped, in one transaction.

        Rows go in id order under one change entry, metadata is merged key by key, and a row deleted meanwhile is
        skipped; the removals ask for no sync, since this run is the one they would ask for.
        """
        gone = set(removed)
        by_id = {edit.proxy_id: edit for edit in edits if edit.proxy_id not in gone}
        if not (by_id or gone):
            return
        deleted: list[str] = []
        edited: list[str] = []
        async with self._session_factory() as session:
            proxies = ProxyRepository(session)
            for proxy_id in sorted({*by_id, *gone}):
                edit = by_id.get(proxy_id)
                if edit is None:
                    if await proxies.delete(proxy_id):
                        deleted.append(proxy_id)
                    continue
                if not (edit.definition or edit.changed or edit.dropped):
                    continue
                # The merge goes first and locks the row, so a definition write
                # after it rewrites the metadata exactly as it then stands.
                if not await proxies.patch_metadata(proxy_id, edit.changed, edit.dropped, None):
                    continue
                edited.append(proxy_id)
                if edit.definition and (current := await proxies.get_by_id(proxy_id)) is not None:
                    await proxies.update(current.model_copy(update=dict(edit.definition)))
            await session.commit()
        await self._announce([*edited, *deleted])
        for proxy_id in deleted:
            logger.info("Removed proxy", proxy_id=proxy_id)

    async def delete(self, proxy_id: str) -> bool:
        """Delete a proxy now, as :meth:`delete_many` does; False when it was already gone."""
        return bool(await self.delete_many([proxy_id]))

    async def delete_many(self, proxy_ids: Iterable[str]) -> list[str]:
        """Delete proxies now in one transaction, under one change entry; the IDs deleted, in id order.

        Each enabled connector that syncs and lost a row gets one sync run, which fills its slots again.
        """
        syncing: set[str] = set()
        deleted: list[str] = []
        async with self._session_factory() as session:
            proxies = ProxyRepository(session)
            # Rows in id order, as every batch writer locks them.
            for proxy, connector, _ in await proxies.get_many_with_connectors(proxy_ids):
                if not await proxies.delete(proxy.id):
                    continue
                deleted.append(proxy.id)
                if connector.enabled and self._registry.is_syncable(connector.credential_type):
                    syncing.add(connector.id)
            await session.commit()
        await self._announce(deleted)
        for proxy_id in deleted:
            logger.info("Removed proxy", proxy_id=proxy_id)
        await self._jobs.request_now(JobKind.SYNC, sorted(syncing))
        return deleted

    async def drain(self, proxy_id: str) -> bool:
        """Stop routing new requests to a proxy; False when gone or draining already."""
        drained = await self._set_status(proxy_id, ProxyStatus.DRAINING)
        if drained:
            logger.info("Started draining proxy", proxy_id=proxy_id)
        return drained

    async def terminate(self, proxy_id: str) -> bool:
        """Mark a proxy for termination; False when gone or terminating already."""
        terminated = await self._set_status(proxy_id, ProxyStatus.TERMINATING)
        if terminated:
            logger.info("Marked proxy as terminating", proxy_id=proxy_id)
        return terminated

    async def set_statuses_if(self, moves: Mapping[str, StatusMove]) -> set[str]:
        """The health job's compare-and-set for a batch of checks: the ids moved.

        One transaction and one change entry, and each proxy moves from the status the check loaded to the one it
        computed, unless something moved it meanwhile, and never out of draining or terminating.
        """
        if not moves:
            return set()
        moved: set[str] = set()
        async with self._session_factory() as session:
            proxies = ProxyRepository(session)
            for proxy_id in sorted(moves):
                if await proxies.set_status_if(proxy_id, moves[proxy_id].new, moves[proxy_id].expected):
                    moved.add(proxy_id)
            await session.commit()
        await self._announce(moved)
        return moved

    # --- Shared steps -----------------------------------------------------------------------

    @staticmethod
    async def _static_connector(session: AsyncSession, project_id: str, connector_id: str, refusal: str) -> Connector:
        """The project's connector a proxy is added to by hand; it must be static (``refusal`` says why not)."""
        if await ProjectRepository(session).get_by_id(project_id) is None:
            raise NotFoundError("Project not found")
        connector = await ConnectorRepository(session).get_by_id(connector_id)
        if connector is None:
            raise NotFoundError("Connector not found")
        if connector.project_id != project_id:
            raise WriteRejectedError("Connector does not belong to this project")
        if connector.credential_type != CredentialType.STATIC_PROXY_PROVIDER.value:
            raise WriteRejectedError(refusal)
        return connector

    @staticmethod
    async def _connectors(session: AsyncSession, connector_ids: Iterable[str]) -> dict[str, Connector]:
        """The connectors new rows belong to; refused when one is gone (its rows would have nowhere to go)."""
        found: dict[str, Connector] = {}
        for connector_id in connector_ids:
            connector = await ConnectorRepository(session).get_by_id(connector_id)
            if connector is None:
                raise NotFoundError("Connector not found")
            found[connector_id] = connector
        return found

    @staticmethod
    def _edited(proxy: Proxy, data: ProxyUpdate) -> Proxy:
        """``proxy`` with an edit applied: the definition fields it sets, then the pinned country and place."""
        edited = proxy.model_copy(update=data.model_dump(include=set(_EDITABLE), exclude_none=True))
        metadata = dict(edited.metadata)
        if data.country is not None:
            if data.country:
                # Set by hand: attribution keeps it as the routing country and
                # verifies the exit against it instead of overwriting it.
                metadata[META_COUNTRY] = data.country
                metadata[META_COUNTRY_SOURCE] = MANUAL_SOURCE
            else:
                metadata.pop(META_COUNTRY, None)
                metadata.pop(META_COUNTRY_SOURCE, None)
        # A state or city set by hand is what -st- and -city- routing matches
        # this proxy on, ahead of the databases' record.
        set_manual_location(metadata, state=data.state, city=data.city)
        return edited.model_copy(update={"metadata": metadata})

    async def _set_status(self, proxy_id: str, status: ProxyStatus) -> bool:
        """Move a proxy into a lifecycle status (``ProxyRepository.set_status``); whether it moved."""
        async with self._session_factory() as session:
            changed = await ProxyRepository(session).set_status(proxy_id, status)
            await session.commit()
        if changed:
            await self._announce([proxy_id])
        return changed

    async def _announce(self, proxy_ids: Collection[str]) -> None:
        """Announce a committed write that changed the rows ``proxy_ids``: one ``proxy`` entry, none for no row."""
        if proxy_ids:
            await self._changes.record(ChangeKind.PROXY, *proxy_ids)

    async def _added(self, proxies: Sequence[Proxy], connectors: Mapping[str, Connector]) -> None:
        """Log committed additions and ask for their first health check and exit lookup."""
        for connector_id in dict.fromkeys(proxy.connector_id for proxy in proxies):
            count = sum(1 for proxy in proxies if proxy.connector_id == connector_id)
            logger.info("Added proxies", connector_id=connector_id, count=count)
        unchecked = [p.id for p in proxies if p.status in _AWAITING_FIRST_CHECK]
        await self._jobs.request_now(JobKind.HEALTH, unchecked)
        if self._lookup_enabled:
            lookups = [p.id for p in proxies if ProxyAttributor.needs_exit_lookup(p, connectors.get(p.connector_id))]
            await self._jobs.request_now(JobKind.ENRICH, lookups)
