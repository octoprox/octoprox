# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Connector weights in ProxyManager selection."""

from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.proxy_manager import ProxyManager
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus


def _manager(strategy: str = "random") -> ProxyManager:
    mock_settings = MagicMock()
    mock_settings.default_strategy = strategy
    mock_settings.instance_id = "test-instance"
    redis = AsyncMock()
    redis.get_sticky_binding.return_value = None
    manager = ProxyManager(MagicMock(), redis, mock_settings)
    manager._projects["p"] = Project(id="p", name="P", username="p", password="pw", routing_strategy=strategy)
    manager.set_project_strategy("p", strategy)
    manager._credentials["k"] = Credential(id="k", name="K", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p", config={})
    return manager


def _connector(manager: ProxyManager, cid: str, rows: int, weight: int | None = None, enabled: bool = True) -> None:
    routing = {"weight": weight} if weight is not None else {}
    manager._connectors[cid] = Connector(
        id=cid, name=cid, credential_id="k", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id="p", routing_config=routing, enabled=enabled,
    )
    for i in range(rows):
        manager._proxies[f"{cid}-{i}"] = Proxy(id=f"{cid}-{i}", host=f"{cid}{i}", port=1, connector_id=cid, status=ProxyStatus.HEALTHY)


class TestGroupByConnector:
    def test_groups_carry_connector_weights_in_first_seen_order(self) -> None:
        manager = _manager()
        _connector(manager, "pool", 3, weight=2)
        _connector(manager, "gateway", 1)
        groups = manager.group_by_connector(manager.get_routable_proxies_for_project("p"))
        assert [(g.key, g.weight, len(g.proxies)) for g in groups] == [("pool", 2, 3), ("gateway", 1, 1)]

    def test_unknown_connector_gets_default_weight(self) -> None:
        manager = _manager()
        orphan = Proxy(id="x", host="x", port=1, connector_id="gone", status=ProxyStatus.HEALTHY)
        groups = manager.group_by_connector([orphan])
        assert groups[0].weight == 1


class TestWeightedSelection:
    async def test_share_is_by_weight_not_row_count(self) -> None:
        manager = _manager("random")
        _connector(manager, "pool", 10)
        _connector(manager, "gateway", 1)
        counts = Counter([(await manager.select_proxy_for_project("p")).connector_id for _ in range(3000)])
        # Equal weights: about half each, although the pool has ten rows and the gateway one.
        assert 0.44 < counts["gateway"] / 3000 < 0.56

    async def test_weight_moves_the_split(self) -> None:
        manager = _manager("random")
        _connector(manager, "pool", 10, weight=1)
        _connector(manager, "gateway", 1, weight=3)
        counts = Counter([(await manager.select_proxy_for_project("p")).connector_id for _ in range(3000)])
        assert 0.70 < counts["gateway"] / 3000 < 0.80

    async def test_round_robin_cycles_inside_each_connector(self) -> None:
        manager = _manager("round_robin")
        _connector(manager, "a", 2, weight=1)
        _connector(manager, "b", 1, weight=1)
        picks = [(await manager.select_proxy_for_project("p")).id for _ in range(6)]
        assert Counter(p.split("-")[0] for p in picks) == {"a": 3, "b": 3}
        assert [p for p in picks if p.startswith("a")] == ["a-0", "a-1", "a-0"]

    async def test_connector_without_healthy_rows_takes_no_share(self) -> None:
        manager = _manager("random")
        _connector(manager, "a", 1, weight=50)
        _connector(manager, "b", 1, weight=1)
        manager._proxies["a-0"].status = ProxyStatus.UNHEALTHY
        for _ in range(20):
            assert (await manager.select_proxy_for_project("p")).connector_id == "b"

    async def test_disabled_connector_takes_no_share(self) -> None:
        manager = _manager("random")
        _connector(manager, "a", 1, weight=50, enabled=False)
        _connector(manager, "b", 1, weight=1)
        for _ in range(20):
            assert (await manager.select_proxy_for_project("p")).connector_id == "b"

    @pytest.mark.parametrize("strategy", ["random", "round_robin", "least_used", "sticky", "health_based"])
    async def test_single_connector_unchanged(self, strategy: str) -> None:
        manager = _manager(strategy)
        _connector(manager, "only", 3, weight=7)
        seen = {(await manager.select_proxy_for_project("p", session_id=f"s{i}")).id for i in range(30)}
        assert seen <= {"only-0", "only-1", "only-2"}
        assert seen
