"""Demand tracking for auto-scaling decisions.

This module tracks request rates per project using a sliding window
approach stored in Redis, enabling demand-based auto-scaling decisions.

Subscribes to request_completed signals to track demand automatically.
"""

from enum import Enum

import structlog

from api.core import utc_now
from api.core.signals import request_completed
from api.db.redis import RedisClient

logger = structlog.get_logger()

# Redis key for demand tracking (sorted set with timestamps)
DEMAND_KEY = "project:demand:{project_id}"
# Window size for demand calculation (in seconds)
DEMAND_WINDOW_SECONDS = 60
# TTL for demand entries (slightly longer than window to allow cleanup)
DEMAND_TTL_SECONDS = 120


class DemandLevel(str, Enum):
    """Demand level classification for auto-scaling."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Thresholds for demand classification (requests per minute per proxy)
# These can be adjusted based on real-world usage patterns
DEMAND_THRESHOLDS = {
    "low_max": 10,      # <= 10 req/min/proxy = LOW
    "medium_max": 30,   # <= 30 req/min/proxy = MEDIUM, > 30 = HIGH
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

    def subscribe_to_signals(self) -> None:
        """Subscribe to signals for automatic demand tracking."""
        request_completed.connect(self._on_request_completed)
        logger.debug("DemandTracker subscribed to request_completed signal")

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
    
    async def get_demand_level(
        self, 
        project_id: str, 
        current_proxy_count: int
    ) -> DemandLevel:
        """Calculate the current demand level for a project.
        
        Demand level is based on requests per minute per proxy:
        - LOW: <= 10 req/min/proxy
        - MEDIUM: 10-30 req/min/proxy  
        - HIGH: > 30 req/min/proxy
        
        Args:
            project_id: The project ID to calculate demand for.
            current_proxy_count: Number of active proxies for the project.
            
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
        
        if rate_per_proxy <= DEMAND_THRESHOLDS["low_max"]:
            return DemandLevel.LOW
        elif rate_per_proxy <= DEMAND_THRESHOLDS["medium_max"]:
            return DemandLevel.MEDIUM
        else:
            return DemandLevel.HIGH
    
    async def get_demand_info(
        self, 
        project_id: str, 
        current_proxy_count: int
    ) -> dict:
        """Get comprehensive demand information for a project.
        
        Args:
            project_id: The project ID.
            current_proxy_count: Number of active proxies.
            
        Returns:
            Dict with demand_level, requests_per_minute, rate_per_proxy.
        """
        requests_per_minute = await self.get_requests_per_minute(project_id)
        
        rate_per_proxy = 0.0
        if current_proxy_count > 0:
            rate_per_proxy = requests_per_minute / current_proxy_count
        
        demand_level = await self.get_demand_level(project_id, current_proxy_count)
        
        return {
            "demand_level": demand_level,
            "requests_per_minute": requests_per_minute,
            "rate_per_proxy": rate_per_proxy,
            "proxy_count": current_proxy_count,
        }

