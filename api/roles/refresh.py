# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The definitions refresher: how an api or worker process keeps the definitions it holds in memory current.

It reads the descriptors, attribution settings and IP database list again when the changes stream names one, or may
have missed one.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core.consumers import BroadcastFollower
from api.core.logging import error_text
from api.core.workers import WorkerName
from api.db.redis import CHANGES_STREAM_KEY
from api.db.streams import ChangeKind, FullRead

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.streams import StreamEntry
    from api.geo.settings import GeoSettingsStore
    from api.geo.store import GeoDatabaseStore
    from api.providers.store import ProviderStore

logger = structlog.get_logger()

# The change kinds that name what an api or worker process keeps in memory, or anything at all. The others name what
# the controller compiles, which such a process reads from Postgres whenever it needs it.
READ_ON = frozenset({ChangeKind.DESCRIPTOR, ChangeKind.GEO_SETTINGS, ChangeKind.GEO_DATABASE, ChangeKind.ALL})


class DefinitionsRefresher(BroadcastFollower):
    """Reads the provider descriptors, attribution settings and IP database list again when the changes stream names one.

    :meth:`start` makes the read the process serves with, from the stream's position on, and the loop follows from there.

    Args:
        reconcile_interval: seconds from one read in full to the next, made whatever the stream says.
    """

    def __init__(
        self,
        provider_store: ProviderStore,
        geo_settings_store: GeoSettingsStore,
        geo_database_store: GeoDatabaseStore,
        *,
        redis_client: RedisClient,
        reconcile_interval: float,
    ) -> None:
        super().__init__(
            redis_client, [CHANGES_STREAM_KEY], name=WorkerName.DEFINITIONS_REFRESHER, reconcile_interval=reconcile_interval
        )
        self._provider_store = provider_store
        self._geo_settings_store = geo_settings_store
        self._geo_database_store = geo_database_store
        # One read at a time: the loop's, or a sync job's that meets a credential type its registry lacks.
        self._reading = asyncio.Lock()
        # Whether a read has worked yet: the process serves only once one has.
        self._loaded = False

    async def start(self) -> None:
        """Take the stream's position, then read the definitions: the read the process serves with.

        Raises when Postgres cannot be read; without Redis it reads all the same, and the loop reads in full once Redis answers.
        """
        try:
            async with self._redis.request_deadline():
                await self._reader.start(FullRead.START)
        except Exception as exc:
            logger.debug("Changes stream not followed yet", error=error_text(exc))
        await self.read()

    async def read(self) -> None:
        """Read the descriptors, attribution settings and IP database list again; each store applies only what changed.

        Raises when Postgres cannot be read, keeping what is in memory; a read asked for while another runs waits for it.
        """
        async with self._reading:
            await self._provider_store.sync_all()
            await self._geo_settings_store.load()
            await self._geo_database_store.sync_all()
            self._loaded = True

    def readiness(self) -> str | None:
        """None once the definitions have been read, else why not."""
        return None if self._loaded else "definitions not loaded yet"

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """Whether any entry names what this process keeps (``READ_ON``)."""
        return any(entry.kind in READ_ON for entry in entries)

    async def _full_read(self) -> None:
        """Read the definitions again."""
        await self.read()
