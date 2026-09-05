# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class and contracts for proxy providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class SyncableProvider(Protocol):
    """Contract driven by ``ProxyProviderSyncer``.

    Implemented by :class:`~api.providers.sdk.provider.DescriptorProvider` and
    by any plugin provider class registered through the ``octoprox.providers``
    entry point group.
    """

    def is_session_based(self) -> bool:
        """Session-based providers have no per-proxy IPs to refresh."""
        ...

    def needs_periodic_sync(self) -> bool:
        """Whether the periodic refresh should also reconcile additions (list mode)."""
        ...

    async def sync_proxies(self, existing_proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        """Return ``(proxies_to_add, proxy_ids_to_remove)`` to match configuration."""
        ...

    async def refresh_ips(self, proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        """Return ``(updated_proxies, proxy_ids_to_remove)`` after re-checking IPs."""
        ...


class ProxyProvider:
    """Base class for proxy providers.

    All providers take a connector and credential.
    """

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        self.connector = connector
        self.credential = credential

    def needs_periodic_sync(self) -> bool:
        return False
