# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Work the worker role takes from Redis: a job stream per kind of job, and the sightings stream.

Any role appends jobs and gateways append sightings; every worker reads them through the workers' group.
"""
