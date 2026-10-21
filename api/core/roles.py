# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What a process is for, and what work a worker process can take.

A leaf module, so the settings, the worker catalogue and the job streams can
all name these without an import cycle.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """The part of Octoprox one process runs, chosen by ``OCTOPROX_ROLE``: every role runs the same image and command.

    ``all`` composes every role's runtime in one process, as a single
    instance, development and most tests run.
    """

    ALL = "all"
    API = "api"
    GATEWAY = "gateway"
    CONTROLLER = "controller"
    WORKER = "worker"
    ECHO = "echo"


class JobKind(StrEnum):
    """A kind of work workers run, one job stream each (``o6:jobs:<kind>:stream``).

    The controller publishes the periodic kinds and any role asks for a run
    now; a worker takes the kinds ``OCTOPROX_WORKER_JOBS`` lists.
    """

    # Probe one proxy through its upstream and update its health.
    HEALTH = "health"
    # Reconcile one provider connector with its vendor (discovery, refresh).
    SYNC = "sync"
    # Evaluate one cloud connector: scale, rotate, drain, terminate.
    SCALE = "scale"
    # Persist an on-demand country slot group a gateway asked for.
    PROVISION = "provision"
    # Re-judge the attribution of a project's proxies, or of all of them.
    REATTRIBUTE = "reattribute"
    # Look up the exit of a newly added static proxy through it.
    ENRICH = "enrich"
    # Decide whether one traffic-limited connector is blocked, and say so when the decision changes.
    TRAFFIC = "traffic"
    # Move the request counters' windows from Redis into Postgres history and totals.
    METRICS_FLUSH = "metrics_flush"
    # Roll old metric rows up into coarser ones, and apply the retentions.
    COMPACTION = "compaction"
    # Record one install-wide gauge reading for the trend charts.
    SNAPSHOT = "snapshot"
    # Download the IP database updates that are due.
    GEO_UPDATE = "geo_update"
    # Record when each tunnel device was last seen, from the statuses gateways publish.
    TUNNEL_LAST_SEEN = "tunnel_last_seen"
    # Prune the Redis indexes, the work streams' stopped consumers and the ended
    # quarantines, and ask for a traffic decision of every connector blocked or
    # waiting for one.
    INDEX_HYGIENE = "index_hygiene"
