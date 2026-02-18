"""Auto-scaler service for dynamic proxy instance management.

This module provides automatic scaling and rotation of cloud proxy instances
based on demand levels and configured rotation periods.

Emits signals for decoupled communication with ProxyManager:
- proxy_add_requested: when a new proxy instance is created
- proxy_remove_requested: when a proxy should be removed
- proxy_draining_requested: when a proxy should start draining
- proxy_terminating_requested: when a proxy should be marked as terminating
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import structlog

from api.core import utc_now
from api.core.demand_tracker import DemandLevel
from api.core.signals import (
    connector_error_updated,
    connector_remove_requested,
    proxy_add_requested,
    proxy_draining_requested,
    proxy_instance_terminated,
    proxy_remove_requested,
    proxy_rotation_started,
    proxy_terminating_requested,
    scale_down_requested,
    scale_up_requested,
)
from api.models.connector import CloudConnectorConfig, Connector
from api.models.credential import CredentialType
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager


class AutoScalerDataProvider(Protocol):
    """Protocol for read-only access to data needed by AutoScaler."""

    @property
    def connectors(self) -> list[Connector]:
        """Get all connectors."""
        ...

    @property
    def demand_tracker(self):
        """Get the demand tracker."""
        ...

    def get_credential(self, credential_id: str):
        """Get a credential by ID."""
        ...

    def get_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a connector."""
        ...

    def get_active_proxies_for_connector(self, connector_id: str) -> list[Proxy]:
        """Get active (non-terminating) proxies for a connector."""
        ...

logger = structlog.get_logger()

# How often to check scaling and rotation (seconds)
CHECK_INTERVAL_SECONDS = 30

# Maximum backoff time in minutes for cloud provider errors
MAX_ERROR_BACKOFF_MINUTES = 30


