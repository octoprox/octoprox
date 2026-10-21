# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The catalogue of named background loops: what each does, which roles run it, and under which lease.

Loops are spawned, counted and shown on the System page by their
``WorkerName``, so a name is an identifier and changing one is an API change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from api.core.roles import JobKind, Role


class WorkerName(StrEnum):
    """The name of a background loop: its task name, its ``job_stats`` key and its System page row."""

    # Every role.
    HEARTBEAT = "heartbeat"
    LOOP_LAG_MONITOR = "loop_lag_monitor"
    # The controller.
    FEED_BUILDER = "feed_builder"
    JOB_PRODUCER_HEALTH = "job_producer_health"
    JOB_PRODUCER_SYNC = "job_producer_sync"
    JOB_PRODUCER_SCALE = "job_producer_scale"
    JOB_PRODUCER_METRICS_FLUSH = "job_producer_metrics_flush"
    JOB_PRODUCER_COMPACTION = "job_producer_compaction"
    JOB_PRODUCER_SNAPSHOT = "job_producer_snapshot"
    JOB_PRODUCER_GEO_UPDATE = "job_producer_geo_update"
    JOB_PRODUCER_TUNNEL_LAST_SEEN = "job_producer_tunnel_last_seen"
    JOB_PRODUCER_INDEX_HYGIENE = "job_producer_index_hygiene"
    # Gateways.
    FEED_FOLLOWER = "feed_follower"
    REQUEST_METERING = "request_metering"
    FAILURE_LOG_PUBLISHER = "failure_log_publisher"
    GEO_OBSERVATION_PUBLISHER = "geo_observation_publisher"
    SIGHTING_PUBLISHER = "sighting_publisher"
    WIREGUARD_STATUS_PUBLISHER = "wireguard_status_publisher"
    OPENVPN_STATUS_PUBLISHER = "openvpn_status_publisher"
    OPENVPN_SUPERVISOR = "openvpn_supervisor"
    TUNNEL_RECONCILER = "tunnel_reconciler"
    GEO_DATABASE_SYNC = "geo_database_sync"
    GEO_DATABASE_RETRY = "geo_database_retry"
    # The api and workers.
    DEFINITIONS_REFRESHER = "definitions_refresher"
    # Workers.
    JOB_RUNNER_HEALTH = "job_runner_health"
    JOB_RUNNER_SYNC = "job_runner_sync"
    JOB_RUNNER_SCALE = "job_runner_scale"
    JOB_RUNNER_PROVISION = "job_runner_provision"
    JOB_RUNNER_REATTRIBUTE = "job_runner_reattribute"
    JOB_RUNNER_ENRICH = "job_runner_enrich"
    JOB_RUNNER_TRAFFIC = "job_runner_traffic"
    JOB_RUNNER_METRICS_FLUSH = "job_runner_metrics_flush"
    JOB_RUNNER_COMPACTION = "job_runner_compaction"
    JOB_RUNNER_SNAPSHOT = "job_runner_snapshot"
    JOB_RUNNER_GEO_UPDATE = "job_runner_geo_update"
    JOB_RUNNER_TUNNEL_LAST_SEEN = "job_runner_tunnel_last_seen"
    JOB_RUNNER_INDEX_HYGIENE = "job_runner_index_hygiene"
    SIGHTING_INGEST = "sighting_ingest"
    OBSERVATION_INGEST = "observation_ingest"


def job_runner_name(kind: JobKind) -> WorkerName:
    """The loop that runs one kind of job on a worker, e.g. ``job_runner_health``."""
    return WorkerName(f"job_runner_{kind}")


def job_producer_name(kind: JobKind) -> WorkerName:
    """The controller loop that publishes a periodic kind of job, e.g. ``job_producer_health``; ValueError for another kind."""
    return WorkerName(f"job_producer_{kind}")


class LeaseName(StrEnum):
    """Lease names, i.e. the ``<name>`` in the ``o6:lease:<name>`` Redis key (``LEASE_KEY``)."""

    # Held by the active controller. Every loop of the controller role runs
    # in the one process that holds it; the other controllers stand by.
    CONTROLLER = "controller"
    # Held for a moment on first start while one process generates the MITM
    # CA on the shared CA volume (api.core.tls_cert_manager), so processes
    # starting together end up with one CA rather than one each.
    CA_BOOTSTRAP = "ca-bootstrap"


@dataclass(frozen=True)
class WorkerInfo:
    """What one background loop does, the roles whose processes run it, and the lease that picks one of them.

    ``lease`` is None for a loop every process of its roles runs, else the
    lease whose holder alone runs it (the controller lease).
    """

    description: str
    roles: frozenset[Role]
    lease: LeaseName | None = None

    @property
    def scope(self) -> Literal["instance", "singleton"]:
        return "singleton" if self.lease else "instance"

    def runs_in(self, role: Role) -> bool:
        """Whether a process of ``role`` runs this loop: the listed roles, and every loop in role ``all``."""
        return role == Role.ALL or role in self.roles


_CONTROLLER = frozenset({Role.CONTROLLER})
_WORKER = frozenset({Role.WORKER})

