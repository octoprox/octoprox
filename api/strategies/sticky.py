# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sticky session routing strategy."""

import hashlib
import random

from api.models.proxy import Proxy
from api.strategies.base import RoutingStrategy


class StickySessionStrategy(RoutingStrategy):
    """Maintains the same proxy for a given session/client."""
    
    def __init__(self) -> None:
        self._session_map: dict[str, str] = {}
    
    @property
    def name(self) -> str:
        return "sticky"
    
    def select(self, proxies: list[Proxy], session_id: str | None = None) -> Proxy | None:
        if not proxies:
            return None
        
        if session_id is None:
            # No session, fall back to random
            return random.choice(proxies)
        
        # Check if we have a cached proxy for this session
        if session_id in self._session_map:
            cached_proxy_id = self._session_map[session_id]
            # Find the proxy in the available list
            for proxy in proxies:
                if proxy.id == cached_proxy_id:
                    return proxy
            # Cached proxy no longer available, remove from map
            del self._session_map[session_id]
        
        # Use consistent hashing to select a proxy
        hash_value = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
        index = hash_value % len(proxies)
        selected = proxies[index]
        
        # Cache the selection
        self._session_map[session_id] = selected.id
        
        return selected
    
    def reset(self) -> None:
        self._session_map.clear()

