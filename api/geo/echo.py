# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The echo endpoint: tell the caller which IP it arrived from, and what we know about it.

Requested *through* a proxy, the caller is the proxy's exit, so the answer is
the exit IP. That is all health checks, discovery, exit lookups and preflight
need from a third party today (httpbin.org, lumtest.com); serving it
ourselves removes the dependency and lets the response carry the attribution
from the same databases the rest of Octoprox uses.

The endpoint is unauthenticated by nature and reflects nothing from the
request beyond the peer address and a nonce the caller may pass to defeat
caches. Behind a load balancer the peer is the balancer, so the client IP is
read from ``X-Forwarded-For`` only when the peer is in ``trusted_proxies``.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from pydantic import BaseModel, Field

from api.core import utc_now
from api.geo.models import IpLocation
from api.geo.readers import is_ip
from api.geo.store import GeoDatabaseStore


class EchoResponse(BaseModel):
    """What ``/echo`` answers. ``ip`` is always present; the rest needs a loaded database."""

    ip: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    organization: str | None = None
    is_anonymous: bool | None = None
    is_hosting: bool | None = None
    databases: list[str] = Field(default_factory=list)
    nonce: str | None = None
    timestamp: str


def parse_trusted(cidrs: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        text = raw.strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return networks


def client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """The address to echo: the peer, or the first forwarded hop when the peer is a trusted balancer."""
    if not peer or not is_ip(peer):
        return None
    if forwarded_for and trusted:
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if any(peer_address in network for network in trusted):
            first = forwarded_for.split(",", 1)[0].strip()
            if is_ip(first):
                return first
    return peer


def build_echo(
    ip: str,
    store: GeoDatabaseStore | None,
    nonce: str | None = None,
) -> EchoResponse:
    """Attribute ``ip`` with whatever databases are loaded and shape the response."""
    merged: IpLocation | None = None
    origins: list[str] = []
    if store is not None:
        for candidate in store.lookup(ip):
            if candidate.location is None:
                continue
            origins.append(candidate.origin)
            merged = candidate.location if merged is None else merged.merged_with(candidate.location)
    fields: dict[str, Any] = merged.model_dump() if merged else {}
    return EchoResponse(
        ip=ip,
        country=fields.get("country"),
        region=fields.get("region"),
        city=fields.get("city"),
        latitude=fields.get("latitude"),
        longitude=fields.get("longitude"),
        asn=fields.get("asn"),
        organization=fields.get("organization"),
        is_anonymous=fields.get("is_anonymous"),
        is_hosting=fields.get("is_hosting"),
        databases=origins,
        nonce=nonce[:64] if nonce else None,
        timestamp=utc_now().isoformat() + "Z",
    )
