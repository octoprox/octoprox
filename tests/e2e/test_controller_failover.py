# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Controller failover: when the leading controller stops, the standby leads within a poll and the feed stays published.

The new leader compiles afresh, so a password changed after the failover reaches the gateway, and its producers keep
publishing the workers' metrics flush, so the requests carried before and after it reach the project's totals.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.roles import Role
from api.core.stats import MetricDelta
from api.db.redis import RedisClient
from api.db.streams import ChangeKind
from tests.e2e.fakes import FakeTarget, FakeUpstream
from tests.e2e.harness import (
    FAILOVER_WITHIN_SECONDS,
    FLUSHED_WITHIN_SECONDS,
    READY_WITHIN_SECONDS,
    ROUTED_WITHIN_SECONDS,
    Cluster,
    change_entry,
    create_pool,
    eventually,
    fetch,
    project_totals,
    until,
    until_routed,
)

pytestmark = pytest.mark.e2e

# The project password the admin sets once the standby leads.
ROTATED_PASSWORD = "rotated-after-failover"


async def test_the_standby_leads_within_a_poll_and_keeps_the_feed_and_the_metrics_flush_going(
    cluster: Cluster,
    upstream: FakeUpstream,
    target: FakeTarget,
    redis_client: RedisClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    leader, standby = (
        cluster.process(Role.CONTROLLER, "controller-a"),
        cluster.process(Role.CONTROLLER, "controller-b"),
    )
    api, worker, gateway = (
        cluster.process(role, role.value) for role in (Role.API, Role.WORKER, Role.GATEWAY)
    )
    await cluster.start(leader)
    # Redis holds no lease yet: the first controller takes it as its keeper's first act.
    await until(
        lambda: leader.controller.lease.is_held,
        within=READY_WITHIN_SECONDS,
        what="the first controller leading",
    )
    await cluster.start(standby, api, worker, gateway)
    await cluster.ready()
    # A standby is ready: leading is not readiness.
    assert not standby.controller.lease.is_held
    pool = await create_pool(api, upstream)
    await until_routed(gateway, pool.proxy_id)
    assert (await fetch(gateway, pool, target.url("/e2e/first-leader"))).status_code == 200

    stopped = time.monotonic()
    await cluster.stop(leader)
    await until(
        lambda: standby.controller.lease.is_held,
        within=FAILOVER_WITHIN_SECONDS,
        what="the standby leading",
    )
    assert time.monotonic() - stopped <= FAILOVER_WITHIN_SECONDS
    assert await redis_client.client.get(standby.controller.lease.key) == standby.node.instance_id

    def password_on_gateway() -> str | None:
        project = gateway.gateway.router.get_project(pool.project_id)
        return project.password if project is not None else None

    written = standby.controller.builder.sections_written
    changed = await api.http.patch(
        f"/api/v1/projects/{pool.project_id}", json={"password": ROTATED_PASSWORD}
    )
    assert changed.status_code == 200, changed.text
    saved = await change_entry(redis_client, ChangeKind.PROJECT, pool.project_id)
    await until(
        lambda: password_on_gateway() == ROTATED_PASSWORD,
        within=ROUTED_WITHIN_SECONDS,
        what="the new password on the gateway",
    )
    # The new leader compiled through the save's entry and published the section it changed.
    await until(lambda: standby.has_compiled(saved), within=ROUTED_WITHIN_SECONDS, what="the save compiled")
    assert standby.controller.builder.sections_written > written

    let_in = await fetch(gateway, pool, target.url("/e2e/second-leader"), password=ROTATED_PASSWORD)
    assert let_in.status_code == 200 and let_in.json()["path"] == "/e2e/second-leader"
    assert (await fetch(gateway, pool, target.url("/e2e/old-password"))).status_code == 407
    assert gateway.node.readiness() == [] and standby.node.readiness() == []

    async def both_flushed() -> MetricDelta | None:
        totals = await project_totals(db_session_factory, pool.project_id)
        return totals if totals is not None and totals.request_count >= 2 else None

    # The metrics flush is published by whichever controller leads: the
    # request carried before the failover and the one after it both reach
    # the project's totals.
    totals = await eventually(
        both_flushed, within=FLUSHED_WITHIN_SECONDS, what="both requests in the project's totals"
    )
    assert (totals.request_count, totals.success_count) == (2, 2)
