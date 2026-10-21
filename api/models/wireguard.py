# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard models: the install's server identity and the devices that may connect.

A device joins a project as a peer: what it sends is routed as if it had signed in with the project's proxy credentials.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from api.core import utc_now
from api.models.location import LocationTarget, normalize_level
from api.models.tunnel import (
    TunnelPeerMetrics,
    TunnelStatus,
    clean_optional,
    naive_utc,
    validate_endpoint_host,
    validate_tunnel_subnet,
)
from api.wireguard import keys

DEFAULT_SUBNET = "10.66.0.0/16"
DEFAULT_ENDPOINT_PORT = 51820


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


def validate_subnet(value: str) -> str:
    """An IPv4 network with room for the gateway and at least one peer, in canonical form."""
    return validate_tunnel_subnet(value, DEFAULT_SUBNET)


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
    # The tunnel on every gateway carrying it, and the devices.
    status: TunnelStatus


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
    # When the device last handshaked and from where, as persisted by the
    # instance that carried the session (see WireGuardPeerModel). Live
    # readings are merged over these in the API.
    last_handshake_at: datetime | None = None
    last_endpoint: str | None = None
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
        return clean_optional(value) if isinstance(value, str) else value

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str | None) -> str | None:
        value = clean_optional(value)
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


class WireGuardPeerReading(BaseModel):
    """One device as a gateway's interface reports it, published every status tick under its public key.

    The one format of the gateway that writes it, the api that shows it and the workers' last-seen job.
    """

    # When the device last completed a handshake with this gateway, unix
    # seconds; 0 when it never has since the interface came up.
    latest_handshake: int = 0
    # The interface's counters for the device since it came up.
    rx_bytes: int = 0
    tx_bytes: int = 0
    # Where the device's packets come from (host:port), as wg last saw it.
    endpoint: str | None = None

    @property
    def handshake_at(self) -> datetime | None:
        """The last handshake as naive UTC; None when there has been none."""
        return naive_utc(self.latest_handshake) if self.latest_handshake else None

    @classmethod
    def latest(cls, published: Iterable[Mapping[str, str]]) -> dict[str, WireGuardPeerReading]:
        """Each device's reading from the gateway it handshaked with last, by public key; an entry that does not parse is skipped.

        Behind a UDP load balancer several gateways publish each device: its traffic lands where it last handshaked.
        """
        latest: dict[str, WireGuardPeerReading] = {}
        for carrier in published:
            for public_key, payload in carrier.items():
                try:
                    reading = cls.model_validate_json(payload)
                except ValueError:
                    continue
                current = latest.get(public_key)
                if current is None or reading.latest_handshake > current.latest_handshake:
                    latest[public_key] = reading
        return latest


class WireGuardPeerStatus(BaseModel):
    """Where a device stands: its last handshake, and the tunnel counters while it is carried.

    Its traffic is in its metrics (``TunnelPeerMetrics``).
    """

    online: bool = False
    # From the carrying instance while one publishes, else as persisted the last time one did.
    last_handshake_at: datetime | None = None
    endpoint: str | None = None
    # True when a carrying instance published this reading within the last
    # half minute; False when it is the persisted last sighting.
    live: bool = False
    # The current carrier's interface counters (wire bytes, handshakes and DNS
    # included); zero when nothing carries the device.
    rx_bytes: int = 0
    tx_bytes: int = 0


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
    status: WireGuardPeerStatus
    # The device's totals, from the same pipeline as the project's and the
    # connectors' counters: history plus the current window, cluster-wide.
    metrics: TunnelPeerMetrics


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
