# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Re-attribution requests: a worker re-judges every proxy, or one connector's, with the attribution in force.

The api asks on the job stream and judges nothing itself; the worker's ``reattribute`` job does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core.roles import JobKind
from api.db.repository import ConnectorRepository
from api.jobs.attribution import REATTRIBUTE_ALL, ReattributeScope
from api.models.geo import ReattributeQueued
from api.services.errors import NotFoundError, UnavailableError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.jobs.queue import JobQueue

logger = structlog.get_logger()


class ReattributionService:
    """Asks a worker to re-judge exits, after a write that changed how they are judged or when an admin asks."""

    def __init__(self, session_factory: SessionFactory, jobs: JobQueue) -> None:
        self._session_factory = session_factory
        self._jobs = jobs

    async def request_all(self, reason: str) -> None:
        """Ask a worker to re-judge every proxy after a committed write that changed how exits are judged; a request
        Redis does not take is logged and dropped, since the write stands."""
        if await self._jobs.request_now(JobKind.REATTRIBUTE, [REATTRIBUTE_ALL]):
            logger.info("Re-attribution requested", key=REATTRIBUTE_ALL, reason=reason)

    async def request(self, connector_id: str | None, actor: Actor) -> ReattributeQueued:
        """Ask a worker to re-judge every proxy with a known exit IP, or one connector's (``connector_id``, blank for
        all); unavailable when Redis did not take the job's key within the request deadline."""
        connector_id = connector_id or None
        if connector_id is not None:
            async with self._session_factory() as session:
                if await ConnectorRepository(session).get_by_id(connector_id) is None:
                    raise NotFoundError("Connector not found")
        key = ReattributeScope(connector_id=connector_id).key
        if not await self._jobs.request_now(JobKind.REATTRIBUTE, [key]):
            raise UnavailableError("The job queue cannot be reached; try again")
        logger.info("Re-attribution requested", key=key, admin=actor.name)
        return ReattributeQueued(key=key)
