# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard endpoint: devices without proxy support become clients of the pool.

A device connects to this instance over WireGuard. Inside the tunnel it does
ordinary networking: it resolves names against the tunnel's DNS and opens TCP
connections to the addresses it gets. Octoprox answers every name with a
synthetic address from a reserved range (``dns``), redirects every TCP
connection on the tunnel interface to a local listener with nftables
(``system``), recovers the destination name from the synthetic address or
from the stream itself (``sniff``), and then does exactly what it does for a
``CONNECT`` request: picks an upstream proxy for the peer's project and
relays bytes (``transparent``). ``runtime`` ties it together for one process.
"""
