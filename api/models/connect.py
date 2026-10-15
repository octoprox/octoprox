# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Connect page shows: how a client reaches a project's proxy, and what the proxy makes of a username."""

from pydantic import BaseModel


class ConnectEndpoint(BaseModel):
    """Where clients reach the proxy listener."""

    # None when no public host is configured: the client uses the host it reached the web UI at.
    host: str | None
    port: int


class ConnectLocation(BaseModel):
    """The place a username asks for, normalised as the proxy reads it."""

    country: str | None
    state: str | None
    city: str | None


class ConnectRoute(BaseModel):
    """One connector a request with the username would reach now."""

    connector_id: str
    connector_name: str
    # A dynamic-sessions gateway: the place is rendered into a fresh vendor session per request.
    dynamic: bool
    # Nothing serves the country yet: this "All countries" pool provisions a slot group for it on the first request.
    on_demand: bool
    # Routable proxies of the connector for the username (the gateway row for a dynamic connector).
    proxies: int
    # Expected share of requests by routing weight, among the connectors listed.
    share: float


class ConnectPlace(BaseModel):
    """A state or city some of the project's fixed exits are known to be in."""

    country: str
    state: str | None
    city: str | None
    proxies: int


class ConnectInfo(BaseModel):
    """The proxy endpoint, the username for the place and session asked for, and where it would route."""

    endpoint: ConnectEndpoint
    # The username to send: canonical when it parses, as composed when it does not.
    username: str
    sessid: str | None
    location: ConnectLocation | None
    # Why the proxy would refuse the username (its 400 message), or None.
    error: str | None
    routes: list[ConnectRoute]
    # Countries the project serves now: declared by a connector or the known country of a healthy exit.
    countries: list[str]
    # A country outside ``countries`` is served too (a dynamic gateway or pool with no country list).
    any_country: bool
    places: list[ConnectPlace]
