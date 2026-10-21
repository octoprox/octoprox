# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Writes of definitions and runtime status, for the api's routes and the worker's jobs alike, and the api's read model.

Each write is one transaction announced after its commit; the Redis steps after it wait at most the request deadline.
"""
