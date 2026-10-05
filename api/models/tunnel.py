# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every tunnel protocol's devices report about their traffic.

A device's requests are metered on the proxy path like any other, keyed by
the device (see ``TunnelPeerMetricDelta``), so these shapes do not depend on
the protocol the device arrived through. WireGuard uses them today; another
tunnel protocol reports the same.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime

from pydantic import BaseModel

from api.core.stats import TunnelPeerMetricDelta


def clean_optional(value: str | None) -> str | None:
    """A stripped string, or None for None and for whitespace."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def validate_endpoint_host(value: str) -> str:
    """A bare hostname or IP literal (v4 or v6), never a URL or host:port pair."""
    value = value.strip().strip("[]")
    if not value:
        return ""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return value
    if any(ch in value for ch in " /:\\@"):
        raise ValueError("endpoint_host is a hostname or IP address without a port or scheme")
    return value


def validate_tunnel_subnet(value: str, example: str) -> str:
    """An IPv4 network with room for the gateway and at least one device, in canonical form."""
    try:
        network = ipaddress.IPv4Network(value.strip(), strict=True)
    except ValueError as exc:
        raise ValueError(f"subnet must be an IPv4 network such as {example}: {exc}") from None
    if network.prefixlen > 30:
        raise ValueError("subnet must leave room for the gateway and at least one device (/30 or larger)")
    if network.is_loopback or network.is_multicast:
        raise ValueError("subnet must be a unicast network")
    return str(network)


class TunnelPeerMetrics(BaseModel):
    """A device's totals: history plus the current window, cluster-wide.

    Bytes are what the proxy path relayed for the device, the same numbers
    that count towards the project's and the connectors' totals; the tunnel
    interface's own counters (handshakes, keepalives, DNS) are not in them.
    """

    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0
    # Connections relayed by address because no destination name could be
    # recovered, and encrypted-DNS connections closed (see docs/wireguard.md).
    connections_by_address: int = 0
    encrypted_dns_blocked: int = 0

    @classmethod
    def from_delta(cls, delta: TunnelPeerMetricDelta | None) -> TunnelPeerMetrics:
        if delta is None:
            return cls()
        return cls(
            request_count=delta.request_count,
            success_count=delta.success_count,
            failure_count=delta.failure_count,
            avg_latency_ms=round(delta.avg_latency_ms, 2),
            bytes_sent=delta.bytes_sent,
            bytes_received=delta.bytes_received,
            connections_by_address=delta.by_address,
            encrypted_dns_blocked=delta.encrypted_dns_blocked,
        )


class TunnelPeerMetricsSnapshot(TunnelPeerMetrics):
    """One interval of a device's history."""

    timestamp: datetime


class TunnelPeerMetricsHistoryResponse(BaseModel):
    snapshots: list[TunnelPeerMetricsSnapshot]
