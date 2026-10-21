# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Traffic decisions: whether a connector is blocked by its traffic limit, and until when.

A worker decides a batch of connectors from their period totals and live windows, records each decision that changed
in the blocks hash with an entry on the runtime stream for the feed builder, and settles the requests it answers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.roles import JobKind
from api.core.traffic_limiter import ConnectorTraffic, TrafficHistory
from api.db.redis import (
    RUNTIME_STREAM_KEY,
    RUNTIME_STREAM_MAXLEN,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
)
from api.db.repository import ConnectorRepository
from api.db.streams import KIND_FIELD, REF_FIELD, RuntimeSignal
from api.feed.state import RuntimeStateReader
from api.jobs.job import Job
from api.models.connector import TrafficLimitAction

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from redis.commands.core import AsyncScript

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.jobs.job import JobBatch
    from api.models.connector import Connector

logger = structlog.get_logger()

# Lua: KEYS[1] the blocks hash, KEYS[2] the runtime stream, KEYS[3] the pending hash; ARGV[1] the connector ID,
# ARGV[2] the end of its block (ISO) or '' for none, ARGV[3] the stream's MAXLEN, ARGV[4] the connector's pending
# request as read before the decision's reads, or '' for none. Deletes the pending request when it is still that one,
# since a request made after the reads started waits for a decision of its own. Stores the decision and announces it
# only when it differs from the stored one, answering 1, and otherwise 0: two workers deciding alike append one entry.
_SET_BLOCK_SCRIPT = f"""
if ARGV[4] ~= '' and redis.call('HGET', KEYS[3], ARGV[1]) == ARGV[4] then
  redis.call('HDEL', KEYS[3], ARGV[1])
end
local stored = redis.call('HGET', KEYS[1], ARGV[1])
if ARGV[2] == '' then
  if not stored then
    return 0
  end
  redis.call('HDEL', KEYS[1], ARGV[1])
else
  if stored == ARGV[2] then
    return 0
  end
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
end
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[3], '*', '{KIND_FIELD}', '{RuntimeSignal.TRAFFIC.value}', '{REF_FIELD}', ARGV[1])
return 1
"""

# The usage statuses in the order a window passes them; each is logged on the way up only.
_STATUS_ORDER = ("ok", "warning", "exceeded")


class TrafficJob(Job):
    """Decides whether each connector of a batch is blocked by its traffic limit, and records each decision that changed.

    A connector that is gone, or has no limit, is not blocked.
    """

    kind = JobKind.TRAFFIC
    batch_size = 100

    def __init__(self, session_factory: SessionFactory, redis_client: RedisClient) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._state = RuntimeStateReader(redis_client)
        self._set_block: AsyncScript | None = None
        # The furthest usage status this process logged for each limited connector, and in which window.
        self._logged: dict[str, tuple[tuple[datetime, datetime], str]] = {}

    async def run(self, batch: JobBatch) -> None:
        """Decide each connector the batch names, then record and log the decisions that changed."""
        pending = await self._state.pending()
        async with self._session_factory() as session:
            found = await ConnectorRepository(session).get_many(batch.keys)
            limited = [connector for connector in found if connector.parsed_traffic_config.limit_bytes is not None]
            history = await TrafficHistory.load(session, limited, utc_now())
        traffic = await history.with_live(self._state)
        connectors = {connector.id: connector for connector in found}
        blocks: dict[str, datetime | None] = {}
        for connector_id in batch.keys:
            current = traffic.get(connector_id)
            if current is None:
                # Gone, or without a limit: not blocked, and nothing to remember until a limit comes back.
                blocks[connector_id] = None
                self._logged.pop(connector_id, None)
                continue
            self._log_usage(current)
            blocks[connector_id] = current.block_until()
        for connector_id in await self._record(blocks, pending):
            self._log_change(connector_id, connectors.get(connector_id), traffic.get(connector_id), blocks[connector_id])

    async def _record(self, blocks: Mapping[str, datetime | None], pending: Mapping[str, str]) -> list[str]:
        """Store each decision that differs from the stored one and announce it, settling the requests in ``pending``, pipelined; the connectors it changed."""
        if self._set_block is None:
            self._set_block = self._redis.client.register_script(_SET_BLOCK_SCRIPT)
        pipe = self._redis.client.pipeline(transaction=False)
        for connector_id, until in blocks.items():
            await self._set_block(
                keys=[TRAFFIC_BLOCKED_KEY, RUNTIME_STREAM_KEY, TRAFFIC_PENDING_KEY],
                args=[
                    connector_id,
                    until.isoformat() if until is not None else "",
                    RUNTIME_STREAM_MAXLEN,
                    pending.get(connector_id, ""),
                ],
                client=pipe,
            )
        answers = await pipe.execute()
        return [connector_id for connector_id, answer in zip(blocks, answers, strict=True) if answer == 1]

    def _log_usage(self, traffic: ConnectorTraffic) -> None:
        """Log a limited connector approaching or reaching its limit, each once per window in this process."""
        total, limit = traffic.total, traffic.config.limit_bytes or 0
        status = traffic.status
        span = (traffic.start, traffic.end)
        logged_span, logged = self._logged.get(traffic.connector.id, (span, "ok"))
        if logged_span == span and _STATUS_ORDER.index(status) <= _STATUS_ORDER.index(logged):
            return
        self._logged[traffic.connector.id] = (span, status)
        connector = traffic.connector
        if status == "exceeded":
            logger.warning(
                "Connector traffic limit reached",
                connector_id=connector.id,
                connector=connector.name,
                project_id=connector.project_id,
                total_bytes=total,
                limit_bytes=limit,
                action=traffic.config.action.value,
                period_end=traffic.end.isoformat(),
            )
        elif status == "warning":
            logger.warning(
                "Connector approaching its traffic limit",
                connector_id=connector.id,
                connector=connector.name,
                project_id=connector.project_id,
                total_bytes=total,
                limit_bytes=limit,
                percent=round(total / limit * 100, 1),
            )

    @staticmethod
    def _log_change(
        connector_id: str, connector: Connector | None, traffic: ConnectorTraffic | None, until: datetime | None
    ) -> None:
        """Log a decision the blocks hash took: the block, or the release and why."""
        if traffic is not None and until is not None:
            logger.warning(
                "Connector blocked by its traffic limit",
                connector_id=connector_id,
                connector=traffic.connector.name,
                project_id=traffic.connector.project_id,
                action=traffic.config.action.value,
                until=until.isoformat(),
            )
            return
        if connector is None:
            reason = "deleted"
        elif traffic is None:
            reason = "no_limit"
        elif traffic.config.action == TrafficLimitAction.ALERT:
            reason = "alert_only"
        else:
            reason = "under_limit"
        logger.info(
            "Connector released from its traffic limit",
            connector_id=connector_id,
            connector=connector.name if connector is not None else None,
            reason=reason,
        )
