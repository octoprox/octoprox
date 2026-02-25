# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Domain-based filtering for connector routing.

Provides domain matching logic for whitelist/blacklist rules on connectors.
Domain matching is hierarchical: 'bing.com' matches 'bing.com' and all
subdomains (www.bing.com, images.bing.com, etc.).
"""

from api.models.connector import RoutingConfig


def matches_domain(target_host: str, pattern: str) -> bool:
    """Check if a target host matches a domain pattern.

    Matching is hierarchical:
    - 'bing.com' matches 'bing.com', 'www.bing.com', 'x.y.bing.com'
    - 'x.bing.com' matches 'x.bing.com', 'sub.x.bing.com'

    Args:
        target_host: The target hostname from the request (e.g., 'www.bing.com').
        pattern: The domain pattern to match against (e.g., 'bing.com').

    Returns:
        True if the target host matches the pattern.
    """
    target = target_host.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    return target == pattern or target.endswith("." + pattern)


def is_domain_allowed(target_host: str, routing_config: RoutingConfig) -> bool:
    """Check if a domain is allowed by a connector's routing config.

    Rules:
    - Both lists empty -> allow all domains
    - Whitelist non-empty -> domain must match at least one whitelist entry
    - Blacklist non-empty -> domain must not match any blacklist entry

    Args:
        target_host: The target hostname from the request.
        routing_config: The connector's routing configuration.

    Returns:
        True if the domain is allowed through this connector.
    """
    if routing_config.domain_whitelist:
        return any(
            matches_domain(target_host, pattern)
            for pattern in routing_config.domain_whitelist
        )

    if routing_config.domain_blacklist:
        return not any(
            matches_domain(target_host, pattern)
            for pattern in routing_config.domain_blacklist
        )

    # No restrictions
    return True
