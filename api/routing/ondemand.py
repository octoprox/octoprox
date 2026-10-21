# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""On-demand country slot groups on a gateway: a worker provisions the group a gateway asked for, the feed brings it.

A gateway has no Postgres, so a miss publishes a ``provision`` job and waits for a snapshot it applies to carry the group.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from api.core.roles import JobKind
from api.jobs.provision import ProvisionTarget

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.jobs.queue import JobQueue
    from api.routing.snapshot import RoutingSnapshot


class FeedOnDemand:
    """The gateway's ``OnDemand``: a ``provision`` job per missing group, and waits that every feed apply wakes.

    Args:
        redis_client: the client ``jobs`` publishes through, whose request deadline each request runs under.
    """

    def __init__(self, jobs: JobQueue, redis_client: RedisClient) -> None:
        self._jobs = jobs
        self._redis = redis_client
        self._applied = asyncio.Condition()
        # The snapshot the gateway applied last; None before its first apply.
        self._snapshot: RoutingSnapshot | None = None

    async def request(self, connector_id: str, country: str) -> None:
        """Publish the ``provision`` job of the connector's slot group for ``country``; raises when Redis does not take it in time."""
        async with self._redis.request_deadline():
            await self._jobs.publish(JobKind.PROVISION, [ProvisionTarget(connector_id, country).key])

    async def wait(self, predicate: Callable[[RoutingSnapshot], bool], timeout: float) -> bool:
        """Wait until an applied snapshot satisfies ``predicate``, at most ``timeout`` seconds; whether one did.

        The snapshot applied last is checked first, so a group that arrived while its job was being requested is not waited for.
        """
        def satisfied() -> bool:
            return self._snapshot is not None and predicate(self._snapshot)

        async with self._applied:
            try:
                async with asyncio.timeout(timeout):
                    await self._applied.wait_for(satisfied)
            except TimeoutError:
                return False
        return True

    async def applied(self, snapshot: RoutingSnapshot) -> None:
        """A feed update was applied: wake every waiter to check ``snapshot``."""
        async with self._applied:
            self._snapshot = snapshot
            self._applied.notify_all()
