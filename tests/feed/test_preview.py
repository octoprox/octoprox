# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing previews: every project's router holds each project's routing as its own preview builds it, the
quarantines and traffic blocks read from Redis, built on the event loop or off it alike."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.demand_tracker import DemandTracker
from api.db.redis import QUARANTINE_KEY, TRAFFIC_BLOCKED_KEY, RedisClient
from api.feed.preview import RoutingPreview
from api.feed.state import RuntimeStateReader
from api.models.proxy import ProxyStatus
from api.routing.router import Router
from api.services.read_model import RuntimeView
from tests.feed.test_compiler import (
    Harness,
    make_connector,
    make_credential,
    make_project,
    make_proxy,
)


@pytest.fixture
def harness(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,  # empties the tables after the test
    test_settings: Settings,
) -> Harness:
    return Harness(db_session_factory, test_settings)


@pytest.fixture
async def two_projects(harness: Harness, redis_client: RedisClient) -> None:
    """Two projects of two proxies each; one proxy in quarantine and one connector blocked, as Redis holds them."""
    for project_id in ("project-1", "project-2"):
        credential = make_credential(f"credential-{project_id}", project_id=project_id)
        connector = make_connector(f"connector-{project_id}", credential)
        await harness.save(
            make_project(project_id),
            credential,
            connector,
            make_proxy(f"proxy-{project_id}-a", connector, status=ProxyStatus.HEALTHY),
            make_proxy(f"proxy-{project_id}-b", connector, status=ProxyStatus.HEALTHY),
        )
    await redis_client.client.zadd(QUARANTINE_KEY, {"proxy-project-1-a": time.time() + 600})
    until = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, "connector-project-2", until.isoformat())


def preview_of(harness: Harness, redis_client: RedisClient, *, off_loop: bool) -> RoutingPreview:
    view = RuntimeView(
        harness.session_factory, RuntimeStateReader(redis_client), harness.registry, DemandTracker(redis_client)
    )
    return RoutingPreview(harness.preview, view, harness.registry, default_strategy="round_robin", off_loop=off_loop)


def routable(router: Router, project_id: str) -> list[str]:
    return sorted(proxy.id for proxy in router.get_routable_proxies_for_project(project_id))


@pytest.mark.parametrize("off_loop", [False, True], ids=["on the loop", "off the loop"])
async def test_every_project_holds_what_each_project_s_preview_builds(
    harness: Harness, redis_client: RedisClient, two_projects: None, off_loop: bool
) -> None:
    preview = preview_of(harness, redis_client, off_loop=off_loop)

    every = await preview.every_project()

    for project_id in ("project-1", "project-2"):
        one = await preview.project(project_id)
        assert routable(every, project_id) == routable(one, project_id)
        assert [view.connector.id for view in every.snapshot.connector_views(project_id)] == [
            view.connector.id for view in one.snapshot.connector_views(project_id)
        ]
    # The quarantine and the block, as the traffic job and the gateways stored them.
    assert routable(every, "project-1") == ["proxy-project-1-b"]
    assert every.is_traffic_blocked("connector-project-2")
    assert routable(every, "project-2") == []


async def test_a_project_that_is_gone_previews_as_nothing(
    harness: Harness, redis_client: RedisClient, two_projects: None
) -> None:
    router = await preview_of(harness, redis_client, off_loop=True).project("project-missing")

    assert list(router.snapshot.connector_views("project-missing")) == []
