# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing feed: how definitions in Postgres reach gateways that have no Postgres, as sections the controller publishes to Redis.

Each gateway fetches only the sections that changed, so a replica's cost does not grow with the replicas.
"""
