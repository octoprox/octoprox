# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the api's tunnel services share: the constructor, the project check, a device's history and the gateways' readings.

Each protocol's settings and devices, and how they stand, stay in its own service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from api.core import utc_now
from api.db.repository import MetricsRepository, ProjectRepository
from api.models.metrics import RANGE_CONFIG
from api.models.tunnel import TunnelPeerMetricsHistoryResponse, TunnelPeerMetricsSnapshot
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeKind, ChangeLog
    from api.models.metrics import MetricsRange
    from api.models.tunnel import DeviceTotals, EndpointStates, TunnelEndpointState, TunnelProtocol

logger = structlog.get_logger()


class TunnelService[Peer](ABC):
    """A tunnel protocol's settings and devices for the admin API, over Postgres; gateways follow through the feed.

    Args:
        defaults: the protocol's ``defaults`` from the config, for a settings row created when a read finds none.
    """

    # The protocol's name in the Redis status keys and in endpoint states.
    PROTOCOL: ClassVar[TunnelProtocol]
    # The change entry that carries the protocol's writes to the gateways.
    CHANGE_KIND: ClassVar[ChangeKind]

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        *,
        endpoint_states: EndpointStates,
        device_totals: DeviceTotals,
        defaults: Mapping[str, Any],
        changes: ChangeLog,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._endpoint_states = endpoint_states
        self._device_totals = device_totals
        self._defaults = defaults
        self._changes = changes

    async def peer_history(
        self, project_id: str, peer_id: str, window: MetricsRange
    ) -> TunnelPeerMetricsHistoryResponse:
        """The device's own snapshots over ``window``, oldest first, in the ranges and tiers of project history.

        Not found for a missing project, then for a device that is not the project's.
        """
        delta, limit, bucket_seconds = RANGE_CONFIG[window]
        async with self._session_factory() as session:
            await self._peer_of_project(session, project_id, peer_id)
            since = utc_now() - delta
            repo = MetricsRepository(session)
            if bucket_seconds:
                rows = await repo.get_tunnel_peer_metrics_history_aggregated(
                    project_id=project_id,
                    peer_id=peer_id,
                    since=since,
                    bucket_seconds=bucket_seconds,
                )
            else:
                rows = await repo.get_tunnel_peer_metrics_history(
                    project_id=project_id, peer_id=peer_id, since=since, limit=limit, granularity=60
                )
        return TunnelPeerMetricsHistoryResponse(
            snapshots=[TunnelPeerMetricsSnapshot(**row) for row in reversed(rows)]
        )

    @staticmethod
    async def _require_project(session: AsyncSession, project_id: str) -> None:
        """Not found when the project does not exist; every device call checks this first."""
        if await ProjectRepository(session).get_by_id(project_id) is None:
            raise NotFoundError("Project not found")

    async def _peer_of_project(self, session: AsyncSession, project_id: str, peer_id: str) -> Peer:
        """The device; not found for a missing project, then for a device that does not exist or is another project's."""
        await self._require_project(session, project_id)
        peer = await self._get_in_project(session, project_id, peer_id)
        if peer is None:
            raise NotFoundError("Peer not found")
        return peer

    @abstractmethod
    async def _get_in_project(
        self, session: AsyncSession, project_id: str, peer_id: str
    ) -> Peer | None:
        """The device from the protocol's table, or None when it does not exist or belongs to another project."""

    async def _announce_seed(self, created: bool) -> None:
        """Announce the settings row a read just created (a backup restored without one); nothing when it was there."""
        if created:
            await self._changes.record(self.CHANGE_KIND)

    async def _published(self) -> list[dict[str, str]]:
        """What every gateway running the protocol's endpoint last published; [] when Redis cannot be reached."""
        try:
            return await self._redis.get_tunnel_peer_status(self.PROTOCOL)
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return []

    async def _gateways(self) -> list[TunnelEndpointState]:
        """The protocol's endpoints the live gateways report, by instance; [] when the heartbeats cannot be read."""
        try:
            states = await self._endpoint_states()
        except Exception as exc:
            logger.debug("Gateway endpoint states unavailable", error=str(exc))
            return []
        return sorted(
            (s for s in states if s.protocol == self.PROTOCOL), key=lambda s: s.instance_id
        )
