# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A client's requests end to end, from the admin's writes on the api to the project's counters, on two layouts.

On one process of role ``all`` and on one process per role, a proxy the admin adds is checked healthy, routed, carries a
plain and a tunnelled request through the vendor, and both reach the totals the api reports.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.stats import MetricDelta
from tests.e2e.fakes import ECHO_PATH, FakeTarget, FakeUpstream
from tests.e2e.harness import (
    FLUSHED_WITHIN_SECONDS,
    Cluster,
    Roles,
    create_pool,
    eventually,
    fetch,
    fetch_in_tunnel,
    project_totals,
    start_all_in_one,
    start_split,
    until_routed,
)

pytestmark = pytest.mark.e2e

# Starts a layout of processes: returns where the admin writes and the client connects.
type Layout = Callable[[Cluster], Awaitable[Roles]]


@pytest.mark.parametrize("start", [start_all_in_one, start_split], ids=["all", "split"])
async def test_a_proxy_added_through_the_api_carries_client_requests_that_reach_the_counters(
    cluster: Cluster,
    start: Layout,
    upstream: FakeUpstream,
    target: FakeTarget,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    roles = await start(cluster)
    pool = await create_pool(roles.api, upstream)
    await until_routed(roles.gateway, pool.proxy_id)

    page = await fetch(roles.gateway, pool, target.url("/e2e/plain"))
    status_line, tunnelled = await fetch_in_tunnel(roles.gateway, pool, target, "/e2e/tunnel")

    assert page.status_code == 200 and page.json() == {"method": "GET", "path": "/e2e/plain"}
    assert status_line == b"HTTP/1.1 200 Connection Established"
    assert json.loads(tunnelled) == {"method": "GET", "path": "/e2e/tunnel"}
    # Both went through the vendor with the vendor's credentials only: the
    # client's Proxy-Authorization never left the gateway.
    carried = [request for request in upstream.carried if request.target != target.url(ECHO_PATH)]
    assert [(request.method, request.target, request.authorization) for request in carried] == [
        ("GET", target.url("/e2e/plain"), [upstream.authorization]),
        ("CONNECT", target.authority, [upstream.authorization]),
    ]

    async def flushed() -> MetricDelta | None:
        totals = await project_totals(db_session_factory, pool.project_id)
        return totals if totals is not None and totals.request_count >= 2 else None

    totals = await eventually(
        flushed, within=FLUSHED_WITHIN_SECONDS, what="both requests in the project's totals"
    )
    assert (totals.request_count, totals.success_count, totals.failure_count) == (2, 2, 0)
    assert totals.bytes_sent > 0 and totals.bytes_received > 0
    # Flushed, so nothing is left in the Redis window: the api's counters are the totals, counted once.
    response = await roles.api.http.get(f"/api/v1/projects/{pool.project_id}/metrics")
    counters = response.json()["pool"]
    assert (
        counters["total_requests"],
        counters["total_successes"],
        counters["total_failures"],
    ) == (2, 2, 0)
    assert (counters["total_bytes_sent"], counters["total_bytes_received"]) == (
        totals.bytes_sent,
        totals.bytes_received,
    )
    assert (counters["total_proxies"], counters["healthy_proxies"]) == (1, 1)

    # Every process heartbeats: the System view lists each under its role, and the controller leading.
    system = (await roles.api.http.get("/api/v1/system/stats")).json()
    listed = {
        role: [instance["instance_id"] for instance in instances]
        for role, instances in system["instances"].items()
    }
    assert listed == {
        process.node.role.value: [process.node.instance_id] for process in roles.processes
    }
    assert system["controller"]["leader"] == roles.controller.node.instance_id
