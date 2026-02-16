"""Auto-scaler service for dynamic proxy instance management.

This module provides automatic scaling and rotation of cloud proxy instances
based on demand levels and configured rotation periods.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.demand_tracker import DemandLevel
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()

# How often to check scaling and rotation (seconds)
CHECK_INTERVAL_SECONDS = 30


class AutoScaler:
    """Manages automatic scaling and rotation of cloud proxy instances.
    
    Responsibilities:
    - Monitor demand levels per project
    - Scale up/down based on demand and min/max configuration
    - Schedule and execute instance rotation based on configured periods
    - Handle graceful draining before termination
    
    Args:
        proxy_manager: The proxy manager instance for accessing proxies and connectors.
    """
    
    def __init__(self, proxy_manager: ProxyManager) -> None:
        self._proxy_manager = proxy_manager
        self._running = False
        # Track scheduled rotation times per proxy: proxy_id -> rotation_time
        self._rotation_schedule: dict[str, datetime] = {}
    
    async def run(self) -> None:
        """Run the auto-scaler loop."""
        logger.info("Starting auto-scaler", interval=CHECK_INTERVAL_SECONDS)
        self._running = True
        
        while self._running:
            try:
                await self._check_all_connectors()
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Auto-scaler stopped")
                break
            except Exception as e:
                logger.error("Auto-scaler error", error=str(e))
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    
    def stop(self) -> None:
        """Signal the auto-scaler to stop."""
        self._running = False
    
    async def _check_all_connectors(self) -> None:
        """Check scaling and rotation for all cloud connectors."""
        connectors = self._proxy_manager.connectors
        
        for connector in connectors:
            if not connector.enabled:
                continue
            
            # Only process cloud connectors (AWS, GCP, Azure)
            credential = self._proxy_manager.get_credential(connector.credential_id)
            if not credential:
                continue
            
            if credential.type not in (
                CredentialType.AWS,
                CredentialType.GCP,
                CredentialType.AZURE,
            ):
                continue
            
            try:
                await self._check_connector_scaling(connector, credential)
                await self._check_connector_rotation(connector, credential)
            except Exception as e:
                logger.error(
                    "Error checking connector",
                    connector_id=connector.id,
                    error=str(e),
                )
    
    async def _check_connector_scaling(
        self, connector: Connector, credential
    ) -> None:
        """Check and apply scaling for a connector."""
        config = connector.config
        min_proxies = config.get("min_proxies", 1)
        max_proxies = config.get("max_proxies", 10)
        
        # Get current proxies (excluding terminating)
        proxies = self._proxy_manager.get_active_proxies_for_connector(connector.id)
        current_count = len(proxies)
        
        # Get demand level for the project
        project_id = connector.project_id
        healthy_proxies = [p for p in proxies if p.status == ProxyStatus.HEALTHY]
        demand_level = await self._proxy_manager.demand_tracker.get_demand_level(
            project_id, len(healthy_proxies)
        )
        
        # Determine target count based on demand
        target_count = self._calculate_target_count(
            demand_level, min_proxies, max_proxies, current_count
        )
        
        # Scale up or down
        if target_count > current_count:
            await self._scale_up(connector, credential, target_count - current_count)
        elif target_count < current_count:
            await self._scale_down(connector, current_count - target_count)
    
    def _calculate_target_count(
        self,
        demand_level: DemandLevel,
        min_proxies: int,
        max_proxies: int,
        current_count: int,
    ) -> int:
        """Calculate target proxy count based on demand level."""
        if demand_level == DemandLevel.LOW:
            # Scale down to minimum
            return min_proxies
        elif demand_level == DemandLevel.MEDIUM:
            # Stay at current or scale to midpoint
            midpoint = (min_proxies + max_proxies) // 2
            # Don't scale down if already above midpoint
            return max(midpoint, min(current_count, max_proxies))
        else:  # HIGH
            # Scale up to maximum
            return max_proxies

    async def _scale_up(
        self, connector: Connector, credential, count: int
    ) -> None:
        """Scale up by creating new proxy instances."""
        logger.info(
            "Scaling up",
            connector_id=connector.id,
            count=count,
        )

        provider = self._get_cloud_provider(connector, credential)
        if not provider:
            return

        for _ in range(count):
            try:
                proxy = await provider.create_instance()
                if proxy:
                    # Set connector_id on the proxy
                    proxy.connector_id = connector.id
                    # Schedule rotation for this new proxy
                    self._schedule_rotation(proxy, connector.config)
                    # Add to proxy manager
                    await self._proxy_manager.add_proxy(proxy)
                    logger.info(
                        "Created new proxy instance",
                        proxy_id=proxy.id,
                        connector_id=connector.id,
                    )
            except Exception as e:
                logger.error("Failed to create proxy instance", error=str(e))

    async def _scale_down(self, connector: Connector, count: int) -> None:
        """Scale down by draining and terminating proxy instances."""
        logger.info(
            "Scaling down",
            connector_id=connector.id,
            count=count,
        )

        # Get proxies that can be terminated (prefer unhealthy, then oldest)
        proxies = self._proxy_manager.get_active_proxies_for_connector(connector.id)

        # Sort: unhealthy first, then by creation time (oldest first)
        def sort_key(p: Proxy) -> tuple:
            is_healthy = p.status == ProxyStatus.HEALTHY
            created_at = p.metadata.get("created_at", "")
            return (is_healthy, created_at)

        proxies_to_remove = sorted(proxies, key=sort_key)[:count]

        for proxy in proxies_to_remove:
            await self._proxy_manager.start_proxy_draining(proxy.id)

    def _schedule_rotation(self, proxy: Proxy, config: dict) -> None:
        """Schedule rotation for a proxy based on config."""
        min_minutes = config.get("min_rotation_period_minutes", 60)
        max_minutes = config.get("max_rotation_period_minutes", 1440)

        # Random rotation time between min and max
        rotation_minutes = random.randint(min_minutes, max_minutes)
        rotation_time = utc_now() + timedelta(minutes=rotation_minutes)

        self._rotation_schedule[proxy.id] = rotation_time
        proxy.metadata["scheduled_rotation_at"] = rotation_time.isoformat()

        logger.debug(
            "Scheduled proxy rotation",
            proxy_id=proxy.id,
            rotation_time=rotation_time.isoformat(),
            minutes_until_rotation=rotation_minutes,
        )

    async def _check_connector_rotation(
        self, connector: Connector, credential
    ) -> None:
        """Check and execute rotation for proxies in a connector."""
        proxies = self._proxy_manager.get_proxies_for_connector(connector.id)
        now = utc_now()
        config = connector.config
        max_rotation_minutes = config.get("max_rotation_period_minutes", 1440)
        draining_timeout = timedelta(minutes=max_rotation_minutes)

        for proxy in proxies:
            # Ensure proxy has a rotation schedule
            if proxy.id not in self._rotation_schedule:
                # Check if it was loaded from metadata
                scheduled_str = proxy.metadata.get("scheduled_rotation_at")
                if scheduled_str:
                    try:
                        self._rotation_schedule[proxy.id] = datetime.fromisoformat(
                            scheduled_str
                        )
                    except ValueError:
                        self._schedule_rotation(proxy, config)
                else:
                    self._schedule_rotation(proxy, config)

            # Handle different proxy states
            if proxy.status == ProxyStatus.DRAINING:
                await self._handle_draining_proxy(proxy, draining_timeout, credential)
            elif proxy.status == ProxyStatus.TERMINATING:
                await self._handle_terminating_proxy(proxy, connector, credential)
            elif proxy.status in (ProxyStatus.HEALTHY, ProxyStatus.DEGRADED, ProxyStatus.UNKNOWN):
                # Check if rotation is due
                rotation_time = self._rotation_schedule.get(proxy.id)
                if rotation_time and now >= rotation_time:
                    await self._start_rotation(proxy, connector, credential)

    async def _start_rotation(
        self, proxy: Proxy, connector: Connector, credential
    ) -> None:
        """Start rotation for a proxy - create replacement first, then drain."""
        logger.info(
            "Starting proxy rotation",
            proxy_id=proxy.id,
            connector_id=connector.id,
        )

        # Create replacement instance first
        provider = self._get_cloud_provider(connector, credential)
        if provider:
            try:
                new_proxy = await provider.create_instance()
                if new_proxy:
                    new_proxy.connector_id = connector.id
                    self._schedule_rotation(new_proxy, connector.config)
                    await self._proxy_manager.add_proxy(new_proxy)
                    logger.info(
                        "Created replacement proxy",
                        old_proxy_id=proxy.id,
                        new_proxy_id=new_proxy.id,
                    )
            except Exception as e:
                logger.error(
                    "Failed to create replacement proxy",
                    proxy_id=proxy.id,
                    error=str(e),
                )
                # Continue with draining anyway - we'll try again next cycle

        # Start draining the old proxy
        await self._proxy_manager.start_proxy_draining(proxy.id)

    async def _handle_draining_proxy(
        self, proxy: Proxy, draining_timeout: timedelta, credential
    ) -> None:
        """Handle a proxy that is draining - check if timeout reached."""
        draining_started_str = proxy.metadata.get("draining_started_at")
        if not draining_started_str:
            # No start time recorded, mark as terminating
            await self._proxy_manager.mark_proxy_terminating(proxy.id)
            return

        try:
            draining_started = datetime.fromisoformat(draining_started_str)
        except ValueError:
            await self._proxy_manager.mark_proxy_terminating(proxy.id)
            return

        # Check if draining timeout has passed
        if utc_now() >= draining_started + draining_timeout:
            logger.info(
                "Draining timeout reached, marking as terminating",
                proxy_id=proxy.id,
            )
            await self._proxy_manager.mark_proxy_terminating(proxy.id)

    async def _handle_terminating_proxy(
        self, proxy: Proxy, connector: Connector, credential
    ) -> None:
        """Handle a proxy that is terminating - actually terminate it."""
        provider = self._get_cloud_provider(connector, credential)
        if not provider:
            return

        try:
            # Get instance ID from proxy metadata or ID
            instance_id = proxy.metadata.get("instance_id", proxy.id)
            success = await provider.terminate_instance(instance_id)

            if success:
                # Remove from rotation schedule
                self._rotation_schedule.pop(proxy.id, None)
                # Remove from proxy manager
                await self._proxy_manager.remove_proxy(proxy.id)
                logger.info("Terminated proxy instance", proxy_id=proxy.id)
            else:
                logger.warning(
                    "Failed to terminate proxy instance",
                    proxy_id=proxy.id,
                )
        except Exception as e:
            logger.error(
                "Error terminating proxy instance",
                proxy_id=proxy.id,
                error=str(e),
            )

    def _get_cloud_provider(self, connector: Connector, credential):
        """Get the appropriate cloud provider for a connector."""
        from api.providers.cloud import AWSProvider, AzureProvider, GCPProvider

        # Pass connector and credential separately to the provider
        if credential.type == CredentialType.AWS:
            return AWSProvider(connector, credential)
        elif credential.type == CredentialType.GCP:
            return GCPProvider(connector, credential)
        elif credential.type == CredentialType.AZURE:
            return AzureProvider(connector, credential)

        return None

