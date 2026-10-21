# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Changes reach every process by the entries of the streams it follows, never by a poll.

Every follower reads in full only as it starts and once an hour, so whatever reaches a process within seconds came by
an entry: a saved setting, a burst of saves compiled about once per compile interval, and a quarantine one gateway
tripped.
"""

from __future__ import annotations

import math
import time

import pytest

from api.core.roles import Role
from api.db.redis import RedisClient
from api.db.streams import ChangeKind
from tests.e2e.fakes import FakeTarget, FakeUpstream
from tests.e2e.harness import (
    FAST_LOOPS,
    ROUTED_WITHIN_SECONDS,
    STARTED_ONLY,
    Cluster,
    Process,
    change_entry,
    create_pool,
    fetch,
    until,
    until_routed,
)

pytestmark = pytest.mark.e2e

# How long a change may take to reach a process: a compile interval, the
# compile and its publish, and the follower's read as the entry arrives, all
# well under a second. The rest is room for a busy machine; a follower's read
# in full comes only once an hour.
PROPAGATED_WITHIN_SECONDS = 3.0
# The attribution setting the admin saves: one the api, the workers and the
# gateways all hold in memory, and one that re-judges no proxy.
SAVED_PREFLIGHT_ATTEMPTS = 7
# The saves of the burst, one after another through the api.
BURST_SAVES = 200
# A proxy rate limit its first request fills: it is quarantined for a minute.
TRIP_AT_ONCE = {
    "max_requests": 1,
    "window_seconds": 60,
    "quarantine_seconds_min": 60,
    "quarantine_seconds_max": 60,
}


async def test_a_saved_setting_reaches_every_gateway_the_other_api_and_the_worker_by_its_entry(
    cluster: Cluster, upstream: FakeUpstream
) -> None:
    def preflight_attempts(process: Process) -> int:
        """The attribution settings' preflight attempts the process holds in memory."""
        geo = process.node.geo
        assert geo is not None, f"{process.name} holds no attribution settings"
        return geo.settings_store.settings.preflight_max_attempts

    writer, reader = cluster.process(Role.API, "api-a"), cluster.process(Role.API, "api-b")
    controller, worker = cluster.process(Role.CONTROLLER, "controller"), cluster.process(Role.WORKER, "worker")
    gateways = (cluster.process(Role.GATEWAY, "gateway-a"), cluster.process(Role.GATEWAY, "gateway-b"))
    await cluster.start(writer, reader, controller, worker, *gateways)
    await cluster.ready()
    pool = await create_pool(writer, upstream)
    for gateway in gateways:
        await until_routed(gateway, pool.proxy_id)
    holders = (*gateways, reader, worker)
    assert all(preflight_attempts(process) != SAVED_PREFLIGHT_ATTEMPTS for process in holders)

    current = (await writer.http.get("/api/v1/geo/settings")).json()["settings"]
    saved = await writer.http.put(
        "/api/v1/geo/settings", json={**current, "preflight_max_attempts": SAVED_PREFLIGHT_ATTEMPTS}
    )
    assert saved.status_code == 200, saved.text

    # The gateways through the feed's global section, the other api and the
    # worker through their refreshers: none of them answers a request meanwhile.
    await until(
        lambda: all(preflight_attempts(process) == SAVED_PREFLIGHT_ATTEMPTS for process in holders),
        within=PROPAGATED_WITHIN_SECONDS,
        what="the saved setting on every gateway, the other api and the worker",
    )
    for gateway in gateways:
        assert gateway.streams()["feed"].full_reads == STARTED_ONLY
    for process in (reader, worker):
        assert process.streams()["changes"].full_reads == STARTED_ONLY


async def test_a_burst_of_saves_is_compiled_at_most_once_per_compile_interval(
    cluster: Cluster, upstream: FakeUpstream, redis_client: RedisClient
) -> None:
    api, controller = cluster.process(Role.API, "api"), cluster.process(Role.CONTROLLER, "controller")
    await cluster.start(api, controller)
    await cluster.ready()
    pool = await create_pool(api, upstream)
    interval = FAST_LOOPS["feed_compile_interval"]

    # Quiet first: the pool's own saves are compiled.
    added = await change_entry(redis_client, ChangeKind.PROXY, pool.proxy_id)
    await until(lambda: controller.has_compiled(added), within=ROUTED_WITHIN_SECONDS, what="the pool compiled")
    compiles = controller.controller.snapshot().compiles

    started = time.monotonic()
    for n in range(BURST_SAVES):
        response = await api.http.patch(
            f"/api/v1/projects/{pool.project_id}/proxies/{pool.proxy_id}", json={"tags": [f"burst-{n}"]}
        )
        assert response.status_code == 200, response.text
    last = await change_entry(redis_client, ChangeKind.PROXY, pool.proxy_id)
    await until(lambda: controller.has_compiled(last), within=ROUTED_WITHIN_SECONDS, what="the burst compiled")
    window = time.monotonic() - started

    # Compiles start at least an interval apart, and every one of the burst's
    # started within the window: from its first save to its last one compiled.
    made = controller.controller.snapshot().compiles - compiles
    assert 1 <= made <= math.floor(window / interval) + 1 < BURST_SAVES, (made, window)


async def test_a_quarantine_tripped_on_one_gateway_reaches_the_other_through_the_feed(
    cluster: Cluster, upstream: FakeUpstream, target: FakeTarget
) -> None:
    api, controller, worker = (
        cluster.process(role, role.value) for role in (Role.API, Role.CONTROLLER, Role.WORKER)
    )
    tripping, other = cluster.process(Role.GATEWAY, "gateway-a"), cluster.process(Role.GATEWAY, "gateway-b")
    await cluster.start(api, controller, worker, tripping, other)
    await cluster.ready()
    pool = await create_pool(api, upstream, rate_limit_config=TRIP_AT_ONCE)
    for gateway in (tripping, other):
        await until_routed(gateway, pool.proxy_id)

    # The request fills the proxy's window: the tripping gateway quarantines it and says so on the runtime stream.
    assert (await fetch(tripping, pool, target.url("/e2e/trip"))).status_code == 200

    # The quarantine is in the feed the other gateway applied, from the controller's runtime section.
    await until(
        lambda: pool.proxy_id in other.gateway.router.snapshot.quarantined,
        within=PROPAGATED_WITHIN_SECONDS,
        what="the quarantine in the other gateway's feed",
    )
    assert other.streams()["feed"].full_reads == STARTED_ONLY
    # The other gateway counted no request, so its rate limiter tripped nothing: it learned the quarantine from the
    # feed alone. Each gateway hears only its own listeners, though both run in this one process.
    assert (tripping.gateway.snapshot().requests, other.gateway.snapshot().requests) == (1, 0)
