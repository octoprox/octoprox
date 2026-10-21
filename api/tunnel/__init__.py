# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The data plane behind every tunnel: devices without proxy support become clients of the pool.

Whatever the protocol, ``dns`` answers a device's names with synthetic
addresses, ``system`` redirects its TCP connections to a local listener,
``sniff`` and ``peers`` recover the name and the device, and ``transparent``
relays as for a ``CONNECT``; ``dataplane`` ties them together on a gateway,
where a protocol's endpoint attaches its interface.
"""
