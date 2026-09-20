# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The catalogue of named background loops.

One table, read by everything that needs to talk about a background worker:
``ProxyManager`` when it spawns the loops, the loops themselves when they take
a lease or count a cycle, and the admin system view when it renders both lists.
Keeping it here rather than in :mod:`api.core.system_stats` means a worker
module can name itself without importing the collectors.

Worker and lease names are identifiers, not labels: they are what
``asyncio.Task.get_name()`` returns, what the Redis lease key embeds
(``lease:<name>``), and what the ``/system/stats`` payload exposes. Changing
one is an API change - ``WorkerName`` exists so that change happens in one
place rather than in a string scattered across five modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class WorkerName(StrEnum):
    """Names passed to ``ProxyManager._spawn``, and so the task names."""

    HEALTH_CHECKER = "health_checker"
    METRICS_FLUSHER = "metrics_flusher"
    METRICS_COMPACTOR = "metrics_compactor"
    AUTO_SCALER = "auto_scaler"
    PROVIDER_SYNCER = "provider_syncer"
    SYSTEM_SNAPSHOTTER = "system_snapshotter"
    HEARTBEAT = "heartbeat"
    FULL_RELOAD = "full_reload"
    METRIC_DELTA_PUBLISHER = "metric_delta_publisher"
    METRIC_DELTA_SUBSCRIBER = "metric_delta_subscriber"
    CROSS_INSTANCE_SUBSCRIBER = "cross_instance_subscriber"


class LeaseName(StrEnum):
    """Lease names, i.e. the ``<name>`` in the ``lease:<name>`` Redis key.

    A global singleton uses the bare name. A per-resource lease appends the
    resource id via :func:`resource_lease`, so different instances can own
    different connectors at the same time.
    """

    METRICS_FLUSHER = "metrics_flusher"
    METRICS_COMPACTOR = "metrics_compactor"
    SYSTEM_SNAPSHOTTER = "system_snapshotter"
    AUTOSCALER = "autoscaler"
    PROVIDER_SYNC = "provider_sync"


def resource_lease(name: LeaseName, resource_id: str) -> str:
    """Build the lease name for one resource, e.g. ``autoscaler:<connector id>``."""
    return f"{name}:{resource_id}"


@dataclass(frozen=True)
class WorkerInfo:
    """What one background loop does, and how it behaves in a cluster.

    ``lease`` is None for a loop every instance runs. Otherwise it is the
    lease this loop elects on - bare for a global singleton, or the prefix of
    ``<lease>:<resource id>`` for one elected per resource.
    """

    description: str
    lease: LeaseName | None = None
    lease_label: str = ""

    @property
    def scope(self) -> Literal["instance", "singleton"]:
        return "singleton" if self.lease else "instance"


WORKERS: dict[WorkerName, WorkerInfo] = {
    WorkerName.HEALTH_CHECKER: WorkerInfo(
        "Probes the proxies this instance owns and publishes their status",
    ),
    WorkerName.METRICS_FLUSHER: WorkerInfo(
        "Turns the Redis request counters into permanent Postgres history rows, "
        "then resets them - the Redis-to-Postgres half of the metrics pipeline",
        lease=LeaseName.METRICS_FLUSHER,
        lease_label="Metrics flush to Postgres",
    ),
    WorkerName.METRICS_COMPACTOR: WorkerInfo(
        "Rolls up old metric rows into coarser granularities and applies retention",
        lease=LeaseName.METRICS_COMPACTOR,
        lease_label="Metrics compaction",
    ),
    WorkerName.AUTO_SCALER: WorkerInfo(
        "Scales cloud connectors and rotates proxies to match demand",
        lease=LeaseName.AUTOSCALER,
        lease_label="Auto-scaling",
    ),
    WorkerName.PROVIDER_SYNCER: WorkerInfo(
        "Discovery and IP refresh: reconciles each connector against its provider",
        lease=LeaseName.PROVIDER_SYNC,
        lease_label="Provider sync",
    ),
    WorkerName.SYSTEM_SNAPSHOTTER: WorkerInfo(
        "Records one install-wide gauge reading per interval for the trend "
        "charts, and prunes readings past the retention window",
        lease=LeaseName.SYSTEM_SNAPSHOTTER,
        lease_label="System snapshots",
    ),
    WorkerName.HEARTBEAT: WorkerInfo(
        "Advertises this instance so peers can shard work and find lease holders",
    ),
    WorkerName.FULL_RELOAD: WorkerInfo(
        "Safety-net reload from Postgres in case an invalidation event was dropped",
    ),
    WorkerName.METRIC_DELTA_PUBLISHER: WorkerInfo(
        "Moves this instance's buffered per-request counters into Redis every few "
        "seconds and announces them to peers - the in-process-to-Redis half of "
        "the metrics pipeline, which keeps the request path off Redis",
    ),
    WorkerName.METRIC_DELTA_SUBSCRIBER: WorkerInfo(
        "Folds peer instances' metric deltas into local counters",
    ),
    WorkerName.CROSS_INSTANCE_SUBSCRIBER: WorkerInfo(
        "Applies entity changes published by other instances",
    ),
}

def worker_info(name: str) -> WorkerInfo | None:
    """Catalogue entry for a task name, or None if that loop is not catalogued.

    Takes a plain ``str`` because the caller's name comes from
    ``asyncio.Task.get_name()``, which knows nothing about :class:`WorkerName`.
    """
    try:
        return WORKERS[WorkerName(name)]
    except ValueError:
        return None


# Both directions of the lease <-> worker mapping, derived rather than
# repeated: the admin view uses them to line up "which instance runs this job"
# against "how is that job going on this instance".
LEASE_WORKERS: dict[str, WorkerName] = {
    info.lease: name for name, info in WORKERS.items() if info.lease
}
LEASE_KINDS: dict[str, str] = {
    info.lease: info.lease_label for info in WORKERS.values() if info.lease
}