class AutoScaler:
    """Manages automatic scaling and rotation of cloud proxy instances.

    Responsibilities:
    - Monitor demand levels per project
    - Scale up/down based on demand and min/max configuration
    - Schedule and execute instance rotation based on configured periods
    - Handle graceful draining before termination

    Communicates with ProxyManager via signals instead of direct method calls.

    Args:
        data_provider: Provider for read-only access to connectors, proxies, and credentials.
                       Typically the ProxyManager instance.
    """

    def __init__(self, data_provider: AutoScalerDataProvider) -> None:
        self._data_provider = data_provider
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

    def _should_skip_scaling(self, connector: Connector) -> bool:
        """Check if scaling should be skipped due to recent errors.

        Uses exponential backoff: 2^consecutive_errors minutes, capped at MAX_ERROR_BACKOFF_MINUTES.

        Returns:
            True if scaling should be skipped, False otherwise.
        """
        if not connector.last_error_at or connector.consecutive_errors == 0:
            return False

        # Calculate backoff time: 2^consecutive_errors minutes, capped
        backoff_minutes = min(
            2 ** connector.consecutive_errors,
            MAX_ERROR_BACKOFF_MINUTES,
        )
        backoff_until = connector.last_error_at + timedelta(minutes=backoff_minutes)

        now = utc_now()
        if now < backoff_until:
            logger.debug(
                "Skipping scaling due to error backoff",
                connector_id=connector.id,
                consecutive_errors=connector.consecutive_errors,
                backoff_minutes=backoff_minutes,
                backoff_until=backoff_until.isoformat(),
            )
            return True

        return False

    async def _record_connector_error(
        self, connector: Connector, error: str
    ) -> None:
        """Record a cloud provider error for a connector.

        Emits connector_error_updated signal - ProxyManager handles persistence.
        """
        new_consecutive_errors = connector.consecutive_errors + 1

        # Emit signal - ProxyManager will persist the error
        await connector_error_updated.send_async(
            self,
            connector_id=connector.id,
            error=error,
            consecutive_errors=new_consecutive_errors,
        )

        # Update local connector state for backoff calculations
        connector.last_error = error
        connector.last_error_at = utc_now()
        connector.consecutive_errors = new_consecutive_errors

    async def _clear_connector_error(self, connector: Connector) -> None:
        """Clear error state for a connector after successful operation.

        Emits connector_error_updated signal - ProxyManager handles persistence.
        """
        if connector.last_error is None and connector.consecutive_errors == 0:
            return  # No error to clear

        # Emit signal - ProxyManager will persist the cleared state
        await connector_error_updated.send_async(
            self,
            connector_id=connector.id,
            error=None,
            consecutive_errors=0,
        )

        # Update local connector state
        connector.last_error = None
        connector.last_error_at = None
        connector.consecutive_errors = 0

    async def _check_all_connectors(self) -> None:
        """Check scaling and rotation for all cloud connectors."""
        connectors = self._data_provider.connectors

        for connector in connectors:
            # Only process cloud connectors (AWS, GCP, Azure)
            credential = self._data_provider.get_credential(connector.credential_id)
            if not credential:
                continue

            if credential.type not in (
                CredentialType.AWS,
                CredentialType.GCP,
                CredentialType.AZURE,
            ):
                continue

            try:
                # If connector is disabled, drain and terminate all its proxies
                if not connector.enabled:
                    await self._drain_disabled_connector(connector, credential)
                    continue

                await self._check_connector_scaling(connector, credential)
                await self._check_connector_rotation(connector, credential)
            except Exception as e:
                logger.error(
                    "Error checking connector",
                    connector_id=connector.id,
                    error=str(e),
                )

    async def _drain_disabled_connector(self, connector: Connector, credential) -> None:
        """Drain and terminate all proxies for a disabled connector.

        When a connector is disabled, we want to gracefully drain all its
        proxies and then terminate them. This is called by the auto-scaler
        on each cycle until all proxies are terminated.

        Handles the full lifecycle:
        - Active proxies → start draining
        - Draining proxies → check if traffic stopped, mark as terminating
        - Terminating proxies → terminate the cloud instance
        - No proxies left → request connector removal
        """
        proxies = self._data_provider.get_proxies_for_connector(connector.id)

        if not proxies:
            # All proxies terminated - only remove if marked for deletion
            if connector.pending_deletion:
                logger.info(
                    "All proxies terminated for connector pending deletion, requesting removal",
                    connector_id=connector.id,
                )
                await connector_remove_requested.send_async(self, connector_id=connector.id)
            return

        for proxy in proxies:
            if proxy.status == ProxyStatus.DRAINING:
                await self._handle_draining_proxy(proxy)
            elif proxy.status == ProxyStatus.TERMINATING:
                await self._handle_terminating_proxy(proxy, connector, credential)
            elif proxy.status not in (ProxyStatus.DRAINING, ProxyStatus.TERMINATING):
                # Active proxy - start draining
                logger.info(
                    "Draining proxy for disabled connector",
                    connector_id=connector.id,
                    proxy_id=proxy.id,
                )
                await proxy_draining_requested.send_async(self, proxy_id=proxy.id)
    
    async def _check_connector_scaling(
        self, connector: Connector, credential
    ) -> None:
        """Check and apply scaling for a connector."""
        cloud_config = connector.cloud_config
        if not cloud_config:
            return

        # Get current proxies (excluding terminating)
        proxies = self._data_provider.get_active_proxies_for_connector(connector.id)
        current_count = len(proxies)

        # Get demand level for the project
        project_id = connector.project_id
        healthy_proxies = [p for p in proxies if p.status == ProxyStatus.HEALTHY]
        demand_level = await self._data_provider.demand_tracker.get_demand_level(
            project_id, len(healthy_proxies)
        )

        # Determine target count based on demand
        target_count = self._calculate_target_count(
            demand_level,
            cloud_config.min_proxies,
            cloud_config.max_proxies,
            current_count,
        )

        # Check if we should skip scaling due to recent errors
        # (applies to both scale-up and scale-down since both involve cloud API calls)
        if self._should_skip_scaling(connector):
            return

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
        """Scale up by creating new proxy instances.

        Emits scale_up_requested signal before scaling.
        Tracks errors and clears them on success.
        """
        logger.info(
            "Scaling up",
            connector_id=connector.id,
            count=count,
        )

        # Emit signal for observability
        await scale_up_requested.send_async(
            self,
            connector_id=connector.id,
            count=count,
            reason="demand-based",
        )

        provider = self._get_cloud_provider(connector, credential)
        if not provider:
            return

        cloud_config = connector.cloud_config
        if not cloud_config:
            return

        had_success = False
        for _ in range(count):
            try:
                proxy = await provider.create_instance()
                if proxy:
                    # Set connector_id on the proxy
                    proxy.connector_id = connector.id
                    # Schedule rotation for this new proxy
                    self._schedule_rotation(proxy, cloud_config)
                    # Request proxy manager to add the proxy
                    await proxy_add_requested.send_async(self, proxy=proxy)
                    logger.info(
                        "Created new proxy instance",
                        proxy_id=proxy.id,
                        connector_id=connector.id,
                    )
                    had_success = True
            except Exception as e:
                logger.error("Failed to create proxy instance", error=str(e))
                await self._record_connector_error(connector, str(e))
                # Stop trying to create more instances after an error
                break

        # Clear error state if we had at least one success
        if had_success:
            await self._clear_connector_error(connector)

    async def _scale_down(self, connector: Connector, count: int) -> None:
        """Scale down by draining and terminating proxy instances.

        Emits scale_down_requested signal before scaling.
        """
        logger.info(
            "Scaling down",
            connector_id=connector.id,
            count=count,
        )

        # Emit signal for observability
        await scale_down_requested.send_async(
            self,
            connector_id=connector.id,
            count=count,
            reason="demand-based",
        )

        # Get proxies that can be terminated (prefer unhealthy, then oldest)
        proxies = self._data_provider.get_active_proxies_for_connector(connector.id)

        # Sort: unhealthy first, then by creation time (oldest first)
        def sort_key(p: Proxy) -> tuple:
            is_healthy = p.status == ProxyStatus.HEALTHY
            created_at = p.metadata.get("created_at", "")
            return (is_healthy, created_at)

        proxies_to_remove = sorted(proxies, key=sort_key)[:count]

        for proxy in proxies_to_remove:
            await proxy_draining_requested.send_async(self, proxy_id=proxy.id)

    def _schedule_rotation(self, proxy: Proxy, typed_config: CloudConnectorConfig) -> None:
        """Schedule rotation for a proxy based on config."""
        # Random rotation time between min and max
        rotation_minutes = random.randint(
            typed_config.min_rotation_period_minutes,
            typed_config.max_rotation_period_minutes,
        )
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
        cloud_config = connector.cloud_config
        if not cloud_config:
            return

        proxies = self._data_provider.get_proxies_for_connector(connector.id)
        now = utc_now()

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
                        self._schedule_rotation(proxy, cloud_config)
                else:
                    self._schedule_rotation(proxy, cloud_config)

            # Handle different proxy states
            if proxy.status == ProxyStatus.DRAINING:
                await self._handle_draining_proxy(proxy)
            elif proxy.status == ProxyStatus.TERMINATING:
                # Check backoff before attempting termination (cloud API call)
                if not self._should_skip_scaling(connector):
                    await self._handle_terminating_proxy(proxy, connector, credential)
            elif proxy.status in (ProxyStatus.HEALTHY, ProxyStatus.DEGRADED, ProxyStatus.UNKNOWN):
                # Check if rotation is due (skip if in error backoff since rotation involves cloud API calls)
                rotation_time = self._rotation_schedule.get(proxy.id)
                if rotation_time and now >= rotation_time:
                    if not self._should_skip_scaling(connector):
                        await self._start_rotation(proxy, connector, credential)

    async def _start_rotation(
        self, proxy: Proxy, connector: Connector, credential
    ) -> None:
        """Start rotation for a proxy - create replacement first, then drain.

        Emits proxy_rotation_started signal.
        """
        logger.info(
            "Starting proxy rotation",
            proxy_id=proxy.id,
            connector_id=connector.id,
        )

        # Emit signal for observability
        await proxy_rotation_started.send_async(
            self,
            old_proxy_id=proxy.id,
            connector_id=connector.id,
        )

        cloud_config = connector.cloud_config
        if not cloud_config:
            return

        # Create replacement instance first
        provider = self._get_cloud_provider(connector, credential)
        if provider:
            try:
                new_proxy = await provider.create_instance()
                if new_proxy:
                    new_proxy.connector_id = connector.id
                    self._schedule_rotation(new_proxy, cloud_config)
                    await proxy_add_requested.send_async(self, proxy=new_proxy)
                    logger.info(
                        "Created replacement proxy",
                        old_proxy_id=proxy.id,
                        new_proxy_id=new_proxy.id,
                    )
                    # Clear any previous errors on successful creation
                    await self._clear_connector_error(connector)
            except Exception as e:
                logger.error(
                    "Failed to create replacement proxy",
                    proxy_id=proxy.id,
                    error=str(e),
                )
                # Track the error
                await self._record_connector_error(connector, str(e))
                # Continue with draining anyway - we'll try again next cycle

        # Start draining the old proxy
        await proxy_draining_requested.send_async(self, proxy_id=proxy.id)

    async def _handle_draining_proxy(self, proxy: Proxy) -> None:
        """Handle a proxy that is draining - check if traffic has stopped.

        The proxy is marked as terminating when there are no changes in bytes
        sent and received between two consecutive check periods.
        """
        # Get current traffic stats
        current_bytes_sent = proxy.bytes_sent
        current_bytes_received = proxy.bytes_received

        # Get previous traffic stats from metadata
        prev_bytes_sent = proxy.metadata.get("draining_prev_bytes_sent")
        prev_bytes_received = proxy.metadata.get("draining_prev_bytes_received")

        if prev_bytes_sent is None or prev_bytes_received is None:
            # First check - store current values for next comparison
            proxy.metadata["draining_prev_bytes_sent"] = current_bytes_sent
            proxy.metadata["draining_prev_bytes_received"] = current_bytes_received
            logger.debug(
                "Draining proxy: recording initial traffic stats",
                proxy_id=proxy.id,
                bytes_sent=current_bytes_sent,
                bytes_received=current_bytes_received,
            )
            return

        # Check if traffic has stopped (no change since last check)
        if current_bytes_sent == prev_bytes_sent and current_bytes_received == prev_bytes_received:
            logger.info(
                "Draining complete: no traffic change detected, marking as terminating",
                proxy_id=proxy.id,
                bytes_sent=current_bytes_sent,
                bytes_received=current_bytes_received,
            )
            await proxy_terminating_requested.send_async(self, proxy_id=proxy.id)
        else:
            # Traffic still flowing - update previous values for next check
            proxy.metadata["draining_prev_bytes_sent"] = current_bytes_sent
            proxy.metadata["draining_prev_bytes_received"] = current_bytes_received
            logger.debug(
                "Draining proxy: traffic still flowing",
                proxy_id=proxy.id,
                prev_bytes_sent=prev_bytes_sent,
                prev_bytes_received=prev_bytes_received,
                current_bytes_sent=current_bytes_sent,
                current_bytes_received=current_bytes_received,
            )

    async def _handle_terminating_proxy(
        self, proxy: Proxy, connector: Connector, credential
    ) -> None:
        """Handle a proxy that is terminating - actually terminate it.

        Emits proxy_instance_terminated signal on successful termination.
        Tracks errors on failure.
        """
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
                # Request proxy manager to remove the proxy
                await proxy_remove_requested.send_async(self, proxy_id=proxy.id)
                logger.info("Terminated proxy instance", proxy_id=proxy.id)

                # Emit signal for observability
                await proxy_instance_terminated.send_async(
                    self,
                    proxy_id=proxy.id,
                    connector_id=connector.id,
                    instance_id=instance_id,
                )
            else:
                error_msg = f"Failed to terminate instance {instance_id}"
                logger.warning(
                    "Failed to terminate proxy instance",
                    proxy_id=proxy.id,
                )
                await self._record_connector_error(connector, error_msg)
        except Exception as e:
            logger.error(
                "Error terminating proxy instance",
                proxy_id=proxy.id,
                error=str(e),
            )
            await self._record_connector_error(connector, str(e))

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

