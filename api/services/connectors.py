# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector writes: create, edit, delete, traffic reset and error recording, and the work each starts.

Each write but an error's is announced after its commit, and the runs it needs are asked for after that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import IntegrityError

from api.core import utc_now
from api.core.roles import JobKind
from api.db.redis import (
    AUTOSCALER_STATE_KEY,
    QUARANTINE_KEY,
    SCALE_CREATING_KEY,
    TRAFFIC_PENDING_KEY,
)
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.streams import ChangeKind, RuntimeSignal
from api.models.connector import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
    validate_rate_limit_config,
    validate_routing_config,
    validate_traffic_config,
)
from api.models.proxy import ProxyStatus
from api.services.errors import (
    NameConflictError,
    NotFoundError,
    WriteRejectedError,
    name_conflict,
    validated,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.jobs.queue import JobQueue
    from api.models.credential import Credential
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()

# The per-project unique index on connector names (migration 022; case-insensitive).
_NAME_UNIQUE = "ix_connectors_project_name_unique"
# The Redis keys a connector's scaling keeps, by connector ID: its autoscaler state and its instances being created.
_SCALE_KEYS = (AUTOSCALER_STATE_KEY, SCALE_CREATING_KEY)


class ConnectorService:
    """Creates, edits and deletes connectors, resets their traffic and records their errors.

    Args:
        redis_client: Lifts quarantines, marks connectors pending a traffic decision and deletes a deleted connector's keys; its request deadline bounds those steps.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        jobs: JobQueue,
        redis_client: RedisClient,
        provider_registry: ProviderRegistry,
        *,
        changes: ChangeLog,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs
        self._redis = redis_client
        self._registry = provider_registry
        self._changes = changes

    async def create(self, project_id: str, data: ConnectorCreate) -> Connector:
        """Validate and store a new connector of the project; the saved connector."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            credential = await self._credential_of(session, data.credential_id, project_id)
            connector = Connector(
                name=data.name,
                credential_id=credential.id,
                credential_type=credential.type,
                project_id=project_id,
                config=self._provider_config(credential, data.config),
                routing_config=_given(validate_routing_config, data.routing_config),
                rate_limit_config=_given(validate_rate_limit_config, data.rate_limit_config),
                traffic_config=_given(validate_traffic_config, data.traffic_config),
                enabled=data.enabled,
            )
            try:
                await ConnectorRepository(session).create(connector)
                await session.commit()
            except IntegrityError as exc:
                raise _taken(exc, connector.name) from None
        logger.info("Added connector", connector_id=connector.id, name=connector.name)
        await self._changes.record(ChangeKind.CONNECTOR, connector.id)
        await self._start_work(connector)
        return connector

    async def update(self, project_id: str, connector_id: str, data: ConnectorUpdate) -> Connector:
        """Apply the fields ``data`` sets to a connector of the project; the saved connector.

        A new provider config is validated for the connector's credential
        type, the new credential's when the edit also moves it to another
        credential of the project.
        """
        async with self._session_factory() as session:
            connectors = ConnectorRepository(session)
            current = await connectors.get_in_project(project_id, connector_id)
            if current is None:
                raise NotFoundError("Connector not found")
            credential = await CredentialRepository(session).get_by_id(current.credential_id)
            changes: dict[str, Any] = {}
            if data.name is not None:
                changes["name"] = data.name
            if data.credential_id is not None:
                credential = await self._credential_of(session, data.credential_id, current.project_id)
                changes |= {"credential_id": credential.id, "credential_type": credential.type}
            if data.config is not None:
                changes["config"] = self._provider_config(credential, data.config) if credential else data.config
            if data.routing_config is not None:
                changes["routing_config"] = validated(validate_routing_config, data.routing_config)
            if data.rate_limit_config is not None:
                changes["rate_limit_config"] = validated(validate_rate_limit_config, data.rate_limit_config)
            if data.traffic_config is not None:
                changes["traffic_config"] = validated(validate_traffic_config, data.traffic_config)
            if data.enabled is not None:
                changes["enabled"] = data.enabled
            edited = current.model_copy(update=changes)
            # Proxies quarantined under the old rate limit are released (read now, lifted after the commit).
            lifted = (
                [proxy.id for proxy in await ProxyRepository(session).get_by_connector(connector_id)]
                if edited.rate_limit_config != current.rate_limit_config
                else []
            )
            try:
                saved = await self._save(session, edited)
                await session.commit()
            except IntegrityError as exc:
                raise _taken(exc, edited.name) from None
        logger.info("Updated connector", connector_id=connector_id, name=saved.name)
        await self._changes.record(ChangeKind.CONNECTOR, connector_id)
        if lifted:
            await self._redis.after_commit(
                lambda: self._lift(lifted), "lift the connector's quarantines", connector_id=connector_id
            )
        await self._start_work(saved)
        if not saved.parsed_traffic_config.blocks_alike(current.parsed_traffic_config):
            # Decided again now, so a raised limit lifts a block at once rather than at the next minute's re-check.
            await self._ask_for_traffic_decision(connector_id)
        return saved

    async def remove(self, project_id: str, connector_id: str) -> None:
        """Delete a connector of the project, as the api's delete does: any but a cloud connector goes now.

        A cloud connector's instances are the scale job's to terminate first: its proxies are marked terminating, it is
        disabled and marked ``pending_deletion``, and a scale run is asked for.
        """
        proxy_ids: list[str] = []
        async with self._session_factory() as session:
            connector = await ConnectorRepository(session).get_in_project(project_id, connector_id)
            if connector is None:
                raise NotFoundError("Connector not found")
            cloud = self._registry.is_cloud(connector.credential_type)
            if cloud:
                proxies = ProxyRepository(session)
                # Rows in id order, as every batch writer locks them.
                proxy_ids = sorted(proxy.id for proxy in await proxies.get_by_connector(connector_id))
                for proxy_id in proxy_ids:
                    await proxies.set_status(proxy_id, ProxyStatus.TERMINATING)
                await self._save(session, connector.model_copy(update={"enabled": False, "pending_deletion": True}))
            else:
                await ConnectorRepository(session).delete(connector_id)
            await session.commit()
        await self._changes.record(ChangeKind.CONNECTOR, connector_id)
        if not cloud:
            await self._removed(connector_id)
            return
        logger.info("Marked cloud connector for deletion", connector_id=connector_id, proxy_count=len(proxy_ids))
        await self._jobs.request_now(JobKind.SCALE, [connector_id])

    async def delete(self, connector_id: str) -> bool:
        """Delete a connector now, with its proxies and history by cascade; False when it was already gone.

        The scale job calls this for a cloud connector pending deletion once
        its last instance is terminated.
        """
        async with self._session_factory() as session:
            deleted = await ConnectorRepository(session).delete(connector_id)
            await session.commit()
        if deleted:
            await self._changes.record(ChangeKind.CONNECTOR, connector_id)
            await self._removed(connector_id)
        return deleted

    async def forget(self, connector_id: str) -> None:
        """Delete the Redis keys a gone connector leaves: its autoscaler state and its instances being created.

        Raises when Redis does not take the delete.
        """
        await self._redis.client.delete(*(key.format(connector_id=connector_id) for key in _SCALE_KEYS))

    async def reset_traffic(self, project_id: str, connector_id: str) -> Connector:
        """Start the connector's traffic usage over from now, for a vendor top-up or a plan change; the saved connector.

        History is untouched: the current period counts from this moment, and a traffic decision is asked for now.
        """
        async with self._session_factory() as session:
            connector = await ConnectorRepository(session).get_in_project(project_id, connector_id)
            if connector is None:
                raise NotFoundError("Connector not found")
            saved = await self._save(session, connector.model_copy(update={"traffic_reset_at": utc_now()}))
            await session.commit()
        logger.info("Connector traffic usage reset", connector_id=connector_id, name=saved.name)
        await self._changes.record(ChangeKind.CONNECTOR, connector_id)
        # The block the reset lifts is the traffic job's to lift.
        await self._ask_for_traffic_decision(connector_id)
        return saved

    async def record_error(self, connector_id: str, error: str | None) -> Connector | None:
        """Record a cloud error (``consecutive_errors`` counts up), or clear it with None; the connector, if any.

        The scale job's backoff reads the error fields from the connector this returns; no change entry, since the
        feed compile clears these fields and no feed can change.
        """
        async with self._session_factory() as session:
            written = await ConnectorRepository(session).record_error(connector_id, error)
            connector = await ConnectorRepository(session).get_by_id(connector_id)
            await session.commit()
        if connector is not None and written:
            if error is None:
                logger.info("Connector error cleared", connector_id=connector_id)
            else:
                logger.warning(
                    "Connector error recorded",
                    connector_id=connector_id,
                    error=error,
                    consecutive_errors=connector.consecutive_errors,
                )
        return connector

    @staticmethod
    async def _credential_of(session: AsyncSession, credential_id: str, project_id: str) -> Credential:
        """The credential a connector is to use: it must exist and belong to the connector's project."""
        credential = await CredentialRepository(session).get_by_id(credential_id)
        if credential is None:
            raise NotFoundError("Credential not found")
        if credential.project_id != project_id:
            raise WriteRejectedError("Credential does not belong to this project")
        return credential

    def _provider_config(self, credential: Credential, config: dict[str, Any]) -> dict[str, Any]:
        """``config`` validated for the provider type of ``credential``."""
        return validated(
            lambda raw: self._registry.validate_connector_config(credential.type, raw, credential.config), config
        )

    async def _save(self, session: AsyncSession, connector: Connector) -> Connector:
        """Write ``connector``'s definition; the connector as stored, ``updated_at`` stamped."""
        saved = await ConnectorRepository(session).update(connector)
        if saved is None:
            # Deleted between the read and the write.
            raise NotFoundError("Connector not found")
        return saved

    async def _start_work(self, connector: Connector) -> None:
        """Ask for the runs a created or edited connector needs now: a sync, a scale."""
        if self._registry.is_syncable(connector.credential_type):
            await self._jobs.request_now(JobKind.SYNC, [connector.id])
        if self._registry.is_cloud(connector.credential_type):
            await self._jobs.request_now(JobKind.SCALE, [connector.id])

    async def _lift(self, proxy_ids: list[str]) -> None:
        """End the quarantines of ``proxy_ids`` and announce it on the runtime stream, in one MULTI."""
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.zrem(QUARANTINE_KEY, *proxy_ids)
        RuntimeSignal.QUARANTINE.append(pipe, *proxy_ids)
        await pipe.execute()

    async def _ask_for_traffic_decision(self, connector_id: str) -> None:
        """Ask a worker for the connector's traffic decision now, and mark the connector pending until a decision is stored.

        A request Redis refuses is logged, the write standing.
        """
        await self._redis.after_commit(
            lambda: self._send_traffic_request(connector_id), "ask for a traffic decision", connector_id=connector_id
        )

    async def _send_traffic_request(self, connector_id: str) -> None:
        """Mark the connector pending with the request's time, and append its ``traffic`` run-now entry, in one MULTI."""
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.hset(TRAFFIC_PENDING_KEY, connector_id, utc_now().isoformat())
        self._jobs.append(pipe, JobKind.TRAFFIC, connector_id)
        await pipe.execute()

    async def _removed(self, connector_id: str) -> None:
        """Log a connector this service deleted, then forget its keys; a delete Redis refuses is logged, the write standing."""
        logger.info("Removed connector", connector_id=connector_id)
        await self._redis.after_commit(
            lambda: self.forget(connector_id), "delete the connector's scale state", connector_id=connector_id
        )


def _given(validate: Callable[[dict[str, Any]], dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """A JSON config of a new connector, validated; one left empty is stored empty."""
    return validated(validate, config) if config else {}


def _taken(exc: IntegrityError, name: str) -> NameConflictError:
    """The per-project unique name ``name`` collided with."""
    return name_conflict(exc, {_NAME_UNIQUE: f"A connector named '{name}' already exists in this project"})
