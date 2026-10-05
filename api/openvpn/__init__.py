# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""OpenVPN endpoint: the second tunnel protocol devices can join a project's pool through.

The OpenVPN side of a tunnel device: the install's private CA and server
certificate, one client certificate per device (``pki``), the text handed to
the ``openvpn`` daemon and to devices (``config``), the daemon itself
(``daemon``) and its management interface, through which every connecting
device is admitted or refused and given its address (``management``), the
peer directory (``peers``) and ``runtime``, which ties them together for one
process and attaches the daemon's interface to the shared tunnel data plane
(:mod:`api.tunnel`). From there on nothing depends on the protocol: fake-IP
DNS, the nftables redirect, name recovery and the relay through the
project's upstream are the same as for WireGuard.
"""
