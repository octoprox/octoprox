# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Autoscaling: the ``scale`` job sizes, rotates and retires the cloud instances of one connector a run.

Runs of one connector overlap, since a create outlasts the period they come at, so every step is safe beside another
run of the same connector.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, cast

import structlog

from api.core.demand_tracker import DEMAND_WINDOW_SECONDS, DemandLevel
from api.core.logging import error_text
from api.core.roles import JobKind
from api.core.stats import MetricDelta
from api.db.redis import AUTOSCALER_STATE_KEY, PROXY_WINDOWS, SCALE_CREATING_KEY
from api.db.repository import (
    ConnectorRepository,
    MetricsRepository,
    ProxyRepository,
)
from api.feed.state import RuntimeStateReader
from api.jobs.job import Job
from api.models.proxy import ProxyStatus
from api.providers.cloud import CloudProvider

if TYPE_CHECKING:
    from redis.commands.core import AsyncScript

    from api.core.demand_tracker import DemandTracker
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.jobs.job import JobBatch
    from api.models.connector import CloudConnectorConfig, Connector
    from api.models.credential import Credential
    from api.models.proxy import Proxy
    from api.providers.cloud import CloudInstance
    from api.providers.registry import ProviderRegistry
    from api.services.connectors import ConnectorService
    from api.services.proxies import ProxyService

logger = structlog.get_logger()

# Seconds after a scaling action before the next one: long enough for new
# instances to come up and take traffic, so demand reflects them.
SCALING_COOLDOWN_SECONDS = 180
# Seconds after which an entry of the creating hash is stale: its run died, was
# stopped in the middle of its create, or lost Redis, before clearing it. A run
# with a listing then counts the entry as none, so its instance is terminated
# unless it has a row, and clears it. Creates are done well within it.
CREATE_TIMEOUT_SECONDS = 600
# Seconds a successful listing of the connector's instances stands: a run
# lists them again once it is older, even with no creating entry and no pending
# deletion to ask for it, so an instance with neither a row nor an entry (Redis
# lost its creator's entry) is terminated within about this.
LISTING_INTERVAL_SECONDS = 3600
# How a failed listing's error starts, as recorded on the connector: a successful
# listing clears the connector's error only when it starts so, and leaves a
# create's or a termination's to count its backoff up.
LISTING_FAILED = "Failed to list the connector's instances"
LISTING_ERROR_PREFIX = f"{LISTING_FAILED}: "
# Checks in a row that must find a draining instance's counters unchanged
# before it is terminated: one can miss the counts a metrics flush holds for
# a few seconds between taking them and committing them.
QUIET_CHECKS = 2
# Statuses of instances that serve, or may again, and so are rotated when due.
_ROTATABLE = frozenset({ProxyStatus.HEALTHY, ProxyStatus.DEGRADED, ProxyStatus.UNKNOWN})

# Fields of the autoscaler hash: when the state was started (epoch seconds),
# the last demand level, the last scaling action and the last successful
# listing (epoch seconds), and one field per instance for its rotation time
# (epoch seconds) and, while it drains, its counters at the last check (``Drain``).
_SINCE = "since"
_LEVEL = "level"
_LAST_ACTION = "last_action"
_LISTED_AT = "listed_at"
_ROTATE = "rotate:"
_DRAIN = "drain:"
# The purposes of an entry of the creating hash: an instance the scaling step
# adds, or the replacement of a proxy being rotated ("rotate:<proxy id>").
_SCALE_PURPOSE = "scale"
_ROTATION_PURPOSE = "rotate:"

