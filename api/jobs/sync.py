# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider syncs: a worker matches a provider connector's proxies to what its vendor reports, a connector a run.

A run refreshes the exits of a port or list pool, reconciles the pool with its configuration, writes the result
through ``ProxyService`` and hands the exits discovery found to attribution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core.roles import JobKind
from api.db.repository import ConnectorRepository, ProxyRepository
from api.jobs.attribution import Sighting
from api.jobs.job import Job
from api.services.proxies import SyncEdit

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.jobs.attribution import Attribution
    from api.jobs.job import JobBatch
    from api.models.proxy import Proxy
    from api.providers.registry import ProviderRegistry
    from api.roles.refresh import DefinitionsRefresher
    from api.services.proxies import ProxyService

logger = structlog.get_logger()

# Connectors one worker syncs at once. A sync can take minutes (a port scan
# through the vendor, a long list), and holds up no other connector's.
SYNC_CONCURRENCY = 4

class ProviderSyncJob(Job):
    """Syncs the connectors of a batch, one at a time: the ``sync`` kind's job.

    Args:
        refresher: the worker's, which brings the registry up to the stored definitions for a credential type it does not know yet.
    """

    kind = JobKind.SYNC
    concurrency = SYNC_CONCURRENCY

    def __init__(
        self,
        session_factory: SessionFactory,
        registry: ProviderRegistry,
        proxies: ProxyService,
        attribution: Attribution,
        *,
        refresher: DefinitionsRefresher,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._proxies = proxies
        self._attribution = attribution
        self._refresher = refresher

    async def run(self, batch: JobBatch) -> None:
        """Sync each connector of the batch."""
        for connector_id in batch.keys:
            await self.sync(connector_id)

    async def sync(self, connector_id: str) -> None:
        """Match one connector's proxies to its vendor; nothing for a connector that is gone, disabled or does not sync.

        The definitions are read first when the credential type is one this worker's registry does not know yet.
        """
        async with self._session_factory() as session:
            found = await ConnectorRepository(session).get_with_credential(connector_id)
            stored = await ProxyRepository(session).get_by_connector(connector_id) if found else []
        if found is None:
            return
        connector, credential = found
        if not connector.enabled:
            return
        if self._registry.get(credential.type) is None:
            await self._refresher.read()
        if not self._registry.is_syncable(credential.type):
            return
        provider = self._registry.create_provider(connector, credential)
        if provider is None:
            logger.warning(
                "No provider for credential type", connector_id=connector_id, credential_type=credential.type
            )
            return

        # The provider changes the rows it is handed in place; the copies say what they were.
        before = {proxy.id: proxy.model_copy(deep=True) for proxy in stored}
        refreshed: list[Proxy] = []
        removed: list[str] = []
        if not provider.is_session_based() and (stored or provider.needs_periodic_sync()):
            refreshed, removed = await provider.refresh_ips(stored)
        gone = set(removed)
        added, surplus = await provider.sync_proxies([proxy for proxy in stored if proxy.id not in gone])
        removed = [*removed, *(proxy_id for proxy_id in surplus if proxy_id not in gone)]
        gone.update(surplus)

        kept = [proxy for proxy in refreshed if proxy.id not in gone]
        await self._proxies.apply_sync([SyncEdit.between(before[proxy.id], proxy) for proxy in kept], removed)
        if added:
            await self._proxies.bulk_add(added)
        sightings = [Sighting.discovered(proxy) for proxy in [*kept, *added]]
        await self._attribution.observe([sighting for sighting in sightings if sighting is not None])

        if kept or added or removed:
            logger.info(
                "Connector synced",
                connector_id=connector_id,
                credential_type=credential.type,
                refreshed=len(kept),
                added=len(added),
                removed=len(removed),
            )
