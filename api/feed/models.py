# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the routing feed carries: the definitions every gateway routes on, cut into sections.

There is a global section, one per connector and a runtime one; the controller publishes them (:mod:`api.feed.publisher`),
and every gateway fetches those that changed (:mod:`api.feed.follower`).
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from api.geo.models import (
    GeoDatabaseFormat,
    GeoDatabaseKind,
    GeoDatabaseSource,
    GeoSettings,
    GeoVendor,
)
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.location import LocationTarget
from api.models.openvpn import OpenVpnProtocol
from api.models.project import Project
from api.models.proxy import Proxy

# The layout of the sections below. A follower applies only a head of its own
# schema, so a controller and gateways of different releases never read each
# other's sections wrongly during an upgrade. Bump it with any change an older
# reader would misread.
FEED_SCHEMA = 1
# The name of the section holding everything that belongs to no one connector.
GLOBAL_SECTION = "global"
# The name of the section holding the quarantines and traffic blocks, which
# move with traffic, without the definitions.
RUNTIME_SECTION = "runtime"
# Connector sections are named "connector:<id>".
_CONNECTOR_SECTION_PREFIX = "connector:"


def connector_section(connector_id: str) -> str:
    """The name of a connector's section."""
    return _CONNECTOR_SECTION_PREFIX + connector_id


class FeedHead(BaseModel):
    """The current compile of the feed: the key a follower reads when the feed stream wakes it.

    ``digest`` changes exactly when a section is added, dropped or changed; stored as plain JSON, as it carries no
    secret.
    """

    # FEED_SCHEMA of the controller that wrote it.
    schema_version: int
    # HMAC over the sorted manifest (api.feed.codec.FeedCodec.manifest_digest).
    digest: str
    # How far the change stream had been read when the compile that last
    # changed the feed began (an entry ID: every change announced up to it
    # is in the sections; None for a compile that followed no stream), and
    # when that compile was made: a compile that changes nothing writes
    # nothing. Informational; the digest decides.
    changes: str | None
    compiled_at: datetime
    # How many sections the manifest lists.
    sections: int


# --- global pieces ---------------------------------------------------------------------------


class GeoDatabaseFeed(BaseModel):
    """One IP database as gateways learn of it: what they need to fetch, verify and open the file.

    The feed's narrow copy of :class:`api.geo.models.GeoDatabaseRecord`, without the schedule, the download
    credentials or the last refresh's outcome.
    """

    id: str
    name: str
    vendor: GeoVendor
    kind: GeoDatabaseKind
    format: GeoDatabaseFormat
    source: GeoDatabaseSource
    enabled: bool
    # Lower is consulted first.
    priority: int
    # A file an operator manages on the host (source=path); None for stored files.
    path: str | None = None
    # The stored file's checksum; a cached copy that matches it is used as is.
    sha256: str
    created_at: datetime


class WireGuardServerFeed(BaseModel):
    """What a gateway runs the WireGuard interface with; any change is applied to the interface."""

    private_key: str
    # The port devices are told; a gateway listens on it unless its own config names another.
    endpoint_port: int
    # The tunnel addresses: the gateway takes the first host.
    subnet: str

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet)

    @property
    def gateway(self) -> str:
        """The interface's own tunnel address: the first host of the subnet."""
        return str(next(self.network.hosts()))


class OpenVpnServerFeed(BaseModel):
    """What a gateway starts the OpenVPN daemon with, never the CA's key; any change restarts the daemon."""

    ca_cert: str
    server_cert: str
    server_key: str
    tls_crypt_key: str
    # The port devices are told; a gateway listens on it unless its own config names another.
    endpoint_port: int
    protocol: OpenVpnProtocol
    # The tunnel addresses: the daemon takes the first host.
    subnet: str
    keepalive_interval: int
    keepalive_timeout: int

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet)

    @property
    def gateway(self) -> str:
        """The daemon's own tunnel address: the first host of the subnet."""
        return str(next(self.network.hosts()))


