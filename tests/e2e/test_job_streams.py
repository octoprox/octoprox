# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The job streams end to end: the leader's producers and the api's run-now asks in, the workers' groups out.

Each test runs whole processes against Postgres and Redis and watches what the jobs leave behind: a proxy's soft
health and status, a runner's counters, the change entries and the streams as the System view reads them.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from itertools import pairwise

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.consumers import MAX_DELIVERIES
from api.core.roles import JobKind, Role
from api.db.redis import JOB_STREAM_KEY, WORKER_GROUP, RedisClient
from api.db.streams import ChangeKind
from api.feed.state import RuntimeStateReader
from api.jobs.job import JobBatch
from api.jobs.provision import ProvisionJob
from api.jobs.queue import JobQueue
from api.models.proxy import ProxyStatus
from tests.e2e.fakes import FakeUpstream
from tests.e2e.harness import (
    FAILOVER_WITHIN_SECONDS,
    FAST_LOOPS,
    POLL_SECONDS,
    READY_WITHIN_SECONDS,
    REGROUPED_WITHIN_SECONDS,
    ROUTED_WITHIN_SECONDS,
    SLACK_SECONDS,
    ClaimTimings,
    Cluster,
    change_entries,
    change_entry,
    checked_after,
    create_pool,
    drained,
    eventually,
    groups_back,
    job_stream,
    soft_health,
    start_split,
    stored_status,
    throughout,
    until,
)

pytestmark = pytest.mark.e2e

# How long a proxy the admin adds may take to be checked, with the workers
# idle before: its run-now entry wakes a worker's blocked read at once.
CHECKED_WITHIN_SECONDS = 1.0
# The bound on a run-now check where its speed is not what a test is about.
RUN_NOW_WITHIN_SECONDS = 5.0
# The health period of the test of how checks are spread, its proxies, and
# how long it watches them: two periods and a second, so each proxy is
# checked twice or three times.
SPREAD_PERIOD_SECONDS = 5
SPREAD_PROXIES = 12
WATCHED_SECONDS = 2 * SPREAD_PERIOD_SECONDS + 1
# How far into the second after its own a periodic check may land: the
# producer publishes a key in its second, and the check runs at once, but the
# producer's cycle may fall at the very end of the second.
LATE_SECONDS = 1
# A held check's timeout: longer than any test, so the worker holding it holds it until the vendor answers.
HUNG_CHECK_TIMEOUT_SECONDS = 60
# How much earlier than its bound a timing may come, for the event loop's own lateness.
SCHEDULING_SLACK_SECONDS = 0.1
# What the job that always raises is asked for: a country group of a connector.
REFUSED_GROUP = "e2e-connector:US"
# How long no periodic job must run while no controller leads: three health periods.
PAUSE_SECONDS = 3 * FAST_LOOPS["health_check_interval"]
# How long periodic jobs may take to come back once a controller leads again:
# a compile, then at most a period. The rest is room for a busy machine.
RESUMED_WITHIN_SECONDS = FAST_LOOPS["health_check_interval"] + SLACK_SECONDS
# Compaction's period in the failover test: the controller's constant, shortened.
COMPACTION_PERIOD_SECONDS = 4
# How much later than a period after the first the second compaction may run: the new leader's producer turns at
# another fraction of the second than the old one's, and a wait polls.
KEPT_PERIOD_SLACK_SECONDS = 2.0


def epoch_second(at: datetime) -> int:
    """The whole epoch second of a naive UTC time."""
    return int(at.replace(tzinfo=UTC).timestamp())


async def check_times(redis_client: RedisClient, proxy_ids: list[str], *, seconds: float) -> dict[str, list[datetime]]:
    """Each proxy's checks over the next ``seconds``: when its soft health was written, read every ``POLL_SECONDS``."""
    reader = RuntimeStateReader(redis_client)
    last = {proxy_id: soft.checked_at for proxy_id, soft in (await reader.health(proxy_ids)).items()}
    seen: dict[str, list[datetime]] = {proxy_id: [] for proxy_id in proxy_ids}
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        await asyncio.sleep(POLL_SECONDS)
        for proxy_id, soft in (await reader.health(proxy_ids)).items():
            if soft.checked_at != last.get(proxy_id):
                seen[proxy_id].append(soft.checked_at)
                last[proxy_id] = soft.checked_at
    return seen


async def idle_seconds(redis_client: RedisClient, kind: JobKind) -> list[float]:
    """How long each entry pending on ``kind``'s job stream has gone since its holder last claimed or refreshed it."""
    pending = await redis_client.client.xpending_range(JOB_STREAM_KEY.format(kind=kind), WORKER_GROUP, "-", "+", 100)
    return [int(row["time_since_delivered"]) / 1000 for row in pending]


