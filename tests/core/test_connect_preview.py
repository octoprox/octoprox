# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Connect page's view of routing: route_preview and served_places."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from api.core.config import Settings
from api.core.proxy_manager import ProxyManager
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


def _healthy(proxy: Proxy) -> Proxy:
    proxy.status = ProxyStatus.HEALTHY
    return proxy


@pytest.fixture
def manager() -> ProxyManager:
    """Fixed exits in New York and Los Angeles, an "All countries" pool and a dynamic connector for US and DE."""
    settings = MagicMock()
    settings.default_strategy = "round_robin"
    settings.instance_id = "test-instance"
    manager = ProxyManager(MagicMock(), MagicMock(), settings)
    manager._projects["project-1"] = Project(id="project-1", name="P", username="p", password="pw")
    manager._credentials["cred-static"] = Credential(
        id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", config={},
    )
    manager._credentials["cred-oxy"] = Credential(
        id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
        config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
    )
    manager._connectors["conn-static"] = Connector(
        id="conn-static", name="ISP", credential_id="cred-static",
        credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
        routing_config={"weight": 3},
    )
    manager._connectors["conn-pool"] = Connector(
        id="conn-pool", name="Pool", credential_id="cred-oxy", credential_type="oxylabs",
        project_id="project-1", config={"num_proxies": 2, "session_duration_minutes": 10},
    )
    manager._connectors["conn-dyn"] = Connector(
        id="conn-dyn", name="Dynamic", credential_id="cred-oxy", credential_type="oxylabs",
        project_id="project-1",
        config={"session_mode": "dynamic", "session_duration_minutes": 10, "country_code": ["US", "DE"]},
    )
    places = {
        "px-ny": {"country": "US", "location": {"state_code": "NY", "city": "New York"}},
        "px-ny-2": {"country": "US", "location": {"state_code": "NY", "city": "New York"}},
        "px-la": {"country": "US", "location": {"state_code": "CA", "city": "Los Angeles"}},
        "px-fr": {"country": "FR"},
    }
    for proxy_id, metadata in places.items():
        manager._proxies[proxy_id] = _healthy(Proxy(id=proxy_id, host=proxy_id, port=1, connector_id="conn-static", metadata=metadata))
    # A pooled vendor session: its exit moves, so its place is no hint.
    manager._proxies["px-slot"] = _healthy(Proxy(
        id="px-slot", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-aaa-sesstime-10", password="{password}", connector_id="conn-pool",
        metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": "aaa",
                  "country": "US", "location": {"state_code": "TX", "city": "Austin"}},
    ))
    manager._proxies["gw"] = _healthy(Proxy(
        id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}", connector_id="conn-dyn",
        metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true"},
    ))
    return manager


def _routes(manager: ProxyManager, location: LocationTarget | None) -> dict[str, tuple[int, bool, bool, float]]:
    return {
        r.connector_id: (r.proxies, r.dynamic, r.on_demand, round(r.share, 3))
        for r in manager.route_preview("project-1", location)
    }


class TestRoutePreview:
    def test_untargeted_request_reaches_every_connector_by_weight(self, manager: ProxyManager) -> None:
        assert _routes(manager, None) == {
            "conn-static": (4, False, False, 0.6),
            "conn-pool": (1, False, False, 0.2),
            "conn-dyn": (1, True, False, 0.2),
        }

    def test_country_narrows_to_the_rows_that_serve_it(self, manager: ProxyManager) -> None:
        assert _routes(manager, LocationTarget(country="FR")) == {"conn-static": (1, False, False, 1.0)}
        assert _routes(manager, LocationTarget(country="DE")) == {"conn-dyn": (1, True, False, 1.0)}

    def test_state_and_city_reach_fixed_exits_there_and_dynamic_gateways_that_render_them(self, manager: ProxyManager) -> None:
        assert _routes(manager, LocationTarget(country="US", state="NY", city="new_york")) == {
            "conn-static": (2, False, False, 0.75),
            "conn-dyn": (1, True, False, 0.25),
        }
        # Oxylabs names US states only, so a German Land reaches nothing.
        assert _routes(manager, LocationTarget(country="DE", state="BY")) == {}

    def test_unserved_country_lists_the_pools_that_would_provision_it(self, manager: ProxyManager) -> None:
        assert _routes(manager, LocationTarget(country="JP")) == {"conn-pool": (0, False, True, 1.0)}
        # A city is never provisioned: nothing serves it.
        assert _routes(manager, LocationTarget(country="JP", city="tokyo")) == {}

    def test_a_country_served_elsewhere_provisions_nothing(self, manager: ProxyManager) -> None:
        # select_proxy_for_project provisions only when nothing in the project serves the country.
        assert "conn-pool" not in _routes(manager, LocationTarget(country="FR"))

    def test_existing_group_is_not_listed_again(self, manager: ProxyManager) -> None:
        manager._proxies["px-jp"] = Proxy(
            id="px-jp", host="pr.oxylabs.io", port=7777, connector_id="conn-pool",
            metadata={"session_id": "jp1", "geo": "JP"}, status=ProxyStatus.UNHEALTHY,
        )
        assert _routes(manager, LocationTarget(country="JP")) == {}

    def test_disabled_connectors_are_left_out(self, manager: ProxyManager) -> None:
        manager._connectors["conn-dyn"].enabled = False
        assert "conn-dyn" not in _routes(manager, None)


class TestServedPlaces:
    def test_countries_any_country_and_fixed_exit_places(self, manager: ProxyManager) -> None:
        countries, any_country, places = manager.served_places("project-1")
        assert countries == ["DE", "FR", "US"]
        assert any_country  # the pool lists no country, so it provisions any on demand
        assert [(p.country, p.state, p.city, p.proxies) for p in places] == [
            ("US", "NY", "new_york", 2),
            ("US", "CA", "los_angeles", 1),
        ]

    def test_dynamic_gateway_without_allow_list_serves_any_country(self, manager: ProxyManager) -> None:
        manager._connectors["conn-pool"].config["country_code"] = ["US"]
        assert not manager.served_places("project-1")[1]
        manager._connectors["conn-dyn"].config.pop("country_code")
        assert manager.served_places("project-1")[1]

    def test_limit_keeps_the_busiest_places(self, manager: ProxyManager) -> None:
        assert [(p.state, p.proxies) for p in manager.served_places("project-1", limit=1)[2]] == [("NY", 2)]


class TestProxyPublicHost:
    """server.proxy_public_host is a bare host: a port or scheme in it would be doubled in every URL."""

    def test_bare_hosts_and_ip_literals(self) -> None:
        assert Settings(proxy_public_host=" proxy.example.com ").proxy_public_host == "proxy.example.com"
        assert Settings(proxy_public_host="203.0.113.7").proxy_public_host == "203.0.113.7"
        assert Settings(proxy_public_host="[2001:db8::1]").proxy_public_host == "2001:db8::1"
        # The field default, not Settings(): that would read OCTOPROX_PROXY_PUBLIC_HOST from the environment.
        assert Settings.model_fields["proxy_public_host"].default == ""

    @pytest.mark.parametrize("value", ["proxy.example.com:8080", "http://proxy.example.com", "user@proxy", "proxy.example.com#x"])
    def test_port_scheme_or_userinfo_is_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            Settings(proxy_public_host=value)
