# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Transfers open through this gateway right now, per proxy and per connector, which ``least_used`` routes on."""

from __future__ import annotations


class InFlight:
    """Counts of open transfers, per proxy and per connector; one per gateway, shared with request metering."""

    def __init__(self) -> None:
        # Only ids with a transfer open: an entry goes when its count reaches zero.
        self._proxies: dict[str, int] = {}
        self._connectors: dict[str, int] = {}

    def begin(self, proxy_id: str, connector_id: str) -> None:
        """Count a transfer that starts through ``proxy_id`` of ``connector_id``."""
        self._proxies[proxy_id] = self._proxies.get(proxy_id, 0) + 1
        self._connectors[connector_id] = self._connectors.get(connector_id, 0) + 1

    def end(self, proxy_id: str, connector_id: str) -> None:
        """Stop counting a transfer ``begin`` counted; an end with nothing open is ignored."""
        _decrement(self._proxies, proxy_id)
        _decrement(self._connectors, connector_id)

    def of_proxy(self, proxy_id: str) -> int:
        """Transfers open through the proxy on this gateway."""
        return self._proxies.get(proxy_id, 0)

    def of_connector(self, connector_id: str) -> int:
        """Transfers open through any proxy of the connector on this gateway."""
        return self._connectors.get(connector_id, 0)

    @property
    def total(self) -> int:
        """Transfers open on this gateway, for its snapshot."""
        return sum(self._proxies.values())


def _decrement(counts: dict[str, int], key: str) -> None:
    """One fewer for ``key``, never below zero; the entry goes at zero."""
    count = counts.get(key, 0)
    if count <= 1:
        counts.pop(key, None)
    else:
        counts[key] = count - 1
