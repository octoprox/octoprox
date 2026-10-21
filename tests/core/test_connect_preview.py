# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Connect page's view of routing: route_preview and served_places.

The api answers these from a router over a project-scoped compile; here the
sections come from :class:`FeedBuilder`, which compiles definitions the same
way, so the preview runs the gateway's real eligibility.
"""

import pytest
from pydantic import ValidationError

from api.core.config import Settings
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.routing.router import Router
from tests.routing.builder import FeedBuilder, healthy


@pytest.fixture
def feed() -> FeedBuilder:
    """Fixed exits in New York and Los Angeles, an "All countries" pool and a dynamic connector for US and DE."""
    feed = FeedBuilder()
    feed.add(
        Project(id="project-1", name="P", username="p", password="pw"),
        Credential(
            id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", config={},
        ),
        Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ),
        Connector(
            id="conn-static", name="ISP", credential_id="cred-static",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
            routing_config={"weight": 3},
        ),
        Connector(
            id="conn-pool", name="Pool", credential_id="cred-oxy", credential_type="oxylabs",
            project_id="project-1", config={"num_proxies": 2, "session_duration_minutes": 10},
        ),
        Connector(
            id="conn-dyn", name="Dynamic", credential_id="cred-oxy", credential_type="oxylabs",
            project_id="project-1",
            config={"session_mode": "dynamic", "session_duration_minutes": 10, "country_code": ["US", "DE"]},
        ),
    )
    places = {
        "px-ny": {"country": "US", "location": {"state_code": "NY", "city": "New York"}},
        "px-ny-2": {"country": "US", "location": {"state_code": "NY", "city": "New York"}},
        "px-la": {"country": "US", "location": {"state_code": "CA", "city": "Los Angeles"}},
        "px-fr": {"country": "FR"},
    }
    for proxy_id, metadata in places.items():
        feed.add(healthy(Proxy(id=proxy_id, host=proxy_id, port=1, connector_id="conn-static", metadata=metadata)))
    # A pooled vendor session: its exit moves, so its place is no hint.
    feed.add(healthy(Proxy(
        id="px-slot", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-aaa-sesstime-10", password="{password}", connector_id="conn-pool",
        metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": "aaa",
                  "country": "US", "location": {"state_code": "TX", "city": "Austin"}},
    )))
    feed.add(healthy(Proxy(
        id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}", connector_id="conn-dyn",
        metadata={
            "provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true",
        },
    )))
    return feed


def _routes(router: Router, location: LocationTarget | None) -> dict[str, tuple[int, bool, bool, float]]:
    return {
        r.connector_id: (r.proxies, r.dynamic, r.on_demand, round(r.share, 3))
        for r in router.route_preview("project-1", location)
    }


class TestRoutePreview:
    def test_untargeted_request_reaches_every_connector_by_weight(self, feed: FeedBuilder) -> None:
        assert _routes(feed.router(), None) == {
            "conn-static": (4, False, False, 0.6),
            "conn-pool": (1, False, False, 0.2),
            "conn-dyn": (1, True, False, 0.2),
        }

    def test_country_narrows_to_the_rows_that_serve_it(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _routes(router, LocationTarget(country="FR")) == {"conn-static": (1, False, False, 1.0)}
        assert _routes(router, LocationTarget(country="DE")) == {"conn-dyn": (1, True, False, 1.0)}

    def test_state_and_city_reach_fixed_exits_there_and_dynamic_gateways_that_render_them(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _routes(router, LocationTarget(country="US", state="NY", city="new_york")) == {
            "conn-static": (2, False, False, 0.75),
            "conn-dyn": (1, True, False, 0.25),
        }
        # Oxylabs names US states only, so a German Land reaches nothing.
        assert _routes(router, LocationTarget(country="DE", state="BY")) == {}

    def test_unserved_country_lists_the_pools_that_would_provision_it(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _routes(router, LocationTarget(country="JP")) == {"conn-pool": (0, False, True, 1.0)}
        # A city is never provisioned: nothing serves it.
        assert _routes(router, LocationTarget(country="JP", city="tokyo")) == {}

    def test_a_country_served_elsewhere_provisions_nothing(self, feed: FeedBuilder) -> None:
        # Selection asks for a group only when nothing in the project serves the country.
        assert "conn-pool" not in _routes(feed.router(), LocationTarget(country="FR"))

    def test_existing_group_is_not_listed_again(self, feed: FeedBuilder) -> None:
        feed.add(Proxy(
            id="px-jp", host="pr.oxylabs.io", port=7777, connector_id="conn-pool",
            metadata={"session_id": "jp1", "geo": "JP"}, status=ProxyStatus.UNHEALTHY,
        ))
        assert _routes(feed.router(), LocationTarget(country="JP")) == {}

    def test_disabled_connectors_are_left_out(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-dyn"].enabled = False
        assert "conn-dyn" not in _routes(feed.router(), None)

    def test_connectors_being_deleted_are_left_out(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-pool"].pending_deletion = True
        router = feed.router()
        assert "conn-pool" not in _routes(router, None)
        assert _routes(router, LocationTarget(country="JP")) == {}


class TestServedPlaces:
    def test_countries_any_country_and_fixed_exit_places(self, feed: FeedBuilder) -> None:
        countries, any_country, places = feed.router().served_places("project-1")
        assert countries == ["DE", "FR", "US"]
        assert any_country  # the pool lists no country, so it provisions any on demand
        assert [(p.country, p.state, p.city, p.proxies) for p in places] == [
            ("US", "NY", "new_york", 2),
            ("US", "CA", "los_angeles", 1),
        ]

    def test_dynamic_gateway_without_allow_list_serves_any_country(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-pool"].config["country_code"] = ["US"]
        assert not feed.router().served_places("project-1")[1]
        feed.connectors["conn-dyn"].config.pop("country_code")
        assert feed.router().served_places("project-1")[1]

    def test_limit_keeps_the_busiest_places(self, feed: FeedBuilder) -> None:
        assert [(p.state, p.proxies) for p in feed.router().served_places("project-1", limit=1)[2]] == [("NY", 2)]


class TestProxyPublicUrl:
    """server.proxy_public_url: http or https, a host and an optional port, nothing else."""

    @pytest.mark.parametrize(("value", "stored"), [
        ("https://proxy.example.com", "https://proxy.example.com"),
        (" https://proxy.example.com/ ", "https://proxy.example.com"),
        ("https://proxy.example.com:8443", "https://proxy.example.com:8443"),
        ("http://203.0.113.7:8080", "http://203.0.113.7:8080"),
        ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
    ])
    def test_accepted(self, value: str, stored: str) -> None:
        assert Settings(proxy_public_url=value).proxy_public_url == stored

    @pytest.mark.parametrize("value", [
        "proxy.example.com", "socks5://proxy.example.com", "https://", "https://proxy.example.com:0",
        "https://proxy.example.com:70000", "https://proxy.example.com/path", "https://proxy.example.com?x=1",
        "https://proxy.example.com#x", "https://user:pw@proxy.example.com", "https://proxy example.com",
        "https://proxy.example.com?", "https://proxy.example.com#",
    ])
    def test_refused(self, value: str) -> None:
        with pytest.raises(ValidationError):
            Settings(proxy_public_url=value)

    @staticmethod
    def _endpoint(**values: object) -> tuple[str, str | None, int | None]:
        # Every field given, so OCTOPROX_* in the environment cannot leak in.
        fields = {"proxy_public_url": "", "proxy_port": 8080}
        return Settings(**{**fields, **values}).proxy_endpoint  # type: ignore[arg-type]

    def test_endpoint_keeps_the_port_only_when_the_url_names_one(self) -> None:
        assert self._endpoint(proxy_public_url="https://Proxy.Example.com") == ("https", "proxy.example.com", None)
        assert self._endpoint(proxy_public_url="https://proxy.example.com:8443") == ("https", "proxy.example.com", 8443)
        assert self._endpoint(proxy_public_url="http://[2001:db8::1]:8080") == ("http", "2001:db8::1", 8080)

    def test_endpoint_without_a_url_is_the_web_uis_host_on_the_listeners_port(self) -> None:
        assert self._endpoint() == ("http", None, 8080)

    def test_the_superseded_host_and_port_settings_are_gone(self) -> None:
        """6.0 reads the URL only: the 4.6.6 pair is no longer a setting, so it cannot be read as one."""
        assert {"proxy_public_host", "proxy_public_port"}.isdisjoint(Settings.model_fields)
