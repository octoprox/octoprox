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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from api.core import utc_now
from api.geo.models import IpLocation
from api.geo.readers import is_ip
from api.geo.store import GeoDatabaseStore

if TYPE_CHECKING:
    from starlette.datastructures import Headers


class EchoResponse(BaseModel):
    """What ``/echo`` answers. ``ip`` is always present; the rest needs a loaded database."""

    ip: str
    country: str | None = None
    region: str | None = None
    state_code: str | None = None
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


def forwarded_for_of(headers: Headers) -> str | None:
    """Every ``X-Forwarded-For`` line of a request joined in order, or None when there is none.

    A balancer may add a line of its own rather than append to the client's,
    as HAProxy does, so the first line alone can be the client's forgery.
    """
    return ", ".join(headers.getlist("x-forwarded-for")) or None


def client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    """A client's address: the peer, or the address a trusted balancer saw connect.

    ``X-Forwarded-For`` is appended to by every hop, so its rightmost entry
    not belonging to a trusted network is the address that connected to the
    balancer: for the echo the proxy's exit, for a sign-in the person's
    browser. Anything to the left was written by the client itself and must
    not be trusted.
    """
    if not peer or not is_ip(peer):
        return None
    if not forwarded_for or not trusted:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in trusted):
        return peer
    for hop in reversed(forwarded_for.split(",")):
        candidate = hop.strip()
        if not is_ip(candidate):
            continue
        if any(ipaddress.ip_address(candidate) in network for network in trusted):
            continue
        return candidate
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
        state_code=fields.get("state_code"),
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
