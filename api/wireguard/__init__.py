# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard endpoint: devices without proxy support become clients of the pool.

The WireGuard side of a tunnel device: the install's key pair, the peers
(``peers``, ``keys``), the interface on the host (``system``), the text
handed to ``wg`` and to devices (``config``), and ``runtime``, which ties
them together for one process and attaches the interface to the shared
tunnel data plane (:mod:`api.tunnel`), where everything that does not depend
on the protocol happens: fake-IP DNS, the nftables redirect, name recovery
and the relay through the project's upstream.
"""
