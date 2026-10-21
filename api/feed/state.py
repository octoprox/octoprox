# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Runtime state other roles keep in Redis, read in batches: quarantines, traffic blocks and decisions pending, soft health and metric windows.

The feed's runtime section, the jobs and the api's read model all read it through :class:`RuntimeStateReader`, so they
agree on what gateways route on.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import structlog
from pydantic import BaseModel, ValidationError

from api.core import utc_now
from api.core.stats import MetricDelta
from api.db.redis import (
    PROXY_HEALTH_KEY,
    QUARANTINE_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
)

if TYPE_CHECKING:
    from api.db.redis import EntityWindows, RedisClient

logger = structlog.get_logger()


class HealthSoft(BaseModel):
    """A proxy's soft health between checks, what the next check builds on and the api shows; the status is a Postgres column.

    Stored as the hash ``PROXY_HEALTH_KEY``, one field per model field as
    ``model_dump(mode="json")``, expiring after ``PROXY_HEALTH_TTL_SECONDS``.
    """

    # Failed checks in a row; 0 after a success.
    failures: int = 0
    # How long the last check took, in milliseconds.
    latency_ms: float = 0.0
    # When the last check ran (naive UTC).
    checked_at: datetime


class RuntimeStateReader:
    """Batch reads of the runtime state other roles keep in Redis."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    async def quarantined(self) -> dict[str, datetime]:
        """Every proxy in quarantine now, with when its quarantine ends (naive UTC), in one round trip.

        Ended entries are left out, whether or not index hygiene has trimmed
        them yet.
        """
        now = time.time()
        entries = await cast(
            "Awaitable[list[tuple[str, float]]]",
            self._redis.client.zrange(QUARANTINE_KEY, f"({now!r}", "+inf", byscore=True, withscores=True),
        )
        return {member: datetime.fromtimestamp(until, UTC).replace(tzinfo=None) for member, until in entries}

    async def blocked(self) -> dict[str, datetime]:
        """Every connector the traffic blocks hash holds, with when its block ends (naive UTC), in one round trip.

        Ended entries are included, until a decision removes them; an unreadable one is logged and left out.
        """
        entries = await cast("Awaitable[dict[str, str]]", self._redis.client.hgetall(TRAFFIC_BLOCKED_KEY))
        found: dict[str, datetime] = {}
        for connector_id, value in entries.items():
            try:
                until = datetime.fromisoformat(value)
            except ValueError:
                logger.warning("Unreadable traffic block skipped", connector_id=connector_id, value=value)
                continue
            found[connector_id] = until.astimezone(UTC).replace(tzinfo=None) if until.tzinfo else until
        return found

    async def blocked_now(self) -> set[str]:
        """The connectors whose stored traffic block is still in its period, its end being later than now, in one round trip."""
        blocks = await self.blocked()
        now = utc_now()
        return {connector_id for connector_id, until in blocks.items() if until > now}

    async def pending(self) -> dict[str, str]:
        """Every connector waiting for the traffic decision a reset or a limit edit asked for, with when it was asked as stored, in one round trip."""
        return await cast("Awaitable[dict[str, str]]", self._redis.client.hgetall(TRAFFIC_PENDING_KEY))

    async def health(self, proxy_ids: Iterable[str]) -> dict[str, HealthSoft]:
        """The soft health of each of ``proxy_ids`` that has any; a proxy without is left out."""
        ids = list(dict.fromkeys(proxy_ids))
        hashes = await self._redis.read_hashes([PROXY_HEALTH_KEY.format(proxy_id=proxy_id) for proxy_id in ids])
        found: dict[str, HealthSoft] = {}
        for proxy_id, data in zip(ids, hashes, strict=True):
            if not data:
                continue
            try:
                found[proxy_id] = HealthSoft.model_validate(data)
            except ValidationError:
                logger.warning("Unreadable soft health skipped", proxy_id=proxy_id)
        return found

    async def windows(self, windows: EntityWindows, ids: Iterable[str]) -> dict[str, MetricDelta]:
        """The current metrics window of each of ``ids`` that has one, of the kind ``windows``: what no metrics flush has taken yet.

        A tunnel device's window is a :class:`TunnelPeerMetricDelta`, so it
        carries the device's name-resolution counters too.
        """
        entity_ids = list(dict.fromkeys(ids))
        hashes = await self._redis.read_hashes([windows.key(entity_id) for entity_id in entity_ids])
        found: dict[str, MetricDelta] = {}
        for entity_id, data in zip(entity_ids, hashes, strict=True):
            if not data:
                continue
            try:
                found[entity_id] = windows.read(data)
            except ValueError:
                logger.warning("Unreadable metrics window skipped", kind=windows.name, entity_id=entity_id)
        return found
