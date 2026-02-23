# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Generic proxy provider syncer for periodic IP refresh and proxy management.

This module provides background synchronization for all registered proxy providers:
- Periodic IP refresh for port-based proxies (every 24 hours)
- Auto-regeneration of deleted proxies
- Sync proxies when connector config changes

Supports any provider registered in the provider registry (e.g., Oxylabs, BrightData).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from api.core.signals import (
    provider_connector_sync_requested,
    proxy_add_requested,
    proxy_remove_requested,
    proxy_removed,
    proxy_update_requested,
)
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy
from api.providers.registry import (
    get_provider,
    is_syncable_credential_type,
)

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# How often to refresh IPs for port-based proxies (24 hours in seconds)
IP_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


class ProviderDataSource(Protocol):
    """Protocol for accessing provider-related data."""

    @property
    def connectors(self) -> list[Connector]:
        """Get all connectors."""
        ...

    def get_credential(self, credential_id: str) -> Credential | None:
        """Get a credential by ID."""
        ...

    def get_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a connector."""
        ...


class ProxyProviderSyncer:
    """Manages periodic synchronization of proxy provider proxies.

    Responsibilities:
    - Refresh discovered IPs for port-based proxies every 24 hours
    - Sync proxy counts when connector config changes
    - Auto-regenerate deleted proxies

    Works with any provider registered in the provider registry.
    Communicates with ProxyManager via signals.
    """

    def __init__(self, data_source: ProviderDataSource) -> None:
        """Initialize the provider syncer.

        Args:
            data_source: Provider for read-only access to connectors, proxies, and credentials.
        """
        self._data_source = data_source
        self._running = False
        # Lock per connector to prevent concurrent syncs
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._subscribe_to_signals()

    def _subscribe_to_signals(self) -> None:
        """Subscribe to signals for connector sync and proxy regeneration."""
        provider_connector_sync_requested.connect(self._on_connector_sync_requested)
        proxy_removed.connect(self._on_proxy_removed)

    async def run(self) -> None:
        """Run the provider syncer loop."""
        logger.info("Starting provider syncer", interval_hours=IP_REFRESH_INTERVAL_SECONDS // 3600)
        self._running = True

        while self._running:
            try:
                await self._refresh_all_provider_proxies()
                await asyncio.sleep(IP_REFRESH_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Provider syncer stopped")
                break
            except Exception as e:
                logger.error("Provider syncer error", error=str(e))
                await asyncio.sleep(IP_REFRESH_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Signal the syncer to stop."""
        self._running = False

    async def _refresh_all_provider_proxies(self) -> None:
        """Refresh IPs for all provider port-based proxies."""
        logger.debug("Refreshing provider proxies")

        for connector in self._data_source.connectors:
            if not connector.enabled:
                continue

            credential = self._data_source.get_credential(connector.credential_id)
            if not credential or not is_syncable_credential_type(credential.type):
                continue

            try:
                await self._refresh_connector_proxies(connector, credential)
            except Exception as e:
                logger.error(
                    "Failed to refresh connector proxies",
                    connector_id=connector.id,
                    credential_type=credential.type.value,
                    error=str(e),
                )

    async def _refresh_connector_proxies(
        self, connector: Connector, credential: Credential
    ) -> None:
        """Refresh IPs for a specific connector's proxies."""
        provider = get_provider(credential.type, connector, credential)
        if not provider:
            return

        # Only refresh port-based proxies
        if provider.is_session_based():
            logger.debug("Skipping session-based connector", connector_id=connector.id)
            return

        proxies = self._data_source.get_proxies_for_connector(connector.id)
        if not proxies:
            logger.debug("No proxies to refresh", connector_id=connector.id)
            return

        logger.info(
            "Refreshing IPs for connector",
            connector_id=connector.id,
            credential_type=credential.type.value,
            proxy_count=len(proxies),
        )

        # Refresh IPs
        updated_proxies = await provider.refresh_ips(proxies)

        # Emit update signals for changed proxies
        for proxy in updated_proxies:
            await proxy_update_requested.send_async(self, proxy=proxy)

    async def _on_connector_sync_requested(
        self, sender: Any, connector: Connector
    ) -> None:
        """Handle connector sync request signal.

        Args:
            sender: Signal sender
            connector: The connector to sync
        """
        credential = self._data_source.get_credential(connector.credential_id)
        if not credential or not is_syncable_credential_type(credential.type):
            return

        logger.info(
            "Connector sync requested",
            connector_id=connector.id,
            credential_type=credential.type.value,
        )
        asyncio.create_task(self.sync_connector(connector, credential))

    async def _on_proxy_removed(
        self, sender: Any, proxy_id: str, connector_id: str
    ) -> None:
        """Handle proxy removal signal - regenerate if needed.

        Args:
            sender: Signal sender
            proxy_id: ID of the removed proxy
            connector_id: ID of the connector
        """
        # Get the connector to check if it's a syncable provider
        connector = None
        for c in self._data_source.connectors:
            if c.id == connector_id:
                connector = c
                break

        if not connector or not connector.enabled:
            return

        credential = self._data_source.get_credential(connector.credential_id)
        if not credential or not is_syncable_credential_type(credential.type):
            return

        logger.info(
            "Proxy removed, triggering sync",
            proxy_id=proxy_id,
            connector_id=connector_id,
            credential_type=credential.type.value,
        )
        asyncio.create_task(self.sync_connector(connector, credential))

    def _get_sync_lock(self, connector_id: str) -> asyncio.Lock:
        """Get or create a lock for a connector's sync operations.

        Args:
            connector_id: The connector ID

        Returns:
            The lock for this connector
        """
        if connector_id not in self._sync_locks:
            self._sync_locks[connector_id] = asyncio.Lock()
        return self._sync_locks[connector_id]

    async def sync_connector(self, connector: Connector, credential: Credential) -> None:
        """Sync proxies for a connector to match configuration.

        Uses a per-connector lock to prevent concurrent syncs which could cause
        race conditions (e.g., multiple syncs each adding proxies).

        Args:
            connector: The connector to sync
            credential: The credential for the connector
        """
        lock = self._get_sync_lock(connector.id)

        async with lock:
            try:
                provider = get_provider(credential.type, connector, credential)
                if not provider:
                    logger.warning(
                        "No provider for credential type",
                        connector_id=connector.id,
                        credential_type=credential.type.value,
                    )
                    return

                existing_proxies = self._data_source.get_proxies_for_connector(connector.id)

                # Sync proxies
                proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

                # Emit signals for changes
                for proxy in proxies_to_add:
                    await proxy_add_requested.send_async(self, proxy=proxy)

                for proxy_id in proxy_ids_to_remove:
                    await proxy_remove_requested.send_async(self, proxy_id=proxy_id)

                logger.info(
                    "Connector synced",
                    connector_id=connector.id,
                    credential_type=credential.type.value,
                    added=len(proxies_to_add),
                    removed=len(proxy_ids_to_remove),
                )
            except Exception as e:
                logger.error(
                    "Failed to sync connector",
                    connector_id=connector.id,
                    credential_type=credential.type.value,
                    error=str(e),
                )