class TunnelPeerFeed(BaseModel):
    """A device a gateway admits through a tunnel, and how its traffic is routed: what :class:`api.tunnel.peers.TunnelPeer` needs.

    Never a private key, and never the last sighting, which moves with every handshake.
    """

    id: str
    project_id: str
    name: str
    address: str
    enabled: bool
    session_id: str | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    created_at: datetime

    @property
    def location(self) -> LocationTarget | None:
        """The exit this device's traffic must use, as a proxy client's ``-cc-`` suffixes would say it."""
        if not self.country:
            return None
        return LocationTarget(country=self.country, state=self.state, city=self.city)


class WireGuardPeerFeed(TunnelPeerFeed):
    """A WireGuard device: its public key and preshared key are what the interface admits it by."""

    public_key: str
    preshared_key: str | None = None


class OpenVpnPeerFeed(TunnelPeerFeed):
    """An OpenVPN device; its certificate and key stay in Postgres."""

    # The serial of the device's current certificate: a connection that
    # presents another (an old certificate after a rotation) is refused.
    serial: str


class WireGuardFeed(BaseModel):
    """The WireGuard endpoint as gateways run it: the server's settings and every device."""

    server: WireGuardServerFeed
    peers: list[WireGuardPeerFeed] = Field(default_factory=list)


class OpenVpnFeed(BaseModel):
    """The OpenVPN endpoint as gateways run it: the server's identity and every device."""

    server: OpenVpnServerFeed
    peers: list[OpenVpnPeerFeed] = Field(default_factory=list)


# --- sections --------------------------------------------------------------------------------


class GlobalSection(BaseModel):
    """Everything a gateway routes on that belongs to no one connector."""

    # Definitions only: the description is left out.
    projects: list[Project] = Field(default_factory=list)
    # The enabled custom provider descriptors as stored (ProviderRecord.spec).
    descriptors: list[dict[str, Any]] = Field(default_factory=list)
    # The settings the install runs with: the stored row, or the controller's
    # config defaults while no admin has saved one (geo_settings_stored False).
    geo_settings: GeoSettings = Field(default_factory=GeoSettings)
    geo_settings_stored: bool = False
    geo_databases: list[GeoDatabaseFeed] = Field(default_factory=list)
    # None while the protocol has no settings row yet.
    wireguard: WireGuardFeed | None = None
    openvpn: OpenVpnFeed | None = None


class ConnectorSection(BaseModel):
    """One connector and its proxies, as gateways route on them."""

    connector: Connector
    # The connector's credential when its provider renders requests from it
    # (descriptor providers: dynamic sessions, on-demand countries, request
    # country checks), without the secrets rendering never reads
    # (api.feed.sections.routing_credential). None for cloud and static
    # connectors, whose rows arrive with their credentials resolved.
    credential: Credential | None = None
    # The countries the connector declares for -cc- routing (upper-case ISO
    # codes, empty when it declares none), as its provider reads them: a
    # descriptor names its own country field, which Connector.countries
    # cannot know. Routing reads only this, never the connector's config.
    declared_countries: list[str] = Field(default_factory=list)
    # Credentials resolved (api.feed.sections.resolve_credentials), status
    # from Postgres, display-only metadata dropped, ordered by (created_at, id).
    proxies: list[Proxy] = Field(default_factory=list)


class RuntimeSection(BaseModel):
    """The runtime state the feed carries for the whole install: the quarantines gateways trip, the traffic blocks workers set.

    Only IDs the compile holds, each map by ID, so the same state gives the same JSON; times are naive UTC.
    """

    # Proxy id -> when its rate-limit quarantine ends.
    quarantined: dict[str, datetime] = Field(default_factory=dict)
    # Connector id -> when the traffic limit that keeps it out of routing ends.
    traffic_blocked_until: dict[str, datetime] = Field(default_factory=dict)


# Any kind of section, as the compiler builds them and a follower decodes them.
type Section = GlobalSection | ConnectorSection | RuntimeSection


def section_model(name: str) -> type[GlobalSection] | type[ConnectorSection] | type[RuntimeSection]:
    """The model a section of this name holds; ValueError for a name the feed never uses."""
    if name == GLOBAL_SECTION:
        return GlobalSection
    if name == RUNTIME_SECTION:
        return RuntimeSection
    if name.startswith(_CONNECTOR_SECTION_PREFIX):
        return ConnectorSection
    raise ValueError(f"Not a feed section: {name}")
