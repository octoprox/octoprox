# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Traffic limits end to end: a mebibyte through a gateway blocks its connector, and a raised limit lifts the block.

The gateway's metering flush asks the workers for a decision, a block reaches the gateway by the runtime stream and the
feed, and a raised limit is decided again by the edit's own run-now or, when that was lost, by index hygiene, which
also ends the pending state the api shows meanwhile.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from api.core.roles import JobKind
from api.gateway.metering import METERING_INTERVAL_SECONDS
from api.models.connector import DEFAULT_TRAFFIC_LIMIT_STATUS
from tests.e2e.fakes import BYTES_PATH, ECHO_PATH, FakeTarget, FakeUpstream
from tests.e2e.harness import (
    ROUTED_WITHIN_SECONDS,
    SLACK_SECONDS,
    Cluster,
    Pool,
    Process,
    Roles,
    create_pool,
    fetch,
    start_split,
    until,
    until_routed,
)

pytestmark = pytest.mark.e2e

# The connector's limit, and what the client downloads through it: the
# headers on top of the body take the connector past the limit.
LIMIT_BYTES = 1 << 20
TRAFFIC_CONFIG: dict[str, Any] = {"limit_bytes": LIMIT_BYTES, "action": "block", "period": "day"}
# The limit the admin raises it to: far above anything the test carries.
RAISED_LIMIT_BYTES = 64 << 20
# The metrics flush an hour apart in these tests, so every decision reads the
# connector's bytes in the live window. Why: a traffic decision that runs
# while a flush has taken the windows from Redis and not yet committed them to
# Postgres misses those bytes, and nothing asks again until the connector's
# next metering flush that carries bytes, since index hygiene decides again
# only the connectors blocked or pending. That gap is accepted: in production
# such a block lands up to one metering window late. These tests bound the
# block by seconds from the flush that carried the bytes, so with a flush every
# second they would fail now and then.
QUIET_FLUSH = {"metrics_flush_interval": 3600}
# How long the gateway's metering may take to write a transfer's window to
# Redis: its fixed cadence.
METERED_WITHIN_SECONDS = METERING_INTERVAL_SECONDS + SLACK_SECONDS
# How long the block may take to reach the gateway once the window is in
# Redis: the decision, its runtime entry, the runtime section and its
# publish, each as the one before it lands. The rest is room for a busy machine.
BLOCKED_WITHIN_SECONDS = 3.0
# The same chain from a raised limit's own run-now entry.
LIFTED_WITHIN_SECONDS = 3.0
# Index hygiene's period in the test of a dropped run-now: the controller's constant, shortened.
HYGIENE_PERIOD_SECONDS = 4


async def raise_limit(api: Process, pool: Pool) -> None:
    """Raise the pool's connector's limit through the api to ``RAISED_LIMIT_BYTES``."""
    response = await api.http.patch(
        f"/api/v1/projects/{pool.project_id}/connectors/{pool.connector_id}",
        json={"traffic_config": {**TRAFFIC_CONFIG, "limit_bytes": RAISED_LIMIT_BYTES}},
    )
    assert response.status_code == 200, response.text


async def traffic_usage(api: Process, pool: Pool) -> dict[str, Any]:
    """The pool's connector's traffic usage as the api answers it."""
    response = await api.http.get(f"/api/v1/projects/{pool.project_id}/connectors/{pool.connector_id}")
    assert response.status_code == 200, response.text
    usage: dict[str, Any] = response.json()["traffic_usage"]
    return usage


async def blocked_by_a_download(
    roles: Roles, pool: Pool, upstream: FakeUpstream, target: FakeTarget
) -> None:
    """Download the limit's worth through the gateway, then check the block comes within seconds of the metering flush.

    Once blocked, the gateway refuses the project with the limit's status and asks the vendor nothing.
    """
    metering = roles.gateway.gateway.metering
    published = metering.published
    page = await fetch(roles.gateway, pool, target.url(f"{BYTES_PATH}{LIMIT_BYTES}"))
    assert page.status_code == 200 and len(page.content) == LIMIT_BYTES

    await until(
        lambda: metering.published > published,
        within=METERED_WITHIN_SECONDS,
        what="the gateway's metering flush",
    )
    await until(
        lambda: roles.gateway.is_traffic_blocked(pool.connector_id),
        within=BLOCKED_WITHIN_SECONDS,
        what="the block on the gateway",
    )
    asked = len(upstream.carried)
    refused = await fetch(roles.gateway, pool, target.url("/e2e/over-the-limit"))
    assert refused.status_code == DEFAULT_TRAFFIC_LIMIT_STATUS
    assert all(request.target == target.url(ECHO_PATH) for request in upstream.carried[asked:])


