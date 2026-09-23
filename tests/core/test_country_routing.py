# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for -cc- country routing: connector countries, proxy eligibility and on-demand slot groups."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.proxy_manager import ProxyManager
from api.core.proxy_server import ProxyServer
from api.db.redis import RedisClient
from api.models.connector import Connector, ProxyTarget, normalize_country_list
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


class TestConnectorCountries:
    """Tests for Connector.countries and normalize_country_list."""

    def _connector(self, **kwargs: object) -> Connector:
        return Connector(
            name="c", credential_id="cred", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p", **kwargs,  # type: ignore[arg-type]
        )

    def test_empty_when_nothing_declared(self) -> None:
        assert self._connector().countries == []
        assert self._connector(config={"country_code": ""}).countries == []
        assert self._connector(config={"countries": []}).countries == []

    def test_static_and_cloud_countries_list(self) -> None:
        assert self._connector(config={"countries": ["gb", "IE"]}).countries == ["GB", "IE"]

    def test_provider_country_code_single_or_list(self) -> None:
        assert self._connector(config={"country_code": "de"}).countries == ["DE"]
        assert self._connector(config={"country_code": ["US", "de"]}).countries == ["US", "DE"]

    def test_normalize_country_list(self) -> None:
        assert normalize_country_list("us, gb,,US") == ["US", "GB"]
        assert normalize_country_list(["de"]) == ["DE"]
        assert normalize_country_list(None) == []
        with pytest.raises(ValueError):
            normalize_country_list(["USA"])


class TestProxyCountry:
    def test_prefers_reported_country_over_geo(self) -> None:
        proxy = Proxy(host="h", port=1, connector_id="c", metadata={"country": "nl", "geo": "DE"})
        assert proxy.country == "NL"

    def test_falls_back_to_geo(self) -> None:
        assert Proxy(host="h", port=1, connector_id="c", metadata={"geo": "de"}).country == "DE"
        assert Proxy(host="h", port=1, connector_id="c").country is None


def _in_memory_manager() -> ProxyManager:
    """ProxyManager with mocked infrastructure; caches are populated directly."""
    mock_settings = MagicMock()
    mock_settings.default_strategy = "round_robin"
    mock_settings.instance_id = "test-instance"
    return ProxyManager(MagicMock(), MagicMock(), mock_settings)


def _healthy(proxy: Proxy) -> Proxy:
    proxy.status = ProxyStatus.HEALTHY
    return proxy


class TestEligibilityRules:
    """Which proxies a -cc- request may use."""

    @pytest.fixture
    def manager(self) -> ProxyManager:
        manager = _in_memory_manager()
        manager._projects["project-1"] = Project(id="project-1", name="P", username="p", password="pw")
        manager._credentials["cred-static"] = Credential(
            id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", config={},
        )
        # Static connector declaring GB+IE, proxies without per-proxy country.
        manager._connectors["conn-uk"] = Connector(
            id="conn-uk", name="UK list", credential_id="cred-static",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
            config={"countries": ["GB", "IE"]},
        )
        manager._proxies["px-uk-1"] = _healthy(Proxy(id="px-uk-1", host="uk1", port=1, connector_id="conn-uk"))
        # Static connector with no countries but per-proxy discovered countries.
        manager._connectors["conn-mixed"] = Connector(
            id="conn-mixed", name="Mixed", credential_id="cred-static",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
        )
        manager._proxies["px-fr"] = _healthy(Proxy(id="px-fr", host="fr", port=1, connector_id="conn-mixed", metadata={"country": "fr"}))
        manager._proxies["px-de"] = _healthy(Proxy(id="px-de", host="de", port=1, connector_id="conn-mixed", metadata={"country": "DE"}))
        manager._proxies["px-unknown"] = _healthy(Proxy(id="px-unknown", host="x", port=1, connector_id="conn-mixed"))
        # Connector declaring US whose one proxy was discovered in CA (vendor mismatch).
        manager._connectors["conn-us"] = Connector(
            id="conn-us", name="US", credential_id="cred-static",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
            config={"countries": ["US"]},
        )
        manager._proxies["px-us-ca"] = _healthy(Proxy(id="px-us-ca", host="ca", port=1, connector_id="conn-us", metadata={"country": "CA"}))
        return manager

    def _ids(self, manager: ProxyManager, country: str | None = None) -> set[str]:
        return {p.id for p in manager.get_routable_proxies_for_project("project-1", country=country)}

    def test_no_country_returns_everything(self, manager: ProxyManager) -> None:
        assert self._ids(manager) == {"px-uk-1", "px-fr", "px-de", "px-unknown", "px-us-ca"}

    def test_connector_list_covers_unlabelled_proxies(self, manager: ProxyManager) -> None:
        assert self._ids(manager, "GB") == {"px-uk-1"}
        assert self._ids(manager, "ie") == {"px-uk-1"}

    def test_per_proxy_country_matches_case_insensitively(self, manager: ProxyManager) -> None:
        assert self._ids(manager, "FR") == {"px-fr"}
        assert self._ids(manager, "de") == {"px-de"}

    def test_unlabelled_proxy_on_unlisted_connector_never_matches(self, manager: ProxyManager) -> None:
        assert "px-unknown" not in self._ids(manager, "FR")
        assert self._ids(manager, "JP") == set()
        assert not manager.are_all_proxies_quarantined("project-1", country="JP")

    def test_vendor_mismatch_serves_neither_country(self, manager: ProxyManager) -> None:
        # Declared US, discovered in CA: the reported country rules it out for US,
        # and the connector's list keeps it out of CA requests.
        assert self._ids(manager, "US") == set()
        assert self._ids(manager, "CA") == set()

    async def test_select_respects_country(self, manager: ProxyManager) -> None:
        for _ in range(3):
            selected = await manager.select_proxy_for_project("project-1", country="GB")
            assert selected is not None and selected.id == "px-uk-1"

    async def test_sticky_session_rebinds_when_country_changes(self, manager: ProxyManager) -> None:
        manager._redis_client.get_sticky_binding = AsyncMock(return_value=None)
        manager._redis_client.set_sticky_binding = AsyncMock()
        manager.set_project_strategy("project-1", "sticky")
        first = await manager.select_proxy_for_project("project-1", "sess-1", country="GB")
        second = await manager.select_proxy_for_project("project-1", "sess-1", country="FR")
        assert first is not None and first.id == "px-uk-1"
        assert second is not None and second.id == "px-fr"


