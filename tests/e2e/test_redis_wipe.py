# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Losing everything in Redis: the gateway keeps routing, and the cluster rebuilds what the wipe took.

After a ``FLUSHDB`` the controller takes its lease again as a new term, republishes the feed and asks for
``reattribute all`` (left queued, since no worker here takes that kind), and the workers recreate their groups on the
job streams, so the health checks resume within a period.
"""

from __future__ import annotations

import pytest

from api.core import utc_now
from api.core.roles import JobKind
from api.db.redis import FEED_HEAD_KEY, RedisClient
from api.jobs.attribution import REATTRIBUTE_ALL
from tests.conftest import queued_job_keys
from tests.e2e.fakes import FakeTarget, FakeUpstream
from tests.e2e.harness import (
    FAST_LOOPS,
    REGROUPED_WITHIN_SECONDS,
    SLACK_SECONDS,
    Cluster,
    checked_after,
    create_pool,
    eventually,
    fetch,
    groups_back,
    job_stream,
    start_all_in_one,
    until_routed,
)

pytestmark = pytest.mark.e2e

# How long the controller may take to republish the feed after a wipe. The
# lease went with it: a fenced publish finds the lease gone, or a refresh
# does, and the lease's keeper takes it again at its next turn, at most one
# refresh interval (2 s) later; the new term compiles and publishes in its
# first cycle. The rest is room for a busy machine.
REPUBLISHED_WITHIN_SECONDS = 6.0
# How long the first health check may take once the feed is back: the new
# term's producer publishes each proxy's check within a period, and a worker
# runs it at once.
CHECKED_WITHIN_SECONDS = FAST_LOOPS["health_check_interval"] + SLACK_SECONDS
# The job kinds the worker runs: all but ``reattribute``, so the wipe's request stays queued on its stream.
WITHOUT_REATTRIBUTE = [kind for kind in JobKind if kind != JobKind.REATTRIBUTE]


async def test_after_a_flushdb_the_gateway_keeps_routing_and_the_cluster_rebuilds_what_was_lost(
    cluster: Cluster, upstream: FakeUpstream, target: FakeTarget, redis_client: RedisClient
) -> None:
    roles = await start_all_in_one(cluster, worker_jobs=WITHOUT_REATTRIBUTE)
    pool = await create_pool(roles.api, upstream)
    await until_routed(roles.gateway, pool.proxy_id)
    assert (await fetch(roles.gateway, pool, target.url("/e2e/before"))).status_code == 200
    lease = roles.controller.controller.lease
    term = lease.term

    await redis_client.client.flushdb()

    # Served from the feed the gateway applied: no read on the request path needs Redis.
    after = await fetch(roles.gateway, pool, target.url("/e2e/after"))
    assert after.status_code == 200 and after.json()["path"] == "/e2e/after"

    # Every kind the worker takes, and the sightings and observations, have the workers' group again, the worker in it.
    await eventually(
        lambda: groups_back(redis_client, WITHOUT_REATTRIBUTE),
        within=REGROUPED_WITHIN_SECONDS,
        what="the workers' groups recreated",
    )

    await eventually(
        lambda: redis_client.client.exists(FEED_HEAD_KEY),
        within=REPUBLISHED_WITHIN_SECONDS,
        what="the routing feed republished",
    )
    republished = utc_now()
    # Taken again as a new term, by the same controller.
    assert lease.is_held and lease.term > term
    assert await redis_client.client.get(lease.key) == roles.controller.node.instance_id

    async def reattribute_all_queued() -> bool:
        return REATTRIBUTE_ALL in await queued_job_keys(redis_client, JobKind.REATTRIBUTE)

    # Every proxy is judged again: the wipe's request waits on the stream no worker here takes.
    await eventually(
        reattribute_all_queued, within=REPUBLISHED_WITHIN_SECONDS, what="reattribute all requested"
    )
    reattribute = await job_stream(redis_client, JobKind.REATTRIBUTE)
    assert (reattribute.queued, reattribute.consumers) == (1, 0)

    await eventually(
        lambda: checked_after(redis_client, pool.proxy_id, republished),
        within=CHECKED_WITHIN_SECONDS,
        what="the proxy's health checked after the wipe",
    )
    assert roles.gateway.node.readiness() == []
    again = await fetch(roles.gateway, pool, target.url("/e2e/again"))
    assert again.status_code == 200 and again.json()["path"] == "/e2e/again"
    assert {"/e2e/before", "/e2e/after", "/e2e/again"} <= set(target.paths)
