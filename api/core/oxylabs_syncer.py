# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Oxylabs proxy syncer for periodic IP refresh and proxy management.

This module provides background synchronization for Oxylabs proxies:
- Periodic IP refresh for port-based proxies (every 24 hours)
- Auto-regeneration of deleted proxies
- Sync proxies when connector config changes
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

import structlog

from api.core.signals import (
    oxylabs_connector_sync_requested,
    proxy_add_requested,
    proxy_remove_requested,
    proxy_removed,
    proxy_update_requested,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.proxy import Proxy
from api.providers.oxylabs import OxylabsProvider

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# How often to refresh IPs for port-based proxies (24 hours in seconds)
IP_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


class OxylabsDataProvider(Protocol):
    """Protocol for accessing Oxylabs-related data."""

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


class OxylabsSyncer:
    """Manages periodic synchronization of Oxylabs proxies.

    Responsibilities:
    - Refresh discovered IPs for port-based proxies every 24 hours
    - Sync proxy counts when connector config changes
    - Auto-regenerate deleted proxies

    Communicates with ProxyManager via signals.
    """

    def __init__(self, data_provider: OxylabsDataProvider) -> None:
        """Initialize the Oxylabs syncer.

        Args:
            data_provider: Provider for read-only access to connectors, proxies, and credentials.
        """
        self._data_provider = data_provider
        self._running = False
        # Lock per connector to prevent concurrent syncs
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._subscribe_to_signals()

    def _subscribe_to_signals(self) -> None:
        """Subscribe to signals for connector sync and proxy regeneration."""
        oxylabs_connector_sync_requested.connect(self._on_connector_sync_requested)
        proxy_removed.connect(self._on_proxy_removed)

    async def run(self) -> None:
        """Run the Oxylabs syncer loop."""
        logger.info("Starting Oxylabs syncer", interval_hours=IP_REFRESH_INTERVAL_SECONDS // 3600)
        self._running = True

        while self._running:
            try:
                await self._refresh_all_oxylabs_proxies()
                await asyncio.sleep(IP_REFRESH_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Oxylabs syncer stopped")
                break
            except Exception as e:
                logger.error("Oxylabs syncer error", error=str(e))
                await asyncio.sleep(IP_REFRESH_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Signal the syncer to stop."""
        self._running = False

    async def _refresh_all_oxylabs_proxies(self) -> None:
        """Refresh IPs for all Oxylabs port-based proxies."""
        connectors = self._data_provider.connectors

        for connector in connectors:
            if not connector.enabled:
                continue

            credential = self._data_provider.get_credential(connector.credential_id)
            if not credential or credential.type != CredentialType.OXYLABS:
                continue

            try:
                await self._refresh_connector_proxies(connector, credential)
            except Exception as e:
                logger.error(
                    "Error refreshing Oxylabs connector",
                    connector_id=connector.id,
                    error=str(e),
                )

    async def _refresh_connector_proxies(
        self, connector: Connector, credential: Credential
    ) -> None:
        """Refresh proxies for a single Oxylabs connector."""
        provider = OxylabsProvider(connector, credential)

        # Only refresh IPs for port-based proxies
        if provider.is_session_based():
            return

        proxies = self._data_provider.get_proxies_for_connector(connector.id)
        if not proxies:
            return

        logger.info(
            "Refreshing Oxylabs proxy IPs",
            connector_id=connector.id,
            proxy_count=len(proxies),
        )

        # Refresh IPs - this updates the metadata in place
        updated_proxies = await provider.refresh_ips(proxies)

        # Signal proxy manager to persist the updated proxies
        for proxy in updated_proxies:
            await proxy_update_requested.send_async(self, proxy=proxy)

        logger.info(
            "Oxylabs proxy IP refresh complete",
            connector_id=connector.id,
            updated_count=len(updated_proxies),
        )

    async def _on_connector_sync_requested(
        self, sender: object, connector_id: str
    ) -> None:
        """Handle connector sync request signal."""
        connector = None
        for c in self._data_provider.connectors:
            if c.id == connector_id:
                connector = c
                break

        if not connector:
            logger.warning(
                "Connector not found for sync",
                connector_id=connector_id,
            )
            return

        credential = self._data_provider.get_credential(connector.credential_id)
        if not credential or credential.type != CredentialType.OXYLABS:
            return

        asyncio.create_task(self.sync_connector(connector, credential))

    async def _on_proxy_removed(
        self, sender: object, proxy_id: str, connector_id: str
    ) -> None:
        """Handle proxy removed signal - auto-regenerate Oxylabs proxies."""
        connector = None
        for c in self._data_provider.connectors:
            if c.id == connector_id:
                connector = c
                break

        if not connector or not connector.enabled:
            return

        credential = self._data_provider.get_credential(connector.credential_id)
        if not credential or credential.type != CredentialType.OXYLABS:
            return

        logger.info("Oxylabs proxy removed, regenerating", proxy_id=proxy_id, connector_id=connector_id)
        # Sync will add/remove proxies as needed
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
        """Synchronize proxies for a connector to match its configuration.

        Uses a per-connector lock to prevent concurrent syncs which could cause
        race conditions (e.g., multiple syncs each adding proxies).

        Args:
            connector: The Oxylabs connector to sync
            credential: The Oxylabs credential
        """
        if credential.type != CredentialType.OXYLABS:
            return

        lock = self._get_sync_lock(connector.id)

        async with lock:
            provider = OxylabsProvider(connector, credential)
            existing_proxies = self._data_provider.get_proxies_for_connector(connector.id)

            logger.info(
                "Syncing Oxylabs connector",
                connector_id=connector.id,
                existing_count=len(existing_proxies),
                target_count=connector.oxylabs_config.num_proxies if connector.oxylabs_config else 0,
            )

            proxies_to_add, proxy_ids_to_remove = await provider.sync_proxies(existing_proxies)

            # Add new proxies
            for proxy in proxies_to_add:
                await proxy_add_requested.send_async(self, proxy=proxy)
                logger.debug("Added Oxylabs proxy", proxy_id=proxy.id)

            # Remove excess proxies
            for proxy_id in proxy_ids_to_remove:
                await proxy_remove_requested.send_async(self, proxy_id=proxy_id)
                logger.debug("Removed Oxylabs proxy", proxy_id=proxy_id)

            logger.info(
                "Oxylabs connector sync complete",
                connector_id=connector.id,
                added=len(proxies_to_add),
                removed=len(proxy_ids_to_remove),
            )