# Lua: KEYS[1] the creating hash; ARGV[1] a purpose, ARGV[2] the oldest time (epoch seconds) a fresh entry has, ARGV[3]
# an entry, then the names to write it under. Writes the entry under every name and answers 1, unless the hash holds a
# fresh entry of that purpose under another name: then it writes nothing and answers 0. So of the runs that write
# entries of one purpose at once, one does, and a run that writes its own entries again keeps them fresh.
_HOLD_SCRIPT = """
local mine = {}
for i = 4, #ARGV do
  mine[ARGV[i]] = true
end
local fields = redis.call('HGETALL', KEYS[1])
for i = 1, #fields, 2 do
  if not mine[fields[i]] then
    local ok, entry = pcall(cjson.decode, fields[i + 1])
    local at = ok and type(entry) == 'table' and tonumber(entry.at)
    if at and entry.purpose == ARGV[1] and at >= tonumber(ARGV[2]) then
      return 0
    end
  end
end
for i = 4, #ARGV do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[3])
end
return 1
"""


class Drain(NamedTuple):
    """A draining instance's traffic counters at its last check, and how many checks in a row found them unchanged since."""

    sent: int
    received: int
    quiet: int = 0

    @classmethod
    def parse(cls, raw: str) -> Drain | None:
        """The drain a hash field holds, ``"<sent> <received> <quiet>"``; None when it is unreadable."""
        try:
            sent, received, quiet = (int(part) for part in raw.split(" "))
        except ValueError:
            return None
        return cls(sent, received, quiet)

    def to_field(self) -> str:
        """The hash field's value."""
        return f"{self.sent} {self.received} {self.quiet}"


@dataclass
class AutoscalerState:
    """What the job remembers about one connector between runs; the hash ``AUTOSCALER_STATE_KEY``."""

    # When this state was started (epoch seconds): a lost hash starts again
    # now, and nothing is scaled in for a demand window from then.
    since: float
    # The demand level of the last run, for hysteresis.
    level: DemandLevel | None = None
    # The last scaling action (epoch seconds), for the cooldown.
    last_action: float | None = None
    # The last successful listing of the connector's instances (epoch seconds),
    # written only once a listing succeeds; None until one has.
    listed_at: float | None = None
    # Each instance's rotation time (epoch seconds).
    rotations: dict[str, float] = field(default_factory=dict)
    # Each draining instance's counters at the last check.
    drains: dict[str, Drain] = field(default_factory=dict)

    @classmethod
    def from_fields(cls, fields: Mapping[str, str], now: float) -> AutoscalerState:
        """The state a hash holds; a new one started at ``now`` when it holds none. Unreadable fields are skipped."""
        since = cls._number(fields.get(_SINCE))
        if since is None:
            return cls(since=now)
        state = cls(
            since=since,
            last_action=cls._number(fields.get(_LAST_ACTION)),
            listed_at=cls._number(fields.get(_LISTED_AT)),
        )
        if fields.get(_LEVEL) in {level.value for level in DemandLevel}:
            state.level = DemandLevel(fields[_LEVEL])
        for name, value in fields.items():
            if name.startswith(_ROTATE) and (due := cls._number(value)) is not None:
                state.rotations[name.removeprefix(_ROTATE)] = due
            elif name.startswith(_DRAIN) and (drain := Drain.parse(value)) is not None:
                state.drains[name.removeprefix(_DRAIN)] = drain
        return state

    def to_fields(self) -> dict[str, str]:
        """The hash's fields."""
        fields = {_SINCE: repr(self.since)}
        if self.level is not None:
            fields[_LEVEL] = self.level.value
        if self.last_action is not None:
            fields[_LAST_ACTION] = repr(self.last_action)
        if self.listed_at is not None:
            fields[_LISTED_AT] = repr(self.listed_at)
        for proxy_id, due in self.rotations.items():
            fields[f"{_ROTATE}{proxy_id}"] = repr(due)
        for proxy_id, drain in self.drains.items():
            fields[f"{_DRAIN}{proxy_id}"] = drain.to_field()
        return fields

    def retain(self, proxy_ids: set[str]) -> None:
        """Forget instances that are gone."""
        self.rotations = {proxy_id: due for proxy_id, due in self.rotations.items() if proxy_id in proxy_ids}
        self.drains = {proxy_id: drain for proxy_id, drain in self.drains.items() if proxy_id in proxy_ids}

    def listed_within(self, now: float) -> bool:
        """Whether a listing succeeded at most ``LISTING_INTERVAL_SECONDS`` before ``now``; false with none recorded."""
        return self.listed_at is not None and now - self.listed_at <= LISTING_INTERVAL_SECONDS

    @staticmethod
    def _number(raw: str | None) -> float | None:
        """A field's number; None when it is absent or unreadable."""
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None


