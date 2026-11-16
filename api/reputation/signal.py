# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The runtime signal a writer of exit reputation records sends once its transaction committed: which projects' routing
it moved, for the controller to read again from Postgres."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core.logging import error_text
from api.db.streams import RuntimeSignal

if TYPE_CHECKING:
    from redis.asyncio.client import Pipeline

    from api.db.redis import RedisClient
    from api.reputation.record import ReputationChanges

logger = structlog.get_logger()


class ReputationSignal:
    """Tells the leading controller, after a commit, the projects whose avoids or dynamic connector factors moved: one
    runtime entry naming each, so the controller reads those projects alone; none when nothing moved. Every writer of
    the records (the report ingest, the metrics flush) sends it."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    async def send(self, changes: ReputationChanges) -> None:
        """Append the entries for ``changes``, which committed, in one pipeline.

        Never raises: the records are committed, and raising would apply the batch again. An entry Redis does not take is
        logged, and the controller's reconcile (every ``reconcile_interval``, an hour by default) reads the records anyway;
        a compile reads only the projects whose exits it moved.
        """
        moved = changes.moved_projects
        if not moved:
            return
        try:
            pipe: Pipeline = self._redis.client.pipeline(transaction=False)
            for project_id in moved:
                RuntimeSignal.REPUTATION.append(pipe, project_id)
            await pipe.execute()
        except Exception as exc:
            logger.warning("Exit reputation signal could not be sent", projects=len(moved), error=error_text(exc))
