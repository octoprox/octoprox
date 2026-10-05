# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""OpenVPN models: the install's CA and server identity, and the devices that may connect.

The same shape as the WireGuard models with the credential swapped: a
device holds a certificate the install's CA issued instead of a key pair,
and is told apart at connect time by its common name (its id) and the
certificate's serial. Routing is stored on the peer as for WireGuard. See
docs/openvpn.md.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from api.core import utc_now
from api.models.location import LocationTarget, normalize_level
from api.models.tunnel import (
    TunnelPeerMetrics,
    clean_optional,
    validate_endpoint_host,
    validate_tunnel_subnet,
)

DEFAULT_SUBNET = "10.67.0.0/16"
DEFAULT_ENDPOINT_PORT = 1194
OpenVpnProtocol = Literal["udp", "tcp"]


def validate_subnet(value: str) -> str:
    return validate_tunnel_subnet(value, DEFAULT_SUBNET)


# --- server ----------------------------------------------------------------------------------


class OpenVpnServerSettings(BaseModel):
    """The install-wide OpenVPN identity and the defaults every device profile carries."""

    ca_cert: str
    ca_key: str
    server_cert: str
    server_key: str
    tls_crypt_key: str
    # Where devices reach the endpoint: a public hostname or IP. Empty until the
    # admin sets it; profiles cannot be completed before.
    endpoint_host: str = ""
    endpoint_port: int = Field(default=DEFAULT_ENDPOINT_PORT, ge=1, le=65535)
    # One transport per install: the daemon listens on one, every profile names it.
    protocol: OpenVpnProtocol = "udp"
    subnet: str = DEFAULT_SUBNET
    # ``keepalive interval timeout`` on the daemon, pushed to devices.
    keepalive_interval: int = Field(default=10, ge=1, le=600)
    keepalive_timeout: int = Field(default=60, ge=2, le=3600)
    # Written into device profiles as tun-mtu when set.
    client_mtu: int | None = Field(default=None, ge=1280, le=1500)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("subnet")
    @classmethod
    def _subnet(cls, value: str) -> str:
        return validate_subnet(value)

    @field_validator("endpoint_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return validate_endpoint_host(value)

    @model_validator(mode="after")
    def _keepalive(self) -> OpenVpnServerSettings:
        if self.keepalive_timeout <= self.keepalive_interval:
            raise ValueError("keepalive_timeout must be longer than keepalive_interval")
        return self

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet)

    @property
    def gateway(self) -> str:
        """The daemon's own tunnel address: the first host of the subnet."""
        return str(next(self.network.hosts()))

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_host)

    def daemon_signature(self) -> tuple[Any, ...]:
        """What the daemon was started with; a change means it must be restarted."""
        return (
            self.ca_cert, self.server_cert, self.tls_crypt_key, self.endpoint_port, self.protocol, self.subnet,
            self.keepalive_interval, self.keepalive_timeout,
        )