@dataclass(frozen=True)
class Creation:
    """An entry of the creating hash: an instance a run is creating, what for, and since when (epoch seconds)."""

    # "scale", or "rotate:<proxy id>" for the replacement of that proxy.
    purpose: str
    at: float

    @classmethod
    def scale(cls, at: float) -> Creation:
        """An instance the scaling step adds."""
        return cls(_SCALE_PURPOSE, at)

    @classmethod
    def rotation(cls, proxy_id: str, at: float) -> Creation:
        """The replacement of a proxy being rotated."""
        return cls(f"{_ROTATION_PURPOSE}{proxy_id}", at)

    @classmethod
    def parse(cls, raw: str) -> Creation:
        """The entry a field holds; an unreadable one reads as a stale scale entry, which the next run with a listing clears."""
        try:
            data = json.loads(raw)
            return cls(str(data["purpose"]), float(data["at"]))
        except (ValueError, KeyError, TypeError):
            return cls.scale(0.0)

    def to_json(self) -> str:
        """The field's value."""
        return json.dumps({"purpose": self.purpose, "at": self.at})

    @property
    def scales(self) -> bool:
        """Whether the entry is an instance the scaling step adds."""
        return self.purpose == _SCALE_PURPOSE

    @property
    def replaces(self) -> str | None:
        """The proxy a rotation's entry replaces; None for any other entry."""
        return self.purpose.removeprefix(_ROTATION_PURPOSE) if self.purpose.startswith(_ROTATION_PURPOSE) else None

    def stale(self, now: float) -> bool:
        """Whether the entry is older than ``CREATE_TIMEOUT_SECONDS``, so a run counts it as none and clears it."""
        return now - self.at > CREATE_TIMEOUT_SECONDS


@dataclass
class _Run:
    """One run over one connector: what it read, and what it changed so far."""

    connector: Connector
    cloud: CloudProvider
    # The instances the cloud listed at the start; None when the run did not list them, or its listing failed.
    listed: list[CloudInstance] | None
    # The creating hash as read after the listing, less the entries the run cleared.
    creating: dict[str, Creation]
    # The connector's instances, as stored once the hash was read and as this run moved them since.
    proxies: list[Proxy]
    state: AutoscalerState
    now: float

    @property
    def config(self) -> CloudConnectorConfig:
        """The connector's cloud config, as its cloud API holds it."""
        return self.cloud.config

    def active(self) -> list[Proxy]:
        """The run's instances not on their way out."""
        return [proxy for proxy in self.proxies if not proxy.status.retiring]

    def holds(self, proxy_id: str) -> bool:
        """Whether the run has the instance among its rows."""
        return any(proxy.id == proxy_id for proxy in self.proxies)