class TestOnDemandCountryGroups:
    """"All countries" residential pools get a slot group per requested country."""

    @pytest.fixture
    def manager(self) -> ProxyManager:
        manager = _in_memory_manager()
        manager._projects["project-1"] = Project(id="project-1", name="P", username="p", password="pw")
        manager._credentials["cred-oxy"] = Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        )
        manager._connectors["conn-any"] = Connector(
            id="conn-any", name="Oxy any", credential_id="cred-oxy", credential_type="oxylabs",
            project_id="project-1", config={"num_proxies": 2, "session_duration_minutes": 10},
        )
        # Two existing non-geo slots, as the syncer would have created them.
        for sid in ("aaa", "bbb"):
            manager._proxies[f"px-{sid}"] = _healthy(Proxy(
                id=f"px-{sid}", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
                username=f"customer-alice-sessid-{sid}-sesstime-10", password="{password}",
                connector_id="conn-any",
                metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": sid},
            ))
        # Persisting and the cluster lease are infrastructure; keep the cache honest instead.
        async def fake_add(proxy: Proxy) -> None:
            manager._proxies[proxy.id] = proxy
        manager.add_proxy = fake_add  # type: ignore[method-assign]
        # Postgres mirrors the cache unless a test says otherwise.
        manager._fetch_connector_proxies = AsyncMock(side_effect=lambda cid: manager.get_proxies_for_connector(cid))  # type: ignore[method-assign]
        manager._redis_client.client.set = AsyncMock(return_value=True)
        manager._redis_client.client.eval = AsyncMock(return_value=1)
        manager._redis_client.client.delete = AsyncMock(return_value=1)
        return manager

    def test_pool_accepts_request_country(self, manager: ProxyManager) -> None:
        assert manager._accepts_request_country(manager._connectors["conn-any"])

    def test_pinned_pool_does_not(self, manager: ProxyManager) -> None:
        manager._connectors["conn-any"].config["country_code"] = ["US"]
        assert not manager._accepts_request_country(manager._connectors["conn-any"])

    async def test_first_request_provisions_group_and_selects_from_it(self, manager: ProxyManager) -> None:
        assert manager.get_routable_proxies_for_project("project-1", country="DE") == []
        selected = await manager.select_proxy_for_project("project-1", country="de")
        assert selected is not None
        assert selected.metadata["geo"] == "DE"
        assert selected.username.startswith("customer-alice-cc-DE-sessid-")
        assert selected.username.endswith("-sesstime-10")
        assert selected.password == "s3cret"
        de_group = [p for p in manager._proxies.values() if p.metadata.get("geo") == "DE"]
        assert len(de_group) == 2  # num_proxies is per country
        assert all(p.status == ProxyStatus.HEALTHY for p in de_group)

    async def test_group_is_created_once(self, manager: ProxyManager) -> None:
        await manager.select_proxy_for_project("project-1", country="DE")
        await manager.select_proxy_for_project("project-1", country="DE")
        assert len([p for p in manager._proxies.values() if p.metadata.get("geo") == "DE"]) == 2

    async def test_requests_without_country_skip_geo_groups(self, manager: ProxyManager) -> None:
        await manager.select_proxy_for_project("project-1", country="DE")
        ids = {p.id for p in manager.get_routable_proxies_for_project("project-1")}
        assert ids == {"px-aaa", "px-bbb"}

    async def test_pool_health_counts_on_demand_groups(self, manager: ProxyManager) -> None:
        """Dashboard health is not routing: the DE group is healthy and must not read as unhealthy."""
        await manager.select_proxy_for_project("project-1", country="DE")
        all_proxies = manager.get_proxies_for_project("project-1")
        healthy = manager.get_healthy_proxies_for_project("project-1")
        assert len(all_proxies) == 4  # 2 base + 2 on demand
        assert {p.id for p in healthy} == {p.id for p in all_proxies}

    async def test_geo_group_serves_only_its_country(self, manager: ProxyManager) -> None:
        await manager.select_proxy_for_project("project-1", country="DE")
        assert manager.get_routable_proxies_for_project("project-1", country="FR") == []

    async def test_group_provisioned_by_a_peer_is_adopted_not_duplicated(self, manager: ProxyManager) -> None:
        """Cluster: a peer created the DE group and released the lease before our cache heard about it."""
        peer_slots = [
            _healthy(Proxy(
                id=f"peer-{i}", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
                username=f"customer-alice-cc-DE-sessid-peer{i}-sesstime-10", password="{password}",
                connector_id="conn-any",
                metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": f"peer{i}", "geo": "DE"},
            ))
            for i in range(2)
        ]
        in_db = {p.id: p for p in [*manager.get_proxies_for_connector("conn-any"), *peer_slots]}
        manager._fetch_connector_proxies = AsyncMock(return_value=list(in_db.values()))  # type: ignore[method-assign]
        manager.reload_proxy = AsyncMock(side_effect=lambda pid: manager._proxies.__setitem__(pid, in_db[pid]))  # type: ignore[method-assign]

        selected = await manager.select_proxy_for_project("project-1", country="DE")

        assert selected is not None and selected.id in {"peer-0", "peer-1"}
        de_group = [p for p in manager._proxies.values() if p.metadata.get("geo") == "DE"]
        assert sorted(p.id for p in de_group) == ["peer-0", "peer-1"]  # adopted, not doubled

    async def test_lease_lost_waits_and_provisions_nothing(self, manager: ProxyManager) -> None:
        manager._redis_client.client.set = AsyncMock(return_value=False)
        selected = await manager.select_proxy_for_project("project-1", country="DE")
        assert selected is None
        assert not any(p.metadata.get("geo") for p in manager._proxies.values())