async def test_a_mebibyte_through_the_gateway_blocks_its_connector_and_a_raised_limit_serves_again(
    cluster: Cluster, upstream: FakeUpstream, target: FakeTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No index hygiene within the test, so only the edit's own run-now can lift the block.
    monkeypatch.setattr("api.roles.controller.INDEX_HYGIENE_PERIOD_SECONDS", 3600)
    roles = await start_split(cluster, **QUIET_FLUSH)
    pool = await create_pool(roles.api, upstream, traffic_config=TRAFFIC_CONFIG)
    await until_routed(roles.gateway, pool.proxy_id)
    await blocked_by_a_download(roles, pool, upstream, target)

    await raise_limit(roles.api, pool)
    await until(
        lambda: not roles.gateway.is_traffic_blocked(pool.connector_id),
        within=LIFTED_WITHIN_SECONDS,
        what="the block lifted on the gateway",
    )
    # The decision the edit asked for, stored before the gateway heard of it, also ended the api's pending state.
    usage = await traffic_usage(roles.api, pool)
    assert (usage["blocked"], usage["pending"]) == (False, False)
    served = await fetch(roles.gateway, pool, target.url("/e2e/raised"))
    assert served.status_code == 200 and served.json()["path"] == "/e2e/raised"


async def test_a_raised_limit_whose_run_now_was_lost_serves_again_within_an_index_hygiene_period(
    cluster: Cluster, upstream: FakeUpstream, target: FakeTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.roles.controller.INDEX_HYGIENE_PERIOD_SECONDS", HYGIENE_PERIOD_SECONDS)
    roles = await start_split(cluster, **QUIET_FLUSH)
    pool = await create_pool(roles.api, upstream, traffic_config=TRAFFIC_CONFIG)
    await until_routed(roles.gateway, pool.proxy_id)
    await blocked_by_a_download(roles, pool, upstream, target)

    def limit_on_gateway() -> int | None:
        """The connector's traffic limit in the feed the gateway applied; None without one, or without the connector."""
        view = roles.gateway.gateway.router.snapshot.connector_view(pool.connector_id)
        return view.traffic.limit_bytes if view is not None else None

    # The api's run-now entries for traffic decisions are lost, as one given up or trimmed away is: the api still
    # marks the connector pending in the same MULTI.
    jobs = roles.api.api.jobs
    append = jobs.append

    def losing(pipe: Any, kind: JobKind, key: str) -> None:
        if kind != JobKind.TRAFFIC:
            append(pipe, kind, key)

    monkeypatch.setattr(jobs, "append", losing)
    # Raised just after a hygiene run and the decision it asked for, so the next one is a period away.
    runners = roles.worker.worker.runners
    hygiene, decisions = runners[JobKind.INDEX_HYGIENE], runners[JobKind.TRAFFIC]
    swept, decided = hygiene.consumed, decisions.consumed
    await until(
        lambda: hygiene.consumed > swept and decisions.consumed > decided,
        within=HYGIENE_PERIOD_SECONDS + SLACK_SECONDS,
        what="an index hygiene run and its traffic decision",
    )
    raised = time.monotonic()
    await raise_limit(roles.api, pool)

    # The raised limit is in the feed, and the block holds: nothing asked for a decision, and the api says it waits.
    await until(
        lambda: limit_on_gateway() == RAISED_LIMIT_BYTES,
        within=ROUTED_WITHIN_SECONDS,
        what="the raised limit on the gateway",
    )
    assert roles.gateway.is_traffic_blocked(pool.connector_id)
    usage = await traffic_usage(roles.api, pool)
    assert (usage["blocked"], usage["pending"]) == (True, True)
    await until(
        lambda: not roles.gateway.is_traffic_blocked(pool.connector_id),
        within=HYGIENE_PERIOD_SECONDS + SLACK_SECONDS,
        what="the block lifted by index hygiene's decision",
    )
    assert time.monotonic() - raised <= HYGIENE_PERIOD_SECONDS + SLACK_SECONDS
    # The decision that lifted it answered the request: the api no longer shows the connector waiting.
    usage = await traffic_usage(roles.api, pool)
    assert (usage["blocked"], usage["pending"]) == (False, False)
    served = await fetch(roles.gateway, pool, target.url("/e2e/raised"))
    assert served.status_code == 200 and served.json()["path"] == "/e2e/raised"