class AutoscaleJob(Job):
    """Evaluates one cloud connector a run: terminates the instances a listing shows with no row, then scales, rotates, drains and terminates the rest.

    Args:
        cloud: Builds a connector's cloud API; None for a type that is no cloud (tests pass fakes).
    """

    kind = JobKind.SCALE
    # One connector a run. A run may wait minutes on a cloud API (an instance
    # coming up), so a worker holds a few at once.
    concurrency = 4

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        registry: ProviderRegistry,
        proxies: ProxyService,
        connectors: ConnectorService,
        demand: DemandTracker,
        *,
        cloud: Callable[[Connector, Credential], CloudProvider | None] = CloudProvider.of,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._runtime = RuntimeStateReader(redis_client)
        self._registry = registry
        self._proxies = proxies
        self._connectors = connectors
        self._demand = demand
        self._cloud = cloud
        self._hold_script: AsyncScript | None = None

    async def run(self, batch: JobBatch) -> None:
        """Evaluate each connector the batch names."""
        for connector_id in batch.keys:
            await self.scale(connector_id)

    async def scale(self, connector_id: str) -> None:
        """Evaluate one connector; one that is gone has what the job keeps about it deleted."""
        async with self._session_factory() as session:
            found = await ConnectorRepository(session).get_with_credential(connector_id)
        if found is None:
            await self._connectors.forget(connector_id)
            return
        connector, credential = found
        if not self._registry.is_cloud(credential.type):
            return
        cloud = self._cloud(connector, credential)
        if cloud is None:
            return
        run = await self._read(connector, cloud)
        await self._terminate_unrecorded(run)
        if connector.enabled:
            await self._check_scaling(run)
            await self._check_instances(run)
        elif await self._retire_all(run):
            return
        await self._save_state(run)

    async def _read(self, connector: Connector, cloud: CloudProvider) -> _Run:
        """Read what a run starts from, in this order: the instances the cloud lists, when the run lists them, the creating hash, then the rows.

        A failed listing is recorded on the connector as a cloud error, which backs it off; a successful one clears a
        failed listing's error.
        """
        now = time.time()
        state = await self._load_state(connector.id, now)
        listed: list[CloudInstance] | None = None
        if await self._lists(connector, state, now):
            try:
                listed = await cloud.tagged_instances()
            except Exception as exc:
                logger.warning(LISTING_FAILED, connector_id=connector.id, error=error_text(exc))
                connector = await self._connectors.record_error(connector.id, LISTING_ERROR_PREFIX + error_text(exc)) or connector
            else:
                state.listed_at = now
                if connector.last_error is not None and connector.last_error.startswith(LISTING_ERROR_PREFIX):
                    connector = await self._connectors.record_error(connector.id, None) or connector
        creating = await self._creations(connector.id)
        proxies = await self._rows(connector.id)
        return _Run(connector, cloud, listed, creating, proxies, state, now)

    async def _lists(self, connector: Connector, state: AutoscalerState, now: float) -> bool:
        """Whether the run lists the connector's instances: never while it backs off after a cloud error, otherwise while it is pending deletion or its creating hash holds any entry, and once no listing succeeded within ``LISTING_INTERVAL_SECONDS``."""
        if connector.in_error_backoff:
            return False
        if connector.pending_deletion or not state.listed_within(now):
            return True
        return bool(await self._redis.client.exists(SCALE_CREATING_KEY.format(connector_id=connector.id)))

    async def _rows(self, connector_id: str) -> list[Proxy]:
        """The connector's instances as stored."""
        async with self._session_factory() as session:
            return await ProxyRepository(session).get_by_connector(connector_id)

    # --- state -------------------------------------------------------------------------

    async def _load_state(self, connector_id: str, now: float) -> AutoscalerState:
        key = AUTOSCALER_STATE_KEY.format(connector_id=connector_id)
        fields = await cast("Awaitable[dict[str, str]]", self._redis.client.hgetall(key))
        return AutoscalerState.from_fields(fields, now)

    async def _save_state(self, run: _Run) -> None:
        """Replace the hash with this run's state, the instances that are gone forgotten."""
        run.state.retain({proxy.id for proxy in run.proxies})
        key = AUTOSCALER_STATE_KEY.format(connector_id=run.connector.id)
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.delete(key)
        # redis-py types the mapping's keys as a union, and a mapping is invariant in its key type.
        pipe.hset(key, mapping=run.state.to_fields())  # type: ignore[arg-type]
        await pipe.execute()

    # --- the creating hash -------------------------------------------------------------

    async def _creations(self, connector_id: str) -> dict[str, Creation]:
        """The connector's creating hash, by creation name."""
        key = SCALE_CREATING_KEY.format(connector_id=connector_id)
        fields = await cast("Awaitable[dict[str, str]]", self._redis.client.hgetall(key))
        return {name: Creation.parse(raw) for name, raw in fields.items()}

    async def _hold(self, run: _Run, creation: Creation, names: Sequence[str]) -> bool:
        """Write ``creation`` under each of ``names`` in one step, unless the hash holds a fresh entry of its purpose under another name; whether it did."""
        if self._hold_script is None:
            self._hold_script = self._redis.client.register_script(_HOLD_SCRIPT)
        key = SCALE_CREATING_KEY.format(connector_id=run.connector.id)
        fresh_since = creation.at - CREATE_TIMEOUT_SECONDS
        held = await self._hold_script(keys=[key], args=[creation.purpose, fresh_since, creation.to_json(), *names])
        return int(held) == 1

    async def _wanted(self, run: _Run) -> bool:
        """Whether the connector, read again once the run's entries are written, still wants instances: not gone, disabled or pending deletion."""
        async with self._session_factory() as session:
            current = await ConnectorRepository(session).get_by_id(run.connector.id)
        if current is not None and current.enabled and not current.pending_deletion:
            return True
        logger.info("Creating no instance: the connector was disabled or removed", connector_id=run.connector.id)
        return False

    async def _end(self, run: _Run, *names: str) -> None:
        """Clear entries once their instances have their rows, or have none to have."""
        key = SCALE_CREATING_KEY.format(connector_id=run.connector.id)
        await self._redis.client.hdel(key, *names)
        for name in names:
            run.creating.pop(name, None)

    # --- instances with no row ---------------------------------------------------------

    async def _terminate_unrecorded(self, run: _Run) -> None:
        """Terminate each listed instance that has neither a row nor an entry newer than ``CREATE_TIMEOUT_SECONDS``, then clear the stale entries.

        A run with no listing, or a failed one, clears none, and a failed termination keeps its instance's entry for the next run.
        """
        if run.listed is None:
            return
        failed: set[str] = set()
        for instance in run.listed:
            creation = run.creating.get(instance.name)
            if run.holds(instance.proxy_id) or (creation is not None and not creation.stale(run.now)):
                continue
            logger.warning("Terminating a cloud instance with no row", connector_id=run.connector.id, name=instance.name)
            try:
                await run.cloud.terminate_instance(instance.proxy_id)
            except Exception as exc:
                logger.warning(
                    "Failed to terminate a cloud instance with no row",
                    connector_id=run.connector.id,
                    name=instance.name,
                    error=error_text(exc),
                )
                failed.add(instance.name)
        for name, creation in list(run.creating.items()):
            if creation.stale(run.now) and name not in failed:
                logger.info("Clearing a stale creation", connector_id=run.connector.id, name=name)
                await self._end(run, name)

    # --- disabled connectors -----------------------------------------------------------

    async def _retire_all(self, run: _Run) -> bool:
        """Drain, terminate and delete every instance of a disabled connector; True once the connector is gone.

        A connector pending deletion goes once a run finds no rows, no entries and no instance listed.
        """
        if run.proxies:
            counters = await self._counters([p for p in run.proxies if p.status == ProxyStatus.DRAINING])
            for proxy in list(run.proxies):
                if proxy.status == ProxyStatus.DRAINING:
                    await self._check_drained(run, proxy, counters)
                elif proxy.status == ProxyStatus.TERMINATING:
                    # Terminating is a cloud call: not while backing off.
                    if not run.connector.in_error_backoff:
                        await self._terminate(run, proxy)
                else:
                    logger.info("Draining proxy of a disabled connector", connector_id=run.connector.id, proxy_id=proxy.id)
                    await self._drain(run, proxy.id)
            return False
        # An instance listed (this run terminated it, if it had no row) or a
        # listing that failed holds the deletion to a later run.
        if not run.connector.pending_deletion or run.creating or run.listed is None or run.listed:
            return False
        logger.info("Removing connector pending deletion: its instances are gone", connector_id=run.connector.id)
        await self._connectors.delete(run.connector.id)
        return True

    # --- scaling -----------------------------------------------------------------------

    async def _check_scaling(self, run: _Run) -> None:
        """Move the pool a step towards what the project's demand asks for, unless something holds it."""
        connector, config, state = run.connector, run.config, run.state
        if connector.in_error_backoff:
            logger.debug("Skipping scaling during error backoff", connector_id=connector.id)
            return
        # A blocked connector takes no requests, and the rejections it causes
        # count as demand: scaling it up would pay for instances that carry
        # nothing. Rotation and draining go on.
        if connector.id in await self._runtime.blocked_now():
            logger.debug("Skipping scaling: connector over its traffic limit", connector_id=connector.id)
            return
        if state.last_action is not None and run.now - state.last_action < SCALING_COOLDOWN_SECONDS:
            logger.debug("Skipping scaling during cooldown", connector_id=connector.id)
            return
        if any(creation.scales for creation in run.creating.values()):
            logger.debug("Skipping scaling: an instance is still being created", connector_id=connector.id)
            return
        active = run.active()
        if any(proxy.status == ProxyStatus.INITIALIZING for proxy in active):
            logger.debug("Skipping scaling: an instance is still initializing", connector_id=connector.id)
            return
        healthy = sum(1 for proxy in active if proxy.status == ProxyStatus.HEALTHY)
        level = await self._demand.get_demand_level(connector.project_id, healthy, state.level)
        state.level = level
        target = config.target_count(level)
        max_up, max_down = config.max_steps
        if target > len(active):
            # A stale burst left in the long window adds nothing; the minimum
            # is reached whatever the traffic.
            if len(active) >= config.min_proxies and not await self._demand.has_recent_activity(connector.project_id):
                logger.debug("Skipping scale-up: no recent activity", connector_id=connector.id, level=level.value)
                return
            await self._scale_up(run, min(target - len(active), max_up))
        elif target < len(active):
            if run.now - state.since < DEMAND_WINDOW_SECONDS:
                logger.debug("Holding scale-down until a whole demand window is observed", connector_id=connector.id)
                return
            await self._scale_down(run, min(len(active) - target, max_down))

    async def _scale_up(self, run: _Run, count: int) -> None:
        """Create up to ``count`` instances under entries all written before the first create, unless another run holds a fresh scale entry or the number of active instances changed since the run read them; a cloud error (recorded), the connector disabled or another run's step meanwhile ends the step.

        However the step ends it clears the entries of the creates it never started, while a create a stop cuts short keeps its own, as a crash does.
        """
        creation = Creation.scale(time.time())
        names = [run.cloud.creation_name(creation.at) for _ in range(count)]
        if not await self._hold(run, creation, names):
            logger.debug("Skipping scaling: an instance is still being created", connector_id=run.connector.id)
            return
        created = 0
        unstarted = list(names)
        try:
            if not await self._count_unchanged(run):
                return
            logger.info("Scaling up", connector_id=run.connector.id, count=count)
            while unstarted and await self._wanted(run):
                name = unstarted.pop(0)
                # The entry is cleared once the create returns or fails, not in a finally: one cut short may have
                # launched its instance, which a run's listing terminates once the entry is stale, unless it has a row.
                try:
                    proxy = await self._create(run, name)
                except Exception as exc:
                    logger.error(
                        "Failed to create proxy instance", connector_id=run.connector.id, error=error_text(exc)
                    )
                    await self._end(run, name)
                    await self._record_error(run, exc)
                    break
                await self._end(run, name)
                if proxy is not None:
                    created += 1
                # The entries still to create are written again, so a long step's stay fresh.
                if unstarted and not await self._hold(run, Creation.scale(time.time()), unstarted):
                    logger.warning("Ending a scaling step: another run is creating instances", connector_id=run.connector.id)
                    break
        finally:
            if unstarted:
                await self._end(run, *unstarted)
        if created:
            await self._clear_error(run)
            run.state.last_action = run.now

    async def _count_unchanged(self, run: _Run) -> bool:
        """Whether the connector's active instances, read again once the step's entries are written, are as many as the run read: another run's whole step saves its rows before it clears its entries."""
        read = len(run.active())
        stored = sum(1 for proxy in await self._rows(run.connector.id) if not proxy.status.retiring)
        if stored == read:
            return True
        logger.debug(
            "Skipping scaling: the connector's instances changed since the run read them",
            connector_id=run.connector.id,
            read=read,
            stored=stored,
        )
        return False

    async def _scale_down(self, run: _Run, count: int) -> None:
        """Drain ``count`` instances: unhealthy ones first, then the oldest."""
        logger.info("Scaling down", connector_id=run.connector.id, count=count)
        retiring = sorted(run.active(), key=lambda p: (p.status == ProxyStatus.HEALTHY, p.created_at))[:count]
        for proxy in retiring:
            await self._drain(run, proxy.id)
        if retiring:
            run.state.last_action = run.now

    # --- each instance -----------------------------------------------------------------

    async def _check_instances(self, run: _Run) -> None:
        """Rotate the instances that are due, terminate the drained ones and delete the terminated ones."""
        counters = await self._counters([p for p in run.proxies if p.status == ProxyStatus.DRAINING])
        for proxy in list(run.proxies):
            if proxy.id not in run.state.rotations:
                self._schedule_rotation(run, proxy)
            if proxy.status == ProxyStatus.DRAINING:
                await self._check_drained(run, proxy, counters)
            elif proxy.status == ProxyStatus.TERMINATING:
                # Terminating is a cloud call: not while backing off.
                if not run.connector.in_error_backoff:
                    await self._terminate(run, proxy)
            elif (
                proxy.status in _ROTATABLE
                and run.now >= run.state.rotations[proxy.id]
                and not run.connector.in_error_backoff
            ):
                await self._rotate(run, proxy)

    async def _rotate(self, run: _Run, proxy: Proxy) -> None:
        """Replace an instance whose time has come, unless another run is replacing it or has, or the connector was disabled meanwhile: its replacement first, then it drains, replaced or not.

        A rotation a stop cuts short keeps its entry, as a crash does.
        """
        if any(creation.replaces == proxy.id for creation in run.creating.values()):
            return
        creation = Creation.rotation(proxy.id, time.time())
        name = run.cloud.creation_name(creation.at)
        if not await self._hold(run, creation, [name]):
            return
        # Not a finally: a cancelled create may have launched the replacement, which keeps the entry until it is stale.
        try:
            if await self._rotatable(proxy) and await self._wanted(run):
                await self._replace(run, proxy, name)
        except Exception:
            await self._end(run, name)
            raise
        await self._end(run, name)

    async def _rotatable(self, proxy: Proxy) -> bool:
        """Whether a due instance's row, read again once the run's entry is written, has not moved on: a run replacing it drains the row before it clears its entry."""
        async with self._session_factory() as session:
            current = await ProxyRepository(session).get_by_id(proxy.id)
        return current is not None and current.status in _ROTATABLE

    async def _replace(self, run: _Run, proxy: Proxy, name: str) -> None:
        """Create the instance ``name`` in place of a due one, then drain the due one, replaced or not."""
        logger.info("Starting proxy rotation", proxy_id=proxy.id, connector_id=run.connector.id)
        try:
            replacement = await self._create(run, name)
            if replacement is not None:
                logger.info("Created replacement proxy", old_proxy_id=proxy.id, new_proxy_id=replacement.id)
                await self._clear_error(run)
        except Exception as exc:
            logger.error(
                "Failed to create replacement proxy",
                proxy_id=proxy.id,
                connector_id=run.connector.id,
                error=error_text(exc),
            )
            await self._record_error(run, exc)
        finally:
            await self._drain(run, proxy.id)

    async def _check_drained(self, run: _Run, proxy: Proxy, counters: Mapping[str, tuple[int, int]]) -> None:
        """Terminate a draining instance once its counters stayed the same over ``QUIET_CHECKS`` checks in a row; else note this check."""
        current = counters[proxy.id]
        drain = run.state.drains.get(proxy.id)
        if drain is None or (drain.sent, drain.received) != current:
            # The first look, or traffic still flowing.
            run.state.drains[proxy.id] = Drain(*current)
            return
        if drain.quiet + 1 < QUIET_CHECKS:
            run.state.drains[proxy.id] = drain._replace(quiet=drain.quiet + 1)
            return
        logger.info(
            "Draining complete: no traffic change detected, marking as terminating",
            proxy_id=proxy.id,
            bytes_sent=current[0],
            bytes_received=current[1],
        )
        await self._proxies.terminate(proxy.id)
        proxy.status = ProxyStatus.TERMINATING
        run.state.drains.pop(proxy.id, None)

    async def _terminate(self, run: _Run, proxy: Proxy) -> None:
        """Delete a terminating instance in its cloud, then its row; an error is recorded on the connector and the row kept."""
        try:
            await run.cloud.terminate_instance(proxy.id)
        except Exception as exc:
            logger.error("Error terminating proxy instance", proxy_id=proxy.id, error=error_text(exc))
            await self._record_error(run, exc)
            return
        await self._proxies.delete(proxy.id)
        run.proxies.remove(proxy)
        logger.info("Terminated proxy instance", proxy_id=proxy.id)

    # --- shared steps ------------------------------------------------------------------

    async def _create(self, run: _Run, name: str) -> Proxy | None:
        """Create the instance ``name`` and save its row, its rotation time drawn; None when its entry went meanwhile.

        An entry gone (Redis lost it, or a run found it stale) means a run may have terminated the instance, or may yet,
        so it gets no row.
        """
        proxy = await run.cloud.create_instance(name)
        key = SCALE_CREATING_KEY.format(connector_id=run.connector.id)
        if not await self._redis.client.hexists(key, name):
            logger.warning(
                "Saving no row for a created instance whose entry is gone: a scale run terminates it",
                connector_id=run.connector.id,
                name=name,
            )
            return None
        await self._proxies.add(proxy)
        logger.info("Saved proxy instance", proxy_id=proxy.id, connector_id=run.connector.id)
        self._schedule_rotation(run, proxy)
        run.proxies.append(proxy)
        return proxy

    async def _drain(self, run: _Run, proxy_id: str) -> None:
        """Stop routing new requests to an instance; it is terminated once its traffic stops."""
        await self._proxies.drain(proxy_id)
        for proxy in run.proxies:
            if proxy.id == proxy_id and not proxy.status.retiring:
                proxy.status = ProxyStatus.DRAINING

    @staticmethod
    def _schedule_rotation(run: _Run, proxy: Proxy) -> None:
        """Draw when an instance is replaced: a random time between the connector's rotation periods from now."""
        minutes = random.randint(run.config.min_rotation_period_minutes, run.config.max_rotation_period_minutes)
        run.state.rotations[proxy.id] = run.now + minutes * 60

    async def _counters(self, proxies: Sequence[Proxy]) -> dict[str, tuple[int, int]]:
        """Each instance's bytes sent and received so far: its flushed totals plus its live Redis window.

        Counts a metrics flush has taken out of the window and not yet committed are in neither for those few seconds.
        """
        if not proxies:
            return {}
        ids = [proxy.id for proxy in proxies]
        async with self._session_factory() as session:
            totals = await MetricsRepository(session).get_totals(PROXY_WINDOWS, ids)
        windows = await self._runtime.windows(PROXY_WINDOWS, ids)
        counters: dict[str, tuple[int, int]] = {}
        for proxy_id in ids:
            delta = MetricDelta.summed(totals.get(proxy_id), windows.get(proxy_id))
            counters[proxy_id] = (delta.bytes_sent, delta.bytes_received)
        return counters

    async def _record_error(self, run: _Run, error: Exception) -> None:
        """Record a cloud error on the connector by its text, or its type's name when it has none; the connector then backs off, and this run goes on with the updated connector."""
        run.connector = await self._connectors.record_error(run.connector.id, error_text(error)) or run.connector

    async def _clear_error(self, run: _Run) -> None:
        """Clear the connector's cloud error after a success; nothing is written when it has none."""
        if run.connector.last_error is None and run.connector.consecutive_errors == 0:
            return
        run.connector = await self._connectors.record_error(run.connector.id, None) or run.connector