async def test_a_proxy_added_through_the_api_is_checked_within_a_second(
    cluster: Cluster, upstream: FakeUpstream, redis_client: RedisClient
) -> None:
    api, worker = cluster.process(Role.API, "api"), cluster.process(Role.WORKER, "worker")
    await cluster.start(api, worker)
    await cluster.ready()
    pool = await create_pool(api, upstream)
    await eventually(
        lambda: soft_health(redis_client, pool.proxy_id), within=ROUTED_WITHIN_SECONDS, what="the first proxy checked"
    )
    # The workers idle: nothing left on the stream, every runner waiting on its read.
    await eventually(lambda: drained(redis_client, JobKind.HEALTH), within=ROUTED_WITHIN_SECONDS, what="no check left")

    started = time.monotonic()
    proxy_id = await pool.add_proxy(api, upstream)
    await eventually(
        lambda: soft_health(redis_client, proxy_id), within=ROUTED_WITHIN_SECONDS, what="the new proxy checked"
    )
    took = time.monotonic() - started
    assert took <= CHECKED_WITHIN_SECONDS, f"checked {took:.2f} s after it was added"


async def test_periodic_checks_are_spread_over_the_period_each_proxy_at_its_own_second(
    cluster: Cluster, upstream: FakeUpstream, redis_client: RedisClient
) -> None:
    api, controller, worker = (
        cluster.process(role, role.value, health_check_interval=SPREAD_PERIOD_SECONDS)
        for role in (Role.API, Role.CONTROLLER, Role.WORKER)
    )
    await cluster.start(api, controller, worker)
    await cluster.ready()
    pool = await create_pool(api, upstream)
    proxy_ids = [pool.proxy_id, *[await pool.add_proxy(api, upstream) for _ in range(SPREAD_PROXIES - 1)]]

    async def all_checked() -> bool:
        return len(await RuntimeStateReader(redis_client).health(proxy_ids)) == len(proxy_ids)

    # Every proxy's run-now check is done: every check from here on is a periodic one.
    await eventually(all_checked, within=ROUTED_WITHIN_SECONDS, what="every proxy's first check")
    checks = await check_times(redis_client, proxy_ids, seconds=WATCHED_SECONDS)

    producer = next(producer for producer in controller.controller.producers if producer.kind == JobKind.HEALTH)
    slots = {proxy_id: producer.slot(proxy_id) for proxy_id in proxy_ids}
    assert len(set(slots.values())) > 1, slots
    for proxy_id, times in checks.items():
        # Once a period, at the second of the period its key's hash picks.
        assert 2 <= len(times) <= 3, (proxy_id, times)
        late = {(epoch_second(at) - slots[proxy_id]) % SPREAD_PERIOD_SECONDS for at in times}
        assert late <= set(range(LATE_SECONDS + 1)), (proxy_id, slots[proxy_id], times)


async def test_a_worker_whose_loops_are_cancelled_has_its_entries_run_by_another_after_the_claim_timeout(
    cluster: Cluster,
    upstream: FakeUpstream,
    redis_client: RedisClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    short_claims: ClaimTimings,
) -> None:
    api = cluster.process(Role.API, "api")
    workers = [
        cluster.process(Role.WORKER, f"worker-{name}", health_check_timeout=HUNG_CHECK_TIMEOUT_SECONDS)
        for name in ("a", "b")
    ]
    await cluster.start(api, *workers)
    await cluster.ready()
    upstream.hold_next(1)
    pool = await create_pool(api, upstream)
    await until(
        lambda: upstream.held == 1,
        within=ROUTED_WITHIN_SECONDS,
        what="a worker's check of the new proxy hanging at the vendor",
    )
    crashed = next(worker for worker in workers if worker.worker.runners[JobKind.HEALTH].in_flight == 1)
    survivor = next(worker for worker in workers if worker is not crashed)
    holding, taking = crashed.worker.runners[JobKind.HEALTH], survivor.worker.runners[JobKind.HEALTH]

    async def kept_by_its_holder() -> bool:
        idle = await idle_seconds(redis_client, JobKind.HEALTH)
        return taking.in_flight == taking.consumed == 0 and bool(idle) and max(idle) < short_claims.timeout / 2

    # A live worker keeps what it runs: it refreshes the entry, which never idles long, so the other takes nothing.
    await throughout(kept_by_its_holder, seconds=2 * short_claims.timeout, what="the holder keeping its entry")

    crashed.cancel_loops()
    await until(
        lambda: taking.consumed == 1,
        within=short_claims.timeout + short_claims.check + SLACK_SECONDS,
        what="the other worker running the entry the first one held",
    )
    assert await stored_status(db_session_factory, pool.proxy_id) == ProxyStatus.HEALTHY
    stream = await job_stream(redis_client, JobKind.HEALTH)
    assert (stream.length, stream.pending) == (0, 0)

    # The hung check ends once the vendor answers it.
    upstream.release()
    await until(lambda: holding.in_flight == 0, within=ROUTED_WITHIN_SECONDS, what="the hung check ending")