class OpenVpnServerSettingsDoc(BaseModel):
    """What an admin edits: everything but the identity."""

    endpoint_host: str = ""
    endpoint_port: int = Field(default=DEFAULT_ENDPOINT_PORT, ge=1, le=65535)
    protocol: OpenVpnProtocol = "udp"
    subnet: str = DEFAULT_SUBNET
    keepalive_interval: int = Field(default=10, ge=1, le=600)
    keepalive_timeout: int = Field(default=60, ge=2, le=3600)
    client_mtu: int | None = Field(default=None, ge=1280, le=1500)

    @field_validator("subnet")
    @classmethod
    def _subnet(cls, value: str) -> str:
        return validate_subnet(value)

    @field_validator("endpoint_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return validate_endpoint_host(value)

    @model_validator(mode="after")
    def _keepalive(self) -> OpenVpnServerSettingsDoc:
        if self.keepalive_timeout <= self.keepalive_interval:
            raise ValueError("keepalive_timeout must be longer than keepalive_interval")
        return self


OpenVpnState = Literal["disabled", "starting", "running", "failed", "stopped"]


class OpenVpnStatus(BaseModel):
    """What the instance answering the request is doing about the endpoint.

    In a cluster this describes the instance the API load balancer picked,
    which may or may not be one carrying the endpoint. ``peers_online`` is
    merged from what every carrying instance publishes to Redis and is
    cluster-wide.
    """

    enabled: bool
    state: OpenVpnState
    error: str | None = None
    instance_id: str
    interface: str
    protocol: OpenVpnProtocol
    listen_port: int | None = None
    daemon_version: str | None = None
    # Times the daemon exited on its own and was started again since this process started.
    restarts: int = 0
    # Connections refused at the management interface (unknown, disabled or rotated device) since start.
    denied: int = 0
    transparent_port: int
    dns_port: int
    fake_ip_range: str
    active_connections: int = 0
    peers_total: int = 0
    peers_enabled: int = 0
    peers_online: int = 0
    connections_by_address: int = 0
    encrypted_dns_blocked: int = 0
    block_encrypted_dns: bool = True


class OpenVpnServerSettingsResponse(BaseModel):
    ca_fingerprint: str
    ca_expires_at: datetime
    endpoint_host: str
    endpoint_port: int
    protocol: OpenVpnProtocol
    subnet: str
    gateway: str
    keepalive_interval: int
    keepalive_timeout: int
    client_mtu: int | None
    configured: bool
    updated_at: datetime
    status: OpenVpnStatus


# --- peers -----------------------------------------------------------------------------------


class OpenVpnPeer(BaseModel):
    """A device that may connect, and how its traffic is routed."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    name: str
    # Issued by the install's CA with the id as common name; the key is kept
    # so the profile can be shown again (no bring-your-own for OpenVPN).
    certificate: str
    private_key: str
    # Decimal serial of ``certificate``: a rotated device keeps its common
    # name, so the serial is what tells the current certificate from an old one.
    serial: str
    certificate_expires_at: datetime
    address: str
    enabled: bool = True
    session_id: str | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    # When the device last connected and from where, as persisted by the
    # instance that carried the session. Live readings are merged over these.
    last_connected_at: datetime | None = None
    last_endpoint: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def location(self) -> LocationTarget | None:
        if not self.country:
            return None
        return LocationTarget(country=self.country, state=self.state, city=self.city)


class OpenVpnPeerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    session_id: str | None = Field(default=None, max_length=255)
    country: str | None = None
    state: str | None = None
    city: str | None = None

    @field_validator("name", "session_id", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return clean_optional(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _location(self) -> OpenVpnPeerCreate:
        target = LocationTarget.parse(self.country, self.state, self.city)
        self.country, self.state, self.city = (target.country, target.state, target.city) if target else (None, None, None)
        return self


class OpenVpnPeerUpdate(BaseModel):
    """Partial update. An empty string clears session_id, country, state or city."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    session_id: str | None = Field(default=None, max_length=255)
    country: str | None = None
    state: str | None = None
    city: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("country", "state", "city")
    @classmethod
    def _level(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or value == "":
            return value
        return normalize_level(info.field_name or "", value) or ""

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class OpenVpnPeerStatus(BaseModel):
    """Where a device stands: whether a carrier has a session for it, and when it was last seen.

    ``connected_since`` and ``endpoint`` come from the instance carrying the
    session when one is publishing; ``last_seen_at`` is that, or the
    persisted last connection otherwise, so a device's last sighting
    survives a restart. ``rx_bytes`` / ``tx_bytes`` are the daemon's counters
    for the current session: wire bytes, control channel and DNS included.
    What the device's traffic amounted to is in its metrics.
    """

    online: bool = False
    connected_since: datetime | None = None
    last_seen_at: datetime | None = None
    endpoint: str | None = None
    live: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0


class OpenVpnPeerResponse(BaseModel):
    id: str
    project_id: str
    name: str
    serial: str
    certificate_expires_at: datetime
    address: str
    enabled: bool
    session_id: str | None
    country: str | None
    state: str | None
    city: str | None
    created_at: datetime
    updated_at: datetime
    status: OpenVpnPeerStatus
    metrics: TunnelPeerMetrics


class OpenVpnPeerListResponse(BaseModel):
    total: int
    peers: list[OpenVpnPeerResponse]
    # Whether profiles can be issued: the admin has set the public endpoint.
    server_configured: bool
    ca_fingerprint: str


class OpenVpnPeerConfigResponse(BaseModel):
    """A device's ``.ovpn`` profile."""

    filename: str
    config: str
    complete: bool
    server_configured: bool
