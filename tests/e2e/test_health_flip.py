# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A proxy whose vendor starts failing: a worker marks it unhealthy and the gateway stops routing to it until it recovers.

Its checks are published every second, the third failure in a row stores it unhealthy, and the controller compiles the
status into the feed, so the gateway answers its no-proxy 502 without asking the vendor until a check passes again.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.models.proxy import ProxyStatus
from tests.e2e.fakes import ECHO_PATH, FakeTarget, FakeUpstream
from tests.e2e.harness import (
    ROUTED_WITHIN_SECONDS,
    Cluster,
    create_pool,
    eventually,
    fetch,
    proxy_status,
    start_all_in_one,
    stored_status,
    until,
    until_routed,
)

pytestmark = pytest.mark.e2e

# How long a failing proxy may take to be stored unhealthy: three failed
# checks in a row, one published a second, each failing at once on the
# vendor's 503.
UNHEALTHY_WITHIN_SECONDS = 15.0


async def test_a_failing_vendor_makes_the_proxy_unhealthy_and_the_gateway_answers_no_proxy_until_it_recovers(
    cluster: Cluster,
    upstream: FakeUpstream,
    target: FakeTarget,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    roles = await start_all_in_one(cluster)
    pool = await create_pool(roles.api, upstream)
    await until_routed(roles.gateway, pool.proxy_id)
    assert (await fetch(roles.gateway, pool, target.url("/e2e/healthy"))).status_code == 200

    upstream.failing = True

    async def stored_unhealthy() -> bool:
        return await stored_status(db_session_factory, pool.proxy_id) == ProxyStatus.UNHEALTHY

    await eventually(
        stored_unhealthy, within=UNHEALTHY_WITHIN_SECONDS, what="the proxy stored unhealthy"
    )
    await until(
        lambda: proxy_status(roles.gateway, pool.proxy_id) == ProxyStatus.UNHEALTHY,
        within=ROUTED_WITHIN_SECONDS,
        what="the gateway applying the unhealthy status",
    )
    asked = len(upstream.carried)
    refused = await fetch(roles.gateway, pool, target.url("/e2e/unhealthy"))

    assert refused.status_code == 502
    assert refused.text == "No upstream proxy available for this domain"
    # Answered by the gateway itself: the vendor was not asked to carry it (its health checks only).
    assert all(request.target == target.url(ECHO_PATH) for request in upstream.carried[asked:])
    assert "/e2e/unhealthy" not in target.paths
    shown = (
        await roles.api.http.get(f"/api/v1/projects/{pool.project_id}/proxies/{pool.proxy_id}")
    ).json()
    assert shown["status"] == ProxyStatus.UNHEALTHY.value

    upstream.failing = False
    await until_routed(roles.gateway, pool.proxy_id)
    recovered = await fetch(roles.gateway, pool, target.url("/e2e/recovered"))
    assert recovered.status_code == 200 and recovered.json()["path"] == "/e2e/recovered"
    assert await stored_status(db_session_factory, pool.proxy_id) == ProxyStatus.HEALTHY