WORKERS: dict[WorkerName, WorkerInfo] = {
    # Every role.
    WorkerName.HEARTBEAT: WorkerInfo(
        "Writes this instance's role and a snapshot of its state to Redis every 5 seconds, for the "
        "System page and the per-gateway views, and checks as it goes that Redis and, in a role that "
        "uses it, Postgres answer, which is what the readiness probe reports; no work is divided up "
        "by which instances are alive",
        # Not echo, which keeps nothing in Redis.
        roles=frozenset({Role.API, Role.GATEWAY, Role.CONTROLLER, Role.WORKER}),
    ),
    WorkerName.LOOP_LAG_MONITOR: WorkerInfo(
        "Measures how late this instance's event loop runs a timer - the wait every "
        "await pays - and warns when it falls a second behind",
        roles=frozenset({Role.API, Role.GATEWAY, Role.CONTROLLER, Role.WORKER, Role.ECHO}),
    ),
    # The controller, in the process holding the controller lease: the feed
    # and the timers that publish the periodic jobs, nothing else.
    WorkerName.FEED_BUILDER: WorkerInfo(
        "Compiles the routing definitions in Postgres into the feed when the change stream announces "
        "a change, at most once per compile interval, folds in the quarantines and traffic blocks the "
        "runtime stream announces, and publishes the sections that changed for the gateways; every "
        "reconcile interval (an hour) it compiles and folds in everything, whatever the streams said",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_HEALTH: WorkerInfo(
        "Publishes a health check of each proxy once per health check interval, the proxies spread "
        "evenly over it, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_SYNC: WorkerInfo(
        "Publishes a sync of each enabled provider connector that syncs once per IP refresh "
        "interval, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_SCALE: WorkerInfo(
        "Publishes a scale check of each cloud connector every 30 seconds, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_METRICS_FLUSH: WorkerInfo(
        "Publishes a metrics flush once per metrics flush interval, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_COMPACTION: WorkerInfo(
        "Publishes a metrics compaction every hour, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_SNAPSHOT: WorkerInfo(
        "Publishes a system snapshot once per system metrics interval, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_GEO_UPDATE: WorkerInfo(
        "Publishes an IP database update every hour, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_TUNNEL_LAST_SEEN: WorkerInfo(
        "Publishes a tunnel device last-seen record every 10 seconds, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    WorkerName.JOB_PRODUCER_INDEX_HYGIENE: WorkerInfo(
        "Publishes an index hygiene pass every minute, for the workers to run",
        roles=_CONTROLLER,
        lease=LeaseName.CONTROLLER,
    ),
    # Gateways.
    WorkerName.FEED_FOLLOWER: WorkerInfo(
        "Wakes when the controller publishes a new feed head and applies the sections that changed "
        "to this gateway's routing snapshot, retrying an update that failed to apply and reading the "
        "head again every hour; with Redis or the controller gone it keeps routing on the last one",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.REQUEST_METERING: WorkerInfo(
        "Moves this gateway's per-request counters (proxies, projects, connectors, hosts, tunnel "
        "devices) and demand into Redis every 5 seconds, and asks the workers for a traffic decision "
        "on each connector with a limit whose bytes it carried; a window Redis did not confirm is "
        "sent again and counted once - the in-process-to-Redis half of the metrics pipeline, which "
        "keeps the request path off Redis",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.FAILURE_LOG_PUBLISHER: WorkerInfo(
        "Writes the failed requests this gateway saw in the last few seconds, folded and capped, to "
        "each project's failure log in Redis; a window Redis did not confirm is sent again and "
        "counted once",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.GEO_OBSERVATION_PUBLISHER: WorkerInfo(
        "Appends this instance's buffered IP observations and judgements to the observations stream "
        "every few seconds, sooner when the buffer is half full and nothing is owed; a part Redis did "
        "not confirm is sent again and lands once",
        # Gateways observe dynamic rows in preflight, the api in Detect, and
        # workers in every check and lookup they attribute.
        roles=frozenset({Role.GATEWAY, Role.API, Role.WORKER}),
    ),
    WorkerName.SIGHTING_PUBLISHER: WorkerInfo(
        "Appends this gateway's preflight sightings of pooled session slots to the sightings stream "
        "the workers attribute them from, every few seconds; a batch Redis did not confirm is sent "
        "again and lands once",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.WIREGUARD_STATUS_PUBLISHER: WorkerInfo(
        "Every 10 seconds publishes each WireGuard device's handshake and transfer counters from the "
        "tunnel interface this gateway carries, for the device views and the workers' last-seen "
        "records",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.OPENVPN_STATUS_PUBLISHER: WorkerInfo(
        "Every 10 seconds publishes the sessions the OpenVPN daemon on this gateway carries (per "
        "device: since when, from where, bytes), for the device views and the workers' last-seen "
        "records",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.OPENVPN_SUPERVISOR: WorkerInfo(
        "Watches the OpenVPN daemon this gateway runs and starts it again, with "
        "backoff, when it exits on its own",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.TUNNEL_RECONCILER: WorkerInfo(
        "Every 30 seconds compares the WireGuard peers and OpenVPN sessions this gateway "
        "carries with the devices in the feed and removes any that should not be there, "
        "which also covers a change that failed to apply",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.GEO_DATABASE_SYNC: WorkerInfo(
        "Brings this gateway's IP databases to the list the routing feed carries, fetching the files "
        "it lacks from the api; a list that arrives while one is synced waits, and a newer one "
        "replaces it",
        roles=frozenset({Role.GATEWAY}),
    ),
    WorkerName.GEO_DATABASE_RETRY: WorkerInfo(
        "While an IP database file this instance should have could not be had, retries it every 30 "
        "seconds until it has it, so a gateway that started before the api, or a configured file "
        "not on disk yet, loads without waiting for the next change to the databases",
        # Every role with IP databases: gateways fetch the feed's files from the api, the api and
        # workers copy them from Postgres or open the configured ones.
        roles=frozenset({Role.GATEWAY, Role.API, Role.WORKER}),
    ),
    # The api and workers.
    WorkerName.DEFINITIONS_REFRESHER: WorkerInfo(
        "Re-reads custom provider descriptors, IP attribution settings and the IP database list when "
        "the change stream says one of them changed, and every hour whatever it said, so this "
        "instance validates and judges with what was last saved",
        roles=frozenset({Role.API, Role.WORKER}),
    ),
    # Workers, sharing every work stream through one consumer group.
    WorkerName.JOB_RUNNER_HEALTH: WorkerInfo(
        "Runs the health checks its job stream holds: probes each proxy through its upstream, keeps "
        "its failure count and latency in Redis, and writes its status to Postgres when it changes",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_SYNC: WorkerInfo(
        "Runs the provider syncs its job stream holds: reconciles each provider connector's proxies "
        "with its vendor (discovery and IP refresh)",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_SCALE: WorkerInfo(
        "Runs the scale checks its job stream holds: scales each cloud connector to its demand, "
        "rotates its proxies, and drains and terminates the ones it retires",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_PROVISION: WorkerInfo(
        "Adds the country slot group a gateway asked for, unless the connector has one for that "
        "country by then",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_REATTRIBUTE: WorkerInfo(
        "Re-judges where proxies exit, a connector's, a project's or all of them, page by page, after "
        "a location, attribution setting or IP database change; asks read together run as one pass",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_ENRICH: WorkerInfo(
        "Looks up the exit of each newly added static proxy through it, then attributes it",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_TRAFFIC: WorkerInfo(
        "Decides whether each traffic-limited connector it is asked about is blocked, from its "
        "stored history and live counter against its limit, and when the decision changes writes "
        "the block or lifts it and tells the controller; asked after a metering flush that carried "
        "the connector's bytes, after a limit edit or a reset, and every minute while the connector "
        "is blocked or waiting for a decision",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_METRICS_FLUSH: WorkerInfo(
        "Moves the Redis request counters (proxies, projects, connectors, tunnel devices, hosts, "
        "failures) into Postgres history rows and running totals, each window once: it takes the "
        "windows out of Redis under its job's id and commits them with that id - the "
        "Redis-to-Postgres half of the metrics pipeline",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_COMPACTION: WorkerInfo(
        "Rolls old metric rows up into coarser granularities and deletes the ones past each "
        "project's retention, the raw IP observations and exit IPs past theirs, and the records of "
        "past metrics flushes",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_SNAPSHOT: WorkerInfo(
        "Records one install-wide gauge reading for the trend charts when one is due, and prunes "
        "readings past the retention window",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_GEO_UPDATE: WorkerInfo(
        "Downloads the scheduled IP database updates that are due from their vendors into Postgres, "
        "announces each new file on the change stream, asks for every proxy to be re-attributed "
        "when a file changed, and reclaims the space replaced files took",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_TUNNEL_LAST_SEEN: WorkerInfo(
        "Records on each tunnel device's row when it was last seen, from the statuses the gateways "
        "publish, so last sightings survive restarts without gateways writing to Postgres",
        roles=_WORKER,
    ),
    WorkerName.JOB_RUNNER_INDEX_HYGIENE: WorkerInfo(
        "Drops instances and tunnel carriers that stopped publishing from the Redis indexes, "
        "stopped workers from the work streams' consumer groups and ended quarantines from the "
        "quarantine set, and asks for a traffic decision of every connector blocked or waiting for one",
        roles=_WORKER,
    ),
    WorkerName.SIGHTING_INGEST: WorkerInfo(
        "Attributes the preflight sightings the gateways append, a batch at a time in the order they "
        "were seen, writes what changed to the proxy rows and rotates out session slots whose exit "
        "is refused; a batch that fails is delivered again, and given up after five deliveries",
        roles=_WORKER,
    ),
    WorkerName.OBSERVATION_INGEST: WorkerInfo(
        "Writes the IP observations and exit judgements every process publishes into Postgres "
        "history, the exit table and the per-connector location accuracy aggregates, a batch at a "
        "time, counting each hand-out once whichever worker ingests it",
        roles=_WORKER,
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