async def test_a_job_that_always_raises_is_given_up_after_five_deliveries(
    cluster: Cluster, redis_client: RedisClient, short_claims: ClaimTimings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[float] = []

    async def refused(job: ProvisionJob, batch: JobBatch) -> None:
        runs.append(time.monotonic())
        raise RuntimeError("the vendor refuses this country group")

    monkeypatch.setattr(ProvisionJob, "run", refused)
    worker = cluster.process(Role.WORKER, "worker")
    await cluster.start(worker)
    await cluster.ready()
    runner = worker.worker.runners[JobKind.PROVISION]

    await JobQueue(redis_client).publish(JobKind.PROVISION, [REFUSED_GROUP])
    await until(
        lambda: runner.dropped == 1,
        within=(MAX_DELIVERIES + 1) * (short_claims.timeout + short_claims.check) + SLACK_SECONDS,
        what="the entry given up",
    )
    given_up = time.monotonic()
    assert len(runs) == MAX_DELIVERIES, runs
    # Each delivery after the first waited for the claim timeout, and the next one after the last run gave it up.
    assert all(later - earlier >= short_claims.timeout - SCHEDULING_SLACK_SECONDS for earlier, later in pairwise(runs))
    assert given_up - runs[-1] < 2 * short_claims.timeout - short_claims.check, given_up - runs[-1]
    stream = await job_stream(redis_client, JobKind.PROVISION)
    assert (stream.length, stream.pending, runner.consumed) == (0, 0, 0)


async def test_periodic_jobs_pause_while_no_controller_leads_and_resume_with_the_next_while_run_now_goes_on(
    cluster: Cluster, upstream: FakeUpstream, redis_client: RedisClient
) -> None:
    first, second = cluster.process(Role.CONTROLLER, "controller-a"), cluster.process(Role.CONTROLLER, "controller-b")
    api, worker = cluster.process(Role.API, "api"), cluster.process(Role.WORKER, "worker")
    await cluster.start(first, api, worker)
    await cluster.ready()
    pool = await create_pool(api, upstream)
    flushes = worker.worker.runners[JobKind.METRICS_FLUSH]
    checked = await eventually(
        lambda: soft_health(redis_client, pool.proxy_id), within=ROUTED_WITHIN_SECONDS, what="the proxy checked"
    )
    await eventually(
        lambda: checked_after(redis_client, pool.proxy_id, checked.checked_at),
        within=RESUMED_WITHIN_SECONDS,
        what="a periodic check",
    )

    await cluster.stop(first)
    # What the leader published before it stopped runs out.
    await eventually(
        lambda: drained(redis_client, JobKind.HEALTH, JobKind.METRICS_FLUSH),
        within=ROUTED_WITHIN_SECONDS,
        what="the last periodic jobs run",
    )
    paused = await soft_health(redis_client, pool.proxy_id)
    assert paused is not None
    last_checked, paused_flushes = paused.checked_at, flushes.consumed

    async def nothing_periodic() -> bool:
        return not await checked_after(redis_client, pool.proxy_id, last_checked) and (
            flushes.consumed == paused_flushes
        )

    await throughout(nothing_periodic, seconds=PAUSE_SECONDS, what="no periodic job while no controller leads")
    # A run-now goes on: a proxy added meanwhile is checked.
    added = await pool.add_proxy(api, upstream)
    await eventually(lambda: soft_health(redis_client, added), within=RUN_NOW_WITHIN_SECONDS, what="the added proxy checked")
    assert await nothing_periodic()

    await cluster.start(second)
    await until(lambda: second.controller.lease.is_held, within=READY_WITHIN_SECONDS, what="the next controller leading")
    await eventually(
        lambda: checked_after(redis_client, pool.proxy_id, last_checked),
        within=RESUMED_WITHIN_SECONDS,
        what="the periodic check resumed",
    )
    await until(lambda: flushes.consumed > paused_flushes, within=RESUMED_WITHIN_SECONDS, what="the metrics flush resumed")


async def test_compaction_keeps_its_period_across_a_controller_failover(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.roles.controller.COMPACTION_PERIOD_SECONDS", COMPACTION_PERIOD_SECONDS)
    leader, standby = cluster.process(Role.CONTROLLER, "controller-a"), cluster.process(Role.CONTROLLER, "controller-b")
    worker = cluster.process(Role.WORKER, "worker")
    await cluster.start(leader)
    await until(lambda: leader.controller.lease.is_held, within=READY_WITHIN_SECONDS, what="the first controller leading")
    await cluster.start(standby, worker)
    await cluster.ready()
    compactions = worker.worker.runners[JobKind.COMPACTION]

    await until(
        lambda: compactions.consumed >= 1,
        within=COMPACTION_PERIOD_SECONDS + SLACK_SECONDS,
        what="the first compaction",
    )
    first = time.monotonic()

    await cluster.stop(leader)
    await until(lambda: standby.controller.lease.is_held, within=FAILOVER_WITHIN_SECONDS, what="the standby leading")
    await until(
        lambda: compactions.consumed >= 2,
        within=COMPACTION_PERIOD_SECONDS + SLACK_SECONDS,
        what="a compaction published by the new leader",
    )
    # At the same second of the period as the old leader's: a period after the first, as if no failover happened.
    assert time.monotonic() - first <= COMPACTION_PERIOD_SECONDS + KEPT_PERIOD_SLACK_SECONDS


async def test_after_a_flushall_every_work_stream_comes_back_and_periodic_and_run_now_jobs_go_on(
    cluster: Cluster, upstream: FakeUpstream, redis_client: RedisClient
) -> None:
    roles = await start_split(cluster)
    pool = await create_pool(roles.api, upstream)
    await eventually(
        lambda: soft_health(redis_client, pool.proxy_id), within=ROUTED_WITHIN_SECONDS, what="the proxy checked"
    )
    flushes = roles.worker.worker.runners[JobKind.METRICS_FLUSH]

    await redis_client.client.flushall()
    wiped = utc_now()
    ran = flushes.consumed

    # Every job stream, and the sightings and observations, has the workers' group again with the worker in it.
    await eventually(
        lambda: groups_back(redis_client, JobKind), within=REGROUPED_WITHIN_SECONDS, what="every work stream's group back"
    )
    # The lease taken again, the producers publish on.
    await eventually(
        lambda: checked_after(redis_client, pool.proxy_id, wiped),
        within=RESUMED_WITHIN_SECONDS,
        what="the proxy checked after the wipe",
    )
    await until(lambda: flushes.consumed > ran, within=RESUMED_WITHIN_SECONDS, what="a metrics flush after the wipe")
    # And a run-now asked after the wipe runs.
    added = await pool.add_proxy(roles.api, upstream)
    await eventually(lambda: soft_health(redis_client, added), within=RUN_NOW_WITHIN_SECONDS, what="the added proxy checked")


async def test_the_same_key_published_twice_moves_the_proxy_status_once(
    cluster: Cluster,
    upstream: FakeUpstream,
    redis_client: RedisClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    api = cluster.process(Role.API, "api")
    workers = (cluster.process(Role.WORKER, "worker-a"), cluster.process(Role.WORKER, "worker-b"))
    await cluster.start(api, *workers)
    await cluster.ready()
    pool = await create_pool(api, upstream)

    async def healthy_and_announced() -> bool:
        return await stored_status(db_session_factory, pool.proxy_id) == ProxyStatus.HEALTHY and await drained(
            redis_client, JobKind.HEALTH
        )

    def health_runs() -> int:
        return sum(worker.worker.runners[JobKind.HEALTH].consumed for worker in workers)

    await eventually(healthy_and_announced, within=ROUTED_WITHIN_SECONDS, what="the proxy healthy")
    moved_healthy = await change_entry(redis_client, ChangeKind.PROXY, pool.proxy_id)
    runs = health_runs()

    upstream.failing = True
    await JobQueue(redis_client).publish(JobKind.HEALTH, [pool.proxy_id, pool.proxy_id])
    await until(lambda: health_runs() == runs + 2, within=ROUTED_WITHIN_SECONDS, what="both entries run")

    # Both checks failed, and the status moved from healthy once: one change entry announced it.
    assert await stored_status(db_session_factory, pool.proxy_id) == ProxyStatus.DEGRADED
    moves = await change_entries(redis_client, ChangeKind.PROXY, pool.proxy_id, after=moved_healthy)
    assert len(moves) == 1, moves
