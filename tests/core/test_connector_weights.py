# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector weights in the router's selection.

Definitions go through :class:`FeedBuilder`, so groups and shares come from
the weights the snapshot parsed once per connector section.
"""

from collections import Counter
from datetime import datetime, timedelta

import pytest

from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.routing.router import Router
from tests.routing.builder import FeedBuilder

_CREATED_AT = datetime(2026, 10, 1, 9, 0, 0)


def _feed(strategy: str = "random") -> FeedBuilder:
    return FeedBuilder().add(
        Project(id="p", name="P", username="p", password="pw", routing_strategy=strategy),
        Credential(id="k", name="K", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p", config={}),
    )


def _connector(feed: FeedBuilder, cid: str, rows: int, weight: int | None = None, enabled: bool = True) -> None:
    routing = {"weight": weight} if weight is not None else {}
    # Created in the order a test adds them: the feed orders a project's connectors by creation.
    feed.add(Connector(
        id=cid, name=cid, credential_id="k", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id="p", routing_config=routing, enabled=enabled,
        created_at=_CREATED_AT + timedelta(seconds=len(feed.connectors)),
    ))
    for i in range(rows):
        feed.add(Proxy(id=f"{cid}-{i}", host=f"{cid}{i}", port=1, connector_id=cid, status=ProxyStatus.HEALTHY))


async def _connector_of_pick(router: Router) -> str:
    selected = await router.select_proxy_for_project("p")
    assert selected is not None
    return selected.connector_id


class TestGroupByConnector:
    def test_groups_carry_connector_weights_in_first_seen_order(self) -> None:
        feed = _feed()
        _connector(feed, "pool", 3, weight=2)
        _connector(feed, "gateway", 1)
        router = feed.router()
        groups = router.group_by_connector(router.get_routable_proxies_for_project("p"))
        assert [(g.key, g.weight, len(g.proxies)) for g in groups] == [("pool", 2, 3), ("gateway", 1, 1)]

    def test_unknown_connector_gets_default_weight(self) -> None:
        router = _feed().router()
        orphan = Proxy(id="x", host="x", port=1, connector_id="gone", status=ProxyStatus.HEALTHY)
        groups = router.group_by_connector([orphan])
        assert groups[0].weight == 1

    def test_unreadable_weight_routes_at_the_default(self) -> None:
        feed = _feed()
        _connector(feed, "pool", 1)
        feed.connectors["pool"].routing_config = {"weight": 0}
        router = feed.router()
        groups = router.group_by_connector(router.get_routable_proxies_for_project("p"))
        assert [(g.key, g.weight) for g in groups] == [("pool", 1)]


class TestWeightedSelection:
    async def test_share_is_by_weight_not_row_count(self) -> None:
        feed = _feed("random")
        _connector(feed, "pool", 10)
        _connector(feed, "gateway", 1)
        router = feed.router()
        counts = Counter([await _connector_of_pick(router) for _ in range(3000)])
        # Equal weights: about half each, although the pool has ten rows and the gateway one.
        assert 0.44 < counts["gateway"] / 3000 < 0.56

    async def test_weight_moves_the_split(self) -> None:
        feed = _feed("random")
        _connector(feed, "pool", 10, weight=1)
        _connector(feed, "gateway", 1, weight=3)
        router = feed.router()
        counts = Counter([await _connector_of_pick(router) for _ in range(3000)])
        assert 0.70 < counts["gateway"] / 3000 < 0.80

    async def test_round_robin_cycles_inside_each_connector(self) -> None:
        feed = _feed("round_robin")
        _connector(feed, "a", 2, weight=1)
        _connector(feed, "b", 1, weight=1)
        router = feed.router()
        picks = []
        for _ in range(6):
            selected = await router.select_proxy_for_project("p")
            assert selected is not None
            picks.append(selected.id)
        assert Counter(p.split("-")[0] for p in picks) == {"a": 3, "b": 3}
        assert [p for p in picks if p.startswith("a")] == ["a-0", "a-1", "a-0"]

    async def test_connector_without_healthy_rows_takes_no_share(self) -> None:
        feed = _feed("random")
        _connector(feed, "a", 1, weight=50)
        _connector(feed, "b", 1, weight=1)
        feed.proxies["a-0"].status = ProxyStatus.UNHEALTHY
        router = feed.router()
        for _ in range(20):
            assert await _connector_of_pick(router) == "b"

    async def test_disabled_connector_takes_no_share(self) -> None:
        feed = _feed("random")
        _connector(feed, "a", 1, weight=50, enabled=False)
        _connector(feed, "b", 1, weight=1)
        router = feed.router()
        for _ in range(20):
            assert await _connector_of_pick(router) == "b"

    @pytest.mark.parametrize("strategy", ["random", "round_robin", "least_used", "sticky", "health_based"])
    async def test_single_connector_unchanged(self, strategy: str) -> None:
        feed = _feed(strategy)
        _connector(feed, "only", 3, weight=7)
        router = feed.router()
        seen = set()
        for i in range(30):
            selected = await router.select_proxy_for_project("p", session_id=f"s{i}")
            assert selected is not None
            seen.add(selected.id)
        assert seen <= {"only-0", "only-1", "only-2"}
        assert seen
