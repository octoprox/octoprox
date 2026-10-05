# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Text the WireGuard endpoint hands to ``wg`` and to devices.

Pure functions, so what reaches the kernel and what a device is told can be
tested without either.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from api.models.wireguard import WireGuardPeer, WireGuardServerSettings

# A device's private key Octoprox never saw: the operator pastes it in.
PRIVATE_KEY_PLACEHOLDER = "<paste the device's private key here>"
# wg-quick derives the interface name from the file name: 15 characters of [a-zA-Z0-9_=+.-].
_FILENAME_LIMIT = 15
_UNSAFE = re.compile(r"[^a-zA-Z0-9_=+.-]+")


def render_server_conf(private_key: str, listen_port: int, peers: Iterable[WireGuardPeer]) -> str:
    """The ``wg setconf`` / ``wg syncconf`` file: our key, our port, every enabled peer."""
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"ListenPort = {listen_port}", ""]
    for peer in peers:
        if not peer.enabled:
            continue
        lines += ["[Peer]", f"PublicKey = {peer.public_key}"]
        if peer.preshared_key:
            lines.append(f"PresharedKey = {peer.preshared_key}")
        lines += [f"AllowedIPs = {peer.address}/32", ""]
    return "\n".join(lines)


def render_client_conf(peer: WireGuardPeer, server: WireGuardServerSettings) -> str:
    """A device's ``wg-quick`` file.

    Everything the device sends goes into the tunnel (``AllowedIPs`` covers
    both address families, so nothing leaks around it even though the tunnel
    itself carries only IPv4), and it resolves names against the gateway,
    which answers from the fake-IP range. ``Endpoint`` is the install-wide
    public endpoint the admin set.
    """
    lines = [
        "[Interface]",
        f"PrivateKey = {peer.private_key or PRIVATE_KEY_PLACEHOLDER}",
        f"Address = {peer.address}/32",
        f"DNS = {server.gateway}",
    ]
    if server.client_mtu:
        lines.append(f"MTU = {server.client_mtu}")
    lines += ["", "[Peer]", f"PublicKey = {server.public_key}"]
    if peer.preshared_key:
        lines.append(f"PresharedKey = {peer.preshared_key}")
    lines += [
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"Endpoint = {_endpoint(server.endpoint_host)}:{server.endpoint_port}",
    ]
    if server.persistent_keepalive:
        lines.append(f"PersistentKeepalive = {server.persistent_keepalive}")
    return "\n".join(lines) + "\n"


def _endpoint(host: str) -> str:
    # An IPv6 literal needs brackets in host:port form.
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def client_conf_filename(peer_name: str) -> str:
    """``<name>.conf`` with a stem wg-quick accepts as an interface name."""
    stem = _UNSAFE.sub("-", peer_name.strip()).strip("-.")[:_FILENAME_LIMIT].rstrip("-.")
    return f"{stem or 'octoprox'}.conf"
