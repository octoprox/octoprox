# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's preflight sightings of pooled session slots, on their way to the workers that attribute them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.core.publishers import ItemPublisher
from api.core.workers import WorkerName
from api.db.redis import SIGHTING_STREAM_KEY, SIGHTING_STREAM_MAXLEN, BatchChannel
from api.jobs.messages import SightingMessage

if TYPE_CHECKING:
    from api.db.redis import RedisClient


class SightingPublisher(ItemPublisher[SightingMessage]):
    """Buffers what preflight saw of each pooled slot and appends it to the sightings stream every ``interval``; one per gateway.

    Args:
        redis_client: where the sightings go, on the ``sighting_appends`` batch channel; None keeps them.
        max_pending: the most sightings buffered, through a Redis outage too; when full the oldest goes.
    """

    def __init__(self, redis_client: RedisClient | None, *, interval: float, max_pending: int) -> None:
        super().__init__(
            redis_client,
            name=WorkerName.SIGHTING_PUBLISHER,
            interval=interval,
            channel=BatchChannel.SIGHTING_APPENDS,
            key=SIGHTING_STREAM_KEY,
            max_items=SIGHTING_STREAM_MAXLEN,
            max_pending=max_pending,
        )
