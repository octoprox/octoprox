# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Outbound request policy for vendor API calls made on behalf of descriptors.

A descriptor is admin-authored data that tells Octoprox to send credential
material to a URL. That is the shape of a credential-exfiltration or SSRF
attack, so every call passes through :class:`EgressGuard` first:

* HTTPS only (plain HTTP can be enabled for development).
* The hostname is resolved and every address is checked against private,
  loopback, link-local, multicast and reserved ranges — cloud metadata
  endpoints, Redis and the Octoprox API itself are unreachable.
* The connection is pinned to the vetted address (SNI and Host keep the
  original hostname) so a DNS answer cannot change between check and connect.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class EgressDeniedError(PermissionError):
    """The request violates the egress policy."""


@dataclass(frozen=True)
class EgressPolicy:
    """Knobs for :class:`EgressGuard`; defaults are the safe production values."""

    allow_http: bool = False
    allow_private: bool = False
    pin_dns: bool = True


@dataclass(frozen=True)
class PinnedTarget:
    """A vetted request destination."""

    url: httpx.URL
    """URL to actually connect to (host swapped for the vetted IP when pinning)."""
    hostname: str
    """Original hostname, sent as Host/SNI."""
    address: str


def is_public_address(address: str) -> bool:
    """True when ``address`` is a globally routable unicast IP."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None
            and not is_public_address(str(ip.ipv4_mapped)))
    )


class EgressGuard:
    """Validates and pins outbound destinations according to an :class:`EgressPolicy`."""

    def __init__(self, policy: EgressPolicy | None = None) -> None:
        self._policy = policy or EgressPolicy()

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    def check_static(self, url: str) -> str:
        """Scheme and literal-IP checks that need no network; returns the hostname."""
        parts = urlsplit(url)
        if parts.scheme == "http" and not self._policy.allow_http:
            raise EgressDeniedError(f"plain http is not allowed: {url}")
        if parts.scheme not in ("http", "https"):
            raise EgressDeniedError(f"unsupported scheme in {url}")
        hostname = parts.hostname
        if not hostname:
            raise EgressDeniedError(f"no host in {url}")
        if hostname == "localhost" and not self._policy.allow_private:
            raise EgressDeniedError("localhost is not allowed")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return hostname
        if not self._policy.allow_private and not is_public_address(hostname):
            raise EgressDeniedError(f"address {hostname} is not publicly routable")
        return hostname

    async def resolve(self, url: str) -> PinnedTarget:
        """Resolve ``url``'s host, vet every address and return a pinned target."""
        hostname = self.check_static(url)
        parsed = httpx.URL(url)
        if self._policy.allow_private and not self._policy.pin_dns:
            # Development/test mode: nothing to vet, so skip the lookup entirely.
            return PinnedTarget(url=parsed, hostname=hostname, address="")
        addresses = await self._lookup(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses:
            raise EgressDeniedError(f"could not resolve {hostname}")
        if not self._policy.allow_private:
            bad = [a for a in addresses if not is_public_address(a)]
            if bad:
                raise EgressDeniedError(f"{hostname} resolves to a non-public address ({bad[0]})")
        address = addresses[0]
        if not self._policy.pin_dns:
            return PinnedTarget(url=parsed, hostname=hostname, address=address)
        pinned_host = f"[{address}]" if ":" in address else address
        return PinnedTarget(url=parsed.copy_with(host=pinned_host), hostname=hostname, address=address)

    @staticmethod
    async def _lookup(hostname: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(hostname)
            return [hostname]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise EgressDeniedError(f"DNS lookup failed for {hostname}: {exc}") from exc
        seen: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in seen:
                seen.append(address)
        return seen
