# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the indexed proxy and connector caches."""

from api.core.entity_index import ConnectorIndex, ProxyIndex
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.proxy import Proxy


def _proxy(pid: str, connector_id: str) -> Proxy:
    return Proxy(id=pid, host=f"{pid}.example.com", port=8080, connector_id=connector_id)


def _connector(cid: str, project_id: str) -> Connector:
    return Connector(id=cid, name=cid, credential_id="cred", credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project_id)


class TestProxyIndex:
    def test_behaves_like_a_dict(self) -> None:
        index = ProxyIndex()
        index["a"] = _proxy("a", "c1")
        index.update({"b": _proxy("b", "c1"), "c": _proxy("c", "c2")})
        assert len(index) == 3 and "a" in index and "zzz" not in index
        assert index.get("zzz") is None and index["a"].id == "a"
        assert list(index) == ["a", "b", "c"]
        assert index.pop("b").id == "b"
        assert index.pop("b", None) is None
        assert [p.id for p in index.values()] == ["a", "c"]

    def test_groups_by_connector(self) -> None:
        index = ProxyIndex([(f"p{i}", _proxy(f"p{i}", "c1" if i % 2 else "c2")) for i in range(6)])
        assert sorted(p.id for p in index.for_connector("c1")) == ["p1", "p3", "p5"]
        assert sorted(p.id for p in index.for_connectors(["c1", "c2", "missing"])) == [f"p{i}" for i in range(6)]
        assert index.for_connector("missing") == []

    def test_reassignment_moves_between_groups(self) -> None:
        index = ProxyIndex()
        index["a"] = _proxy("a", "c1")
        index["a"] = _proxy("a", "c2")
        assert index.for_connector("c1") == [] and [p.id for p in index.for_connector("c2")] == ["a"]

    def test_delete_and_remove_groups(self) -> None:
        index = ProxyIndex([("a", _proxy("a", "c1")), ("b", _proxy("b", "c1")), ("c", _proxy("c", "c2"))])
        del index["a"]
        assert [p.id for p in index.for_connector("c1")] == ["b"]
        assert sorted(index.remove_groups(["c1", "nope"])) == ["b"]
        assert list(index) == ["c"] and index.for_connector("c1") == []

    def test_reindex_after_in_place_change(self) -> None:
        index = ProxyIndex([("a", _proxy("a", "c1"))])
        index["a"].connector_id = "c2"
        assert [p.id for p in index.for_connector("c1")] == ["a"]  # stale until re-filed
        index.reindex("a")
        assert index.for_connector("c1") == [] and [p.id for p in index.for_connector("c2")] == ["a"]


class TestConnectorIndex:
    def test_groups_by_project(self) -> None:
        index = ConnectorIndex([("k1", _connector("k1", "p1")), ("k2", _connector("k2", "p2")), ("k3", _connector("k3", "p1"))])
        assert sorted(c.id for c in index.for_project("p1")) == ["k1", "k3"]
        index.pop("k1")
        assert [c.id for c in index.for_project("p1")] == ["k3"]
        assert index.for_project("p3") == []
