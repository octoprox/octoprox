"""Health-based routing strategy."""

import random

from api.models.proxy import Proxy, ProxyStatus
from api.strategies.base import RoutingStrategy


class HealthBasedStrategy(RoutingStrategy):
    """Routes to proxies based on health and performance metrics."""
    
    @property
    def name(self) -> str:
        return "health_based"
    
    def select(self, proxies: list[Proxy], session_id: str | None = None) -> Proxy | None:
        if not proxies:
            return None
        
        # Filter to only healthy proxies
        healthy = [p for p in proxies if p.status == ProxyStatus.HEALTHY]
        
        if not healthy:
            # Fall back to degraded proxies if no healthy ones
            healthy = [p for p in proxies if p.status == ProxyStatus.DEGRADED]
        
        if not healthy:
            # Last resort: any proxy
            healthy = proxies
        
        # Score proxies based on success rate and latency
        scored = []
        for proxy in healthy:
            score = self._calculate_score(proxy)
            scored.append((proxy, score))
        
        # Sort by score (higher is better)
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Select from top performers with some randomization
        top_count = max(1, len(scored) // 3)
        top_proxies = [p for p, _ in scored[:top_count]]
        
        return random.choice(top_proxies)
    
    def _calculate_score(self, proxy: Proxy) -> float:
        """Calculate a health score for a proxy."""
        score = 0.0
        
        # Success rate contributes 60% of score
        score += proxy.success_rate * 0.6
        
        # Latency contributes 40% (lower is better)
        if proxy.avg_latency_ms > 0:
            # Normalize latency: 100ms = 100 points, 1000ms = 10 points
            latency_score = max(0, 100 - (proxy.avg_latency_ms / 10))
            score += latency_score * 0.4
        else:
            # Unknown latency, give neutral score
            score += 50 * 0.4
        
        return score

