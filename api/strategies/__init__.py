# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Routing strategies for proxy selection: one object per project on a gateway, built by name (:func:`get_strategy`)."""

from collections.abc import Callable

from api.routing.inflight import InFlight
from api.routing.outcomes import LocalOutcomes
from api.strategies.base import ProxyGroup, RoutingStrategy
from api.strategies.health_based import HealthBasedStrategy
from api.strategies.least_used import LeastUsedStrategy
from api.strategies.random import RandomStrategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy

__all__ = [
    "STRATEGY_NAMES",
    "ProxyGroup",
    "RoutingStrategy",
    "RoundRobinStrategy",
    "LeastUsedStrategy",
    "RandomStrategy",
    "StickySessionStrategy",
    "HealthBasedStrategy",
    "get_strategy",
    "served_strategy",
    "strategy_name",
]

# Each strategy by the name projects store, simplest first (the order the
# Overview lists them), built from the gateway's counters it may route on.
_STRATEGIES: dict[str, Callable[[InFlight | None, LocalOutcomes | None], RoutingStrategy]] = {
    "round_robin": lambda inflight, outcomes: RoundRobinStrategy(),
    "least_used": lambda inflight, outcomes: LeastUsedStrategy(inflight),
    "random": lambda inflight, outcomes: RandomStrategy(),
    "sticky": lambda inflight, outcomes: StickySessionStrategy(),
    "health_based": lambda inflight, outcomes: HealthBasedStrategy(outcomes),
}
# The names a project's routing_strategy may hold, simplest first.
STRATEGY_NAMES = tuple(_STRATEGIES)


def get_strategy(
    name: str, *, inflight: InFlight | None = None, outcomes: LocalOutcomes | None = None
) -> RoutingStrategy:
    """A new strategy object for ``name``; ValueError for a name no strategy has.

    ``inflight`` and ``outcomes`` are the gateway's counters, read by the
    strategies that route on them and ignored by the rest.
    """
    return _STRATEGIES[strategy_name(name)](inflight, outcomes)


def strategy_name(name: str) -> str:
    """``name`` as routers know it, in lower case; ValueError for a name no strategy has."""
    known = name.lower()
    if known not in _STRATEGIES:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(_STRATEGIES)}")
    return known


def served_strategy(name: str, default: str) -> str:
    """The strategy gateways route a project storing ``name`` with: ``name`` as routers know it, else ``default``."""
    try:
        return strategy_name(name)
    except ValueError:
        return default
