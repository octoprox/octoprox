# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The data plane behind every tunnel: devices without proxy support become clients of the pool.

A device connects to this instance through some tunnel protocol (WireGuard
today; ``api/wireguard`` owns that side) and inside the tunnel does ordinary
networking: it resolves names against the tunnel's DNS and opens TCP
connections to the addresses it gets. What happens next does not depend on
the protocol. Octoprox answers every name with a synthetic address from a
reserved range (``dns``), redirects every TCP connection on the tunnel
interface to a local listener with nftables (``system``), recovers the
destination name from the synthetic address or from the stream itself
(``sniff``), identifies the device by the tunnel address it sent from
(``peers``) and then does exactly what it does for a ``CONNECT`` request:
picks an upstream proxy for the device's project and relays bytes
(``transparent``). ``dataplane`` ties those together for one process; a
tunnel protocol brings its interface up and attaches it there.
"""
