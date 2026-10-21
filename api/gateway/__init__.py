# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Components only a gateway runs: what it counts per request, and how it hands that to Redis.

The listeners (api.core.proxy_server, api.tunnel), routing (api.routing) and
the composition that wires them together (api.roles.gateway) live elsewhere.
"""
