# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every tunnel protocol shares: device traffic, gateway endpoints, addresses and errors.

A device's requests are metered on the proxy path like any other, keyed by
the device (see ``TunnelPeerMetricDelta``), so the traffic shapes do not
depend on the protocol the device arrived through. Neither does the rest of
what is here:

- how a gateway reports the endpoint it runs for a protocol
  (:class:`TunnelEndpointState`, carried in its heartbeat) and how the api
  shows a protocol across every gateway (:class:`TunnelStatus`);
- how a new device gets its tunnel address (:func:`first_free_address`);
- the errors the protocols' services raise for the admin API to map.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.core.stats import TunnelPeerMetricDelta

# The tunnel protocols, as they name their tables, their Redis status keys and
# their endpoint states.
TunnelProtocol = Literal["wireguard", "openvpn"]

# What a gateway's endpoint is doing: bringing the tunnel up (also while it
# waits for the feed to carry the protocol's settings), carrying it, failed
# (it says why, and tries again), or stopped with the gateway.
TunnelEndpointPhase = Literal["starting", "running", "failed", "stopped"]


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


def first_free_address(network: ipaddress.IPv4Network, used: Container[str]) -> str:
    """The lowest host of ``network`` no device holds, skipping the gateway (the first host).

    ``used`` is every address the protocol's devices hold, read from Postgres
    in the transaction that inserts the new device: the unique index on the
    address column turns a device created elsewhere at the same moment into
    an integrity error rather than a shared address. Raises
    :class:`TunnelAddressError` when every host is taken.
    """
    hosts = network.hosts()
    next(hosts, None)
    for candidate in hosts:
        address = str(candidate)
        if address not in used:
            return address
    raise TunnelAddressError(f"no free address left in {network}")


def naive_utc(epoch: int) -> datetime:
    """Unix seconds as naive UTC, like every other timestamp the API serialises."""
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


def device_changes(update: BaseModel) -> dict[str, Any]:
    """The fields a partial device update sets, as either protocol's update model carries them.

    Only the fields the request named. An empty string clears an optional
    field (session, country, state, city); name and enabled cannot be
    cleared, so an explicit null for them is ignored rather than becoming a
    NOT NULL error.
    """
    return {
        key: (None if value == "" else value)
        for key, value in update.model_dump(exclude_unset=True).items()
        if not (value is None and key in ("name", "enabled"))
    }


class TunnelValidationError(ValueError):
    """A device or settings change the install cannot take as asked.

    A name or public key another device holds, a location without its
    country, or a subnet that overlaps the other protocol's or would strand
    a device. The admin API answers 400 with the message, which is written
    for the operator.
    """


class TunnelAddressError(RuntimeError):
    """No tunnel address for a new device.

    Either the subnet is full, or a device created at the same moment took
    the address this one was given. The admin API answers 409 with the
    message; in the second case trying again succeeds.
    """


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


# --- endpoints and status --------------------------------------------------------------------


class TunnelEndpointState(BaseModel):
    """What one gateway's endpoint for one tunnel protocol is doing.

    Every gateway with a protocol enabled runs an endpoint for it and puts
    this in its heartbeat snapshot, so the api can show the protocol per
    gateway without asking any of them. Everything here is the gateway's own
    view: its listener, its daemon, its counters since it started.
    """

    protocol: TunnelProtocol
    instance_id: str
    state: TunnelEndpointPhase
    # Why the tunnel is not up, or what last went wrong applying a change to
    # it (with the daemon's last output for OpenVPN); None while all is well.
    error: str | None = None
    # WireGuard: the kernel module or wireguard-go, once the interface is up.
    backend: Literal["kernel", "userspace"] | None = None
    # The port devices reach this gateway on: its own override, else the
    # install's endpoint port; None until the feed carries the settings.
    listen_port: int | None = None
    # Connections the gateway's tunnel listener is relaying right now, every
    # tunnel protocol included (the listener is shared).
    active_connections: int = 0
    # OpenVPN: times the daemon exited on its own and was started again, and
    # connections refused at admission, since the gateway started.
    restarts: int = 0
    denied: int = 0
    daemon_version: str | None = None
    # The gateway's tunnel configuration: the interface it creates, the range
    # its resolver answers from, and whether it closes encrypted-DNS
    # connections. Per gateway, since each is set on the gateway's process.
    interface: str | None = None
    fake_ip_range: str | None = None
    block_encrypted_dns: bool = True


class TunnelStatus(BaseModel):
    """A tunnel protocol across the install: every gateway carrying it, and its devices."""

    # One entry per live gateway that runs the protocol's endpoint, by instance
    # id; empty when no gateway does.
    gateways: list[TunnelEndpointState] = Field(default_factory=list)
    peers_total: int = 0
    peers_enabled: int = 0
    # Devices a gateway currently carries (OpenVPN) or that handshaked
    # recently (WireGuard), merged across gateways.
    peers_online: int = 0
    # Degradation signals summed over every device of the protocol, all time
    # (see docs/wireguard.md); each device's own numbers are in its metrics.
    connections_by_address: int = 0
    encrypted_dns_blocked: int = 0


# Every live gateway's endpoint states, as their heartbeats report them
# (instance_stats:*), whatever the protocol; the api's composition reads them.
EndpointStates = Callable[[], Awaitable[list[TunnelEndpointState]]]
# Each device's traffic totals by device id: Postgres totals plus the current
# Redis window, as the api's read model computes them for every metered
# entity. Devices without traffic may be left out.
DeviceTotals = Callable[[Sequence[str]], Awaitable[Mapping[str, TunnelPeerMetricDelta]]]