class TestProxyServerCountryAuth:
    """Tests for country extraction in ProxyServer._authenticate_project."""

    @staticmethod
    def _server(project: Project) -> ProxyServer:
        manager = MagicMock()
        manager.get_project_by_username.side_effect = (
            lambda username: project if username == project.username else None
        )
        return ProxyServer(manager)

    @staticmethod
    def _headers(username: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"proxy-authorization": f"Basic {token}"}

    def test_country_and_sessid_extracted(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        result = self._server(project)._authenticate_project(self._headers("geo-cc-us-sessid-abc", "pw"))
        assert result is not None
        assert result.project.id == project.id
        assert result.sessid == "abc"
        assert result.country == "US"

    def test_country_alone(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        result = self._server(project)._authenticate_project(self._headers("geo-cc-de", "pw"))
        assert result is not None
        assert result.sessid is None
        assert result.country == "DE"

    def test_wrong_password_with_country_fails(self) -> None:
        project = Project(name="P", username="geo", password="pw")
        assert self._server(project)._authenticate_project(self._headers("geo-cc-de", "nope")) is None

    def test_no_proxy_message_mentions_country(self) -> None:
        assert "country US" in ProxyServer._no_proxy_message("US")
        assert "country" not in ProxyServer._no_proxy_message(None)


class TestDatabaseBackedCountryRouting:
    """End-to-end through the persisted ProxyManager for static connectors."""

    @pytest.fixture
    async def proxy_manager(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        db_session: AsyncSession,  # Required to ensure tables are cleaned up
    ) -> ProxyManager:
        manager = ProxyManager(session_factory=db_session_factory, redis_client=redis_client, settings=test_settings)
        await manager._load_from_database()
        await manager._hydrate_from_redis()
        return manager

    async def test_countries_persist_and_filter(self, proxy_manager: ProxyManager) -> None:
        project = Project(name="Geo", username="geo", password="pass")
        await proxy_manager.add_project(project)
        credential = Credential(name="Geo Cred", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={})
        await proxy_manager.add_credential(credential)
        us = Connector(
            name="us", credential_id=credential.id, credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id, config={"countries": ["US"]}, routing_config={"domain_whitelist": ["allowed.com"]},
        )
        anywhere = Connector(
            name="any", credential_id=credential.id, credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
        )
        await proxy_manager.add_connector(us)
        await proxy_manager.add_connector(anywhere)
        px_us = Proxy(host="us.example.com", port=8080, protocol=ProxyProtocol.HTTP, connector_id=us.id)
        px_any = Proxy(host="any.example.com", port=8080, protocol=ProxyProtocol.HTTP, connector_id=anywhere.id)
        for p in (px_us, px_any):
            p.status = ProxyStatus.HEALTHY
            await proxy_manager.add_proxy(p)

        assert {p.id for p in proxy_manager.get_healthy_proxies_for_project(project.id)} == {px_us.id, px_any.id}
        assert [p.id for p in proxy_manager.get_routable_proxies_for_project(project.id, country="US")] == [px_us.id]
        assert proxy_manager.get_routable_proxies_for_project(project.id, target_host="blocked.com", country="US") == []
        assert proxy_manager.get_routable_proxies_for_project(project.id, country="DE") == []
        selected = await proxy_manager.select_proxy_for_project(project.id, target_host="allowed.com", country="us")
        assert selected is not None and selected.id == px_us.id


class TestDynamicSessions:
    """A dynamic-sessions connector: one gateway row, credentials rendered per request."""

    @pytest.fixture
    def manager(self) -> ProxyManager:
        manager = _in_memory_manager()
        manager._projects["project-1"] = Project(id="project-1", name="P", username="p", password="pw")
        manager._credentials["cred-oxy"] = Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        )
        manager._connectors["conn-dyn"] = Connector(
            id="conn-dyn", name="Oxy dynamic", credential_id="cred-oxy", credential_type="oxylabs",
            project_id="project-1", config={"session_mode": "dynamic", "session_duration_minutes": 10},
        )
        manager._proxies["gw"] = _healthy(Proxy(
            id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
            username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}",
            connector_id="conn-dyn",
            metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true"},
        ))
        return manager

    def test_gateway_serves_any_country(self, manager: ProxyManager) -> None:
        assert [p.id for p in manager.get_routable_proxies_for_project("project-1")] == ["gw"]
        assert [p.id for p in manager.get_routable_proxies_for_project("project-1", country="DE")] == ["gw"]
        assert not manager._accepts_request_country(manager._connectors["conn-dyn"])
        assert manager.get_connector_target(manager._connectors["conn-dyn"]) == ProxyTarget(
            total=1, dynamic=True, exit_sample_percent=5
        )

    def test_allow_list_filters_at_the_connector(self, manager: ProxyManager) -> None:
        manager._connectors["conn-dyn"].config["country_code"] = ["US", "DE"]
        assert [p.id for p in manager.get_routable_proxies_for_project("project-1", country="de")] == ["gw"]
        assert manager.get_routable_proxies_for_project("project-1", country="FR") == []
        assert [p.id for p in manager.get_routable_proxies_for_project("project-1")] == ["gw"]

    async def test_explicit_session_is_derived_and_stable(self, manager: ProxyManager) -> None:
        first = await manager.select_proxy_for_project("project-1", "order-1", sessid="order-1", country="de")
        second = await manager.select_proxy_for_project("project-1", "order-1", sessid="order-1", country="de")
        assert first is not None and second is not None
        assert first.id == "gw" and first.username == second.username
        assert first.username.startswith("customer-alice-cc-DE-sessid-") and first.username.endswith("-sesstime-10")
        assert "order-1" not in first.username
        assert first.password == "s3cret"  # resolved after rendering
        assert first.metadata["session_id"] in first.username and first.metadata["geo"] == "DE"
        assert manager._proxies["gw"].username == "customer-alice-sessid-probe0probe0-sesstime-10"  # stored row untouched

    async def test_client_ip_fallback_does_not_pin_a_vendor_session(self, manager: ProxyManager) -> None:
        # session_id carries the client address for sticky routing; sessid is None, so the vendor rotates.
        first = await manager.select_proxy_for_project("project-1", "10.0.0.7", sessid=None)
        second = await manager.select_proxy_for_project("project-1", "10.0.0.7", sessid=None)
        assert first is not None and second is not None
        assert first.username != second.username
        assert "session_id" not in first.metadata and "-cc-" not in first.username
