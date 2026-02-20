# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Routing strategies for proxy selection."""

from api.strategies.base import RoutingStrategy
from api.strategies.health_based import HealthBasedStrategy
from api.strategies.least_used import LeastUsedStrategy
from api.strategies.random import RandomStrategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy

__all__ = [
    "RoutingStrategy",
    "RoundRobinStrategy",
    "LeastUsedStrategy",
    "RandomStrategy",
    "StickySessionStrategy",
    "HealthBasedStrategy",
    "get_strategy",
]

_STRATEGIES: dict[str, type[RoutingStrategy]] = {
    "round_robin": RoundRobinStrategy,
    "least_used": LeastUsedStrategy,
    "random": RandomStrategy,
    "sticky": StickySessionStrategy,
    "health_based": HealthBasedStrategy,
}


def get_strategy(name: str) -> RoutingStrategy:
    """Get a routing strategy by name."""
    strategy_class = _STRATEGIES.get(name.lower())
    if strategy_class is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(_STRATEGIES.keys())}")
    return strategy_class()

