# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard endpoint: devices without proxy support become clients of the pool.

The WireGuard side of a tunnel device: the install's and the devices' key
pairs (``keys``), the interface on the host (``system``) and the text handed
to ``wg`` and to devices (``config``), in two halves that never meet:

- ``service`` is the api's: settings and devices in Postgres, configs, and
  how the devices stand across the gateways;
- ``endpoint`` is a gateway's: it follows the routing feed, runs the
  interface and attaches it to the shared tunnel data plane
  (:mod:`api.tunnel`), where everything that does not depend on the
  protocol happens: fake-IP DNS, the nftables redirect, name recovery and
  the relay through the project's upstream.
"""
