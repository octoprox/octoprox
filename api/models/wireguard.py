# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard models: the install's server identity and the devices that may connect.

A device joins a project by becoming a *peer*: it gets a tunnel address, and
everything it sends through the tunnel is routed as if it had authenticated
with that project's proxy credentials. What a proxy client would put in its
username (``-sessid-``, ``-cc-``, ``-st-``, ``-city-``) is stored on the peer,
because a TV or a router cannot say it any other way. See docs/wireguard.md.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from api.core import utc_now
from api.models.location import LocationTarget, normalize_level
from api.wireguard import keys

# Reserved RFC 2544 benchmarking range, the conventional pool for synthetic
# "fake IP" DNS answers (sing-box and Clash use it too).
DEFAULT_FAKE_IP_RANGE = "198.18.0.0/15"
DEFAULT_SUBNET = "10.66.0.0/16"
DEFAULT_ENDPOINT_PORT = 51820


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# --- server ----------------------------------------------------------------------------------


class WireGuardServerSettings(BaseModel):
    """The install-wide WireGuard identity and the defaults every device config carries."""

    private_key: str
    public_key: str
    # Where devices reach the tunnel: a public hostname or IP. Empty until the
    # admin sets it; configs cannot be issued before.
    endpoint_host: str = ""
    endpoint_port: int = Field(default=DEFAULT_ENDPOINT_PORT, ge=1, le=65535)
    # Tunnel addresses: the first host is the gateway (this server), the rest go to peers.
    subnet: str = DEFAULT_SUBNET
    persistent_keepalive: int = Field(default=25, ge=0, le=3600)
    # Written into device configs as MTU when set; WireGuard's own default otherwise.
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

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet)

    @property
    def gateway(self) -> str:
        """The server's own tunnel address: the first host of the subnet."""
        return str(next(self.network.hosts()))

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_host)


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


def validate_subnet(value: str) -> str:
    """An IPv4 network with room for the gateway and at least one peer, in canonical form."""
    try:
        network = ipaddress.IPv4Network(value.strip(), strict=True)
    except ValueError as exc:
        raise ValueError(f"subnet must be an IPv4 network such as {DEFAULT_SUBNET}: {exc}") from None
    if network.prefixlen > 30:
        raise ValueError("subnet must leave room for the gateway and at least one device (/30 or larger)")
    if network.is_loopback or network.is_multicast:
        raise ValueError("subnet must be a unicast network")
    return str(network)


class WireGuardServerSettingsDoc(BaseModel):
    """What an admin edits: everything but the key pair."""

    endpoint_host: str = ""
    endpoint_port: int = Field(default=DEFAULT_ENDPOINT_PORT, ge=1, le=65535)
    subnet: str = DEFAULT_SUBNET
    persistent_keepalive: int = Field(default=25, ge=0, le=3600)
    client_mtu: int | None = Field(default=None, ge=1280, le=1500)

    @field_validator("subnet")
    @classmethod
    def _subnet(cls, value: str) -> str:
        return validate_subnet(value)

    @field_validator("endpoint_host")
    @classmethod
    def _host(cls, value: str) -> str:
        return validate_endpoint_host(value)


WireGuardState = Literal["disabled", "starting", "running", "failed", "stopped"]


class WireGuardStatus(BaseModel):
    """What the instance answering the request is doing about the tunnel.

    In a cluster this describes the instance the API load balancer picked,
    which may or may not be one carrying the tunnel. ``peers_online`` is
    merged from what every carrying instance publishes to Redis and is
    cluster-wide.
    """

    enabled: bool
    state: WireGuardState
    error: str | None = None
    instance_id: str
    interface: str
    backend: Literal["kernel", "userspace"] | None = None
    listen_port: int | None = None
    transparent_port: int
    dns_port: int
    fake_ip_range: str
    active_connections: int = 0
    peers_total: int = 0
    peers_enabled: int = 0
    peers_online: int = 0
    # Degradation signals on this instance since it started (see docs/wireguard.md).
    connections_by_address: int = 0
    encrypted_dns_blocked: int = 0
    block_encrypted_dns: bool = True


class WireGuardServerSettingsResponse(BaseModel):
    public_key: str
    endpoint_host: str
    endpoint_port: int
    subnet: str
    gateway: str
    persistent_keepalive: int
    client_mtu: int | None
    configured: bool
    updated_at: datetime
    status: WireGuardStatus


# --- peers -----------------------------------------------------------------------------------


class WireGuardPeer(BaseModel):
    """A device that may connect, and how its traffic is routed."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    name: str
    public_key: str
    private_key: str | None = None
    preshared_key: str | None = None
    address: str
    enabled: bool = True
    session_id: str | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def location(self) -> LocationTarget | None:
        """The exit this device's traffic must use, as a proxy client's ``-cc-`` suffixes would say it."""
        if not self.country:
            return None
        return LocationTarget(country=self.country, state=self.state, city=self.city)


class WireGuardPeerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Bring-your-own key: the device keeps its private key and only the public
    # half is registered. None has Octoprox generate the pair and keep it, so
    # the config can be shown again.
    public_key: str | None = None
    preshared: bool = True
    enabled: bool = True
    session_id: str | None = Field(default=None, max_length=255)
    country: str | None = None
    state: str | None = None
    city: str | None = None

    @field_validator("name", "session_id", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return _clean(value) if isinstance(value, str) else value

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str | None) -> str | None:
        value = _clean(value)
        if value is not None and not keys.is_valid_key(value):
            raise ValueError("public_key must be a base64 WireGuard key (44 characters)")
        return value

    @model_validator(mode="after")
    def _location(self) -> WireGuardPeerCreate:
        target = LocationTarget.parse(self.country, self.state, self.city)
        self.country, self.state, self.city = (target.country, target.state, target.city) if target else (None, None, None)
        return self


class WireGuardPeerUpdate(BaseModel):
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
        # "" means clear and survives as such; a value is normalised. Whether a
        # state or city still has a country is only known once merged with the
        # stored peer, so the route checks that.
        if value is None or value == "":
            return value
        return normalize_level(info.field_name or "", value) or ""

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class WireGuardPeerStatus(BaseModel):
    """Live state of one device, as the instance carrying its session last published it."""

    online: bool = False
    last_handshake_at: datetime | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    endpoint: str | None = None
    # Connections relayed by address because no name could be recovered, and
    # encrypted-DNS connections closed, both since the carrying instance started.
    connections_by_address: int = 0
    encrypted_dns_blocked: int = 0


class WireGuardPeerResponse(BaseModel):
    id: str
    project_id: str
    name: str
    public_key: str
    has_private_key: bool
    has_preshared_key: bool
    address: str
    enabled: bool
    session_id: str | None
    country: str | None
    state: str | None
    city: str | None
    created_at: datetime
    updated_at: datetime
    status: WireGuardPeerStatus | None = None


class WireGuardPeerListResponse(BaseModel):
    total: int
    peers: list[WireGuardPeerResponse]
    # Whether configs can be issued: the admin has set the public endpoint.
    server_configured: bool
    server_public_key: str


class WireGuardPeerConfigResponse(BaseModel):
    """A device's ``wg-quick`` configuration file."""

    filename: str
    config: str
    # False when the device holds its own private key: the file carries a
    # placeholder the operator fills in.
    complete: bool
    server_configured: bool
