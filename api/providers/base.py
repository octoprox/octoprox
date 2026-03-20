# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class for proxy providers."""

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy, ProxyStatus

# Proxy statuses ordered from healthiest to least healthy.
# Proxies with lower priority values are kept; higher values are removed first.
_STATUS_REMOVAL_PRIORITY: dict[ProxyStatus, int] = {
    ProxyStatus.HEALTHY: 0,
    ProxyStatus.INITIALIZING: 1,
    ProxyStatus.UNKNOWN: 2,
    ProxyStatus.DEGRADED: 3,
    ProxyStatus.DRAINING: 4,
    ProxyStatus.UNHEALTHY: 5,
    ProxyStatus.TERMINATING: 6,
}


def _sort_proxies_healthy_first(proxies: list[Proxy]) -> list[Proxy]:
    """Sort proxies so healthy ones come first and unhealthy ones are at the tail.

    When reducing proxy count, slicing from the end of this sorted list
    ensures unhealthy proxies are removed before healthy ones.
    """
    return sorted(proxies, key=lambda p: _STATUS_REMOVAL_PRIORITY.get(p.status, 2))


class ProxyProvider:
    """Base class for proxy providers.

    All providers take a connector and credential.
    """

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        self.connector = connector
        self.credential = credential
