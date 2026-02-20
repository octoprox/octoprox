"""Demand tracking for auto-scaling decisions.

This module tracks request rates per project using a sliding window
approach stored in Redis, enabling demand-based auto-scaling decisions.

Subscribes to request_completed and request_rejected signals to track demand
automatically, including demand from rejected requests (e.g., when no proxy
is available).
"""

import time
from enum import Enum

import structlog

from api.core import utc_now
from api.core.signals import request_completed, request_rejected
from api.db.redis import RedisClient

logger = structlog.get_logger()

# Redis key for demand tracking (sorted set with timestamps)
DEMAND_KEY = "project:demand:{project_id}"
# Window size for demand calculation (in seconds)
DEMAND_WINDOW_SECONDS = 300
# Short window for recent activity confirmation (in seconds)
RECENT_ACTIVITY_WINDOW_SECONDS = 60
# Minimum recent req/min to confirm demand is still active (not a stale burst)
RECENT_ACTIVITY_MIN_RPM = 1.0
# TTL for demand entries (slightly longer than window to allow cleanup)
DEMAND_TTL_SECONDS = 360
# Rate limit for recording rejected requests (seconds between records per project)
REJECTION_RECORD_INTERVAL_SECONDS = 1.0


class DemandLevel(str, Enum):
    """Demand level classification for auto-scaling."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Hysteresis thresholds for demand classification (requests per minute per proxy)
# Directional thresholds prevent oscillation between levels
DEMAND_THRESHOLDS = {
    "low_to_medium": 12,     # Must exceed 12 to leave LOW
    "medium_to_low": 8,      # Must drop below 8 to enter LOW
    "medium_to_high": 35,    # Must exceed 35 to enter HIGH
    "high_to_medium": 25,    # Must drop below 25 to leave HIGH
}


class DemandTracker:
    """Tracks request demand per project using a sliding window in Redis.

    Uses Redis sorted sets to store request timestamps, allowing efficient
    calculation of request rates over a sliding time window.

    Subscribes to request_completed signals to automatically track demand.

    Args:
        redis_client: Redis client for storing demand data.
    """

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client
        self._last_rejection_recorded: dict[str, float] = {}

    def subscribe_to_signals(self) -> None:
        """Subscribe to signals for automatic demand tracking."""
        request_completed.connect(self._on_request_completed)
        request_rejected.connect(self._on_request_rejected)
        logger.debug("DemandTracker subscribed to request signals")

    async def _on_request_completed(
        self,
        sender: object,
        proxy_id: str,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int,
        bytes_received: int,
    ) -> None:
        """Handle request completed signal to track demand."""
        await self.record_request(project_id)

    async def _on_request_rejected(
        self,
        sender: object,
        project_id: str,
        reason: str,
    ) -> None:
        """Handle request rejected signal to track demand from 503s.

        Rate-limited to avoid flooding Redis during sustained outages
        where clients may retry aggressively.
        """
        now = time.monotonic()
        last = self._last_rejection_recorded.get(project_id, 0.0)
        if now - last < REJECTION_RECORD_INTERVAL_SECONDS:
            return
        self._last_rejection_recorded[project_id] = now
        await self.record_request(project_id)

    async def record_request(self, project_id: str) -> None:
        """Record a request for demand tracking.
        
        Adds the current timestamp to a sorted set for the project.
        Old entries are automatically cleaned up.
        
        Args:
            project_id: The project ID to record the request for.
        """
        key = DEMAND_KEY.format(project_id=project_id)
        now = utc_now()
        timestamp = now.timestamp()
        
        # Use pipeline for efficiency
        pipe = self._redis_client.client.pipeline()
        
        # Add current request with timestamp as score
        pipe.zadd(key, {f"{timestamp}": timestamp})
        
        # Remove entries older than the window
        cutoff = timestamp - DEMAND_WINDOW_SECONDS
        pipe.zremrangebyscore(key, "-inf", cutoff)
        
        # Set TTL on the key
        pipe.expire(key, DEMAND_TTL_SECONDS)
        
        await pipe.execute()
    
    async def get_requests_per_minute(self, project_id: str) -> float:
        """Get the current request rate for a project.
        
        Counts requests in the sliding window and extrapolates to per-minute.
        
        Args:
            project_id: The project ID to get the rate for.
            
        Returns:
            Requests per minute (float).
        """
        key = DEMAND_KEY.format(project_id=project_id)
        now = utc_now()
        timestamp = now.timestamp()
        cutoff = timestamp - DEMAND_WINDOW_SECONDS
        
        # Count requests in the window
        count = await self._redis_client.client.zcount(key, cutoff, timestamp)
        
        # Extrapolate to per-minute rate
        # If window is 60 seconds, count is already per-minute
        rate = count * (60.0 / DEMAND_WINDOW_SECONDS)
        
        return rate

    async def get_recent_requests_per_minute(self, project_id: str) -> float:
        """Get the request rate using only the recent activity window.

        Uses RECENT_ACTIVITY_WINDOW_SECONDS to detect whether traffic is
        currently active, as opposed to stale burst data in the longer window.

        Args:
            project_id: The project ID to get the rate for.

        Returns:
            Requests per minute based on the recent window (float).
        """
        key = DEMAND_KEY.format(project_id=project_id)
        now = utc_now()
        timestamp = now.timestamp()
        cutoff = timestamp - RECENT_ACTIVITY_WINDOW_SECONDS

        count = await self._redis_client.client.zcount(key, cutoff, timestamp)
        return count * (60.0 / RECENT_ACTIVITY_WINDOW_SECONDS)

    async def has_recent_activity(self, project_id: str) -> bool:
        """Check if a project has recent request activity.

        Returns True if the request rate in the recent window meets
        RECENT_ACTIVITY_MIN_RPM. Used to gate scale-up decisions so that
        stale burst data in the longer window doesn't trigger scaling.

        Args:
            project_id: The project ID to check.

        Returns:
            True if there is recent activity, False otherwise.
        """
        recent_rpm = await self.get_recent_requests_per_minute(project_id)
        return recent_rpm >= RECENT_ACTIVITY_MIN_RPM

    async def get_demand_level(
        self,
        project_id: str,
        current_proxy_count: int,
        previous_level: DemandLevel | None = None,
    ) -> DemandLevel:
        """Calculate the current demand level for a project.

        Uses hysteresis thresholds to prevent oscillation between levels.
        The thresholds for transitioning up are higher than those for
        transitioning down, creating a dead zone that stabilizes scaling.

        Args:
            project_id: The project ID to calculate demand for.
            current_proxy_count: Number of active proxies for the project.
            previous_level: The previous demand level (for hysteresis).
                If None (first check), uses conservative thresholds.

        Returns:
            DemandLevel enum value.
        """
        requests_per_minute = await self.get_requests_per_minute(project_id)

        # Avoid division by zero
        if current_proxy_count <= 0:
            # No proxies - if there are requests, demand is HIGH
            if requests_per_minute > 0:
                return DemandLevel.HIGH
            return DemandLevel.LOW

        # Calculate per-proxy rate
        rate_per_proxy = requests_per_minute / current_proxy_count

        # Apply hysteresis based on previous level
        if previous_level is None or previous_level == DemandLevel.LOW:
            # From LOW (or first check): use scale-up thresholds
            if rate_per_proxy > DEMAND_THRESHOLDS["medium_to_high"]:
                return DemandLevel.HIGH
            elif rate_per_proxy > DEMAND_THRESHOLDS["low_to_medium"]:
                return DemandLevel.MEDIUM
            else:
                return DemandLevel.LOW
        elif previous_level == DemandLevel.MEDIUM:
            # From MEDIUM: need to clearly break out in either direction
            if rate_per_proxy > DEMAND_THRESHOLDS["medium_to_high"]:
                return DemandLevel.HIGH
            elif rate_per_proxy < DEMAND_THRESHOLDS["medium_to_low"]:
                return DemandLevel.LOW
            else:
                return DemandLevel.MEDIUM
        else:  # HIGH
            # From HIGH: need to clearly drop to leave
            if rate_per_proxy < DEMAND_THRESHOLDS["medium_to_low"]:
                return DemandLevel.LOW
            elif rate_per_proxy < DEMAND_THRESHOLDS["high_to_medium"]:
                return DemandLevel.MEDIUM
            else:
                return DemandLevel.HIGH
    
    async def get_demand_info(
        self,
        project_id: str,
        current_proxy_count: int,
        previous_level: DemandLevel | None = None,
    ) -> dict:
        """Get comprehensive demand information for a project.

        Args:
            project_id: The project ID.
            current_proxy_count: Number of active proxies.
            previous_level: The previous demand level (for hysteresis).

        Returns:
            Dict with demand_level, requests_per_minute, rate_per_proxy.
        """
        requests_per_minute = await self.get_requests_per_minute(project_id)

        rate_per_proxy = 0.0
        if current_proxy_count > 0:
            rate_per_proxy = requests_per_minute / current_proxy_count

        demand_level = await self.get_demand_level(
            project_id, current_proxy_count, previous_level
        )

        return {
            "demand_level": demand_level,
            "requests_per_minute": requests_per_minute,
            "rate_per_proxy": rate_per_proxy,
            "proxy_count": current_proxy_count,
        }

