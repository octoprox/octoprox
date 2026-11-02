# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Reserved and excluded countries: which connectors a request for a country may reach, and what keeps off them.

A country reserved by some connectors goes to those alone, split by weight, and fails when none of them can serve it;
an excluded country is never served by the connector excluding it.
"""

from collections.abc import Callable
from datetime import timedelta

import pytest

from api.core import utc_now
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from tests.routing.builder import FeedBuilder, healthy

GB = LocationTarget(country="GB")
US = LocationTarget(country="US")


def _ids(router: Router, location: LocationTarget | None = None, host: str | None = None) -> set[str]:
    return {p.id for p in router.get_routable_proxies_for_project("project-1", target_host=host, location=location)}


def _static(connector_id: str, name: str, **fields: object) -> Connector:
    return Connector(
        id=connector_id, name=name, credential_id="cred-static",
        credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", **fields,  # type: ignore[arg-type]
    )


def _oxylabs(connector_id: str, name: str, config: dict[str, object], **fields: object) -> Connector:
    return Connector(
        id=connector_id, name=name, credential_id="cred-oxy", credential_type="oxylabs",
        project_id="project-1", config=config, **fields,  # type: ignore[arg-type]
    )


def _gateway_row(proxy_id: str, connector_id: str) -> Proxy:
    """The one row of an Oxylabs dynamic-sessions connector, as the sync stores it."""
    return healthy(Proxy(
        id=proxy_id, host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}", connector_id=connector_id,
        metadata={
            "provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true",
        },
    ))


DYNAMIC = {"session_mode": "dynamic", "session_duration_minutes": 10}


@pytest.fixture
def feed() -> FeedBuilder:
    """A GB connector reserving GB, a static pool with exits in GB and US, and an "All countries" dynamic gateway."""
    feed = FeedBuilder()
    feed.add(
        Project(id="project-1", name="P", username="p", password="pw"),
        Credential(id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1"),
        Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ),
        _static("conn-gb", "GB pool", config={"countries": ["GB"]}, routing_config={"reserved_countries": ["GB"]}),
        healthy(Proxy(id="px-gb", host="gb", port=1, connector_id="conn-gb")),
        _static("conn-mixed", "Mixed"),
        healthy(Proxy(id="px-mixed-gb", host="mgb", port=1, connector_id="conn-mixed", metadata={"country": "GB"})),
        healthy(Proxy(id="px-mixed-us", host="mus", port=1, connector_id="conn-mixed", metadata={"country": "US"})),
        _oxylabs("conn-dyn", "Oxy dynamic", DYNAMIC),
        _gateway_row("gw", "conn-dyn"),
    )
    return feed


class TestReservedCountry:
    def test_a_reserved_country_goes_only_to_the_connectors_reserving_it(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _ids(router, GB) == {"px-gb"}
        assert [route.connector_id for route in router.route_preview("project-1", GB)] == ["conn-gb"]
        # Other countries are untouched, and the reserving connector serves none of them.
        assert _ids(router, US) == {"px-mixed-us", "gw"}

    def test_untargeted_traffic_keeps_off_the_reserving_connectors_reserved_exits(self, feed: FeedBuilder) -> None:
        # conn-gb lists only GB, which it reserves: requests naming no country never reach it. Another connector's
        # exit in GB still serves them, since that connector reserves nothing.
        assert _ids(feed.router()) == {"px-mixed-gb", "px-mixed-us", "gw"}

    async def test_a_pinned_request_may_still_use_a_reserved_exit(self, feed: FeedBuilder) -> None:
        router = feed.router()
        pinned = await router.select_proxy_for_project("project-1", pinned_proxy_id="px-gb")
        assert pinned is not None and pinned.id == "px-gb"
        # A pinned exit outside the reservation does not serve the reserved country.
        assert await router.select_proxy_for_project("project-1", location=GB, pinned_proxy_id="px-mixed-gb") is None

    async def test_a_disabled_reserving_connector_fails_the_country(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-gb"].enabled = False
        router = feed.router()
        assert _ids(router, GB) == set()
        assert router.route_preview("project-1", GB) == []
        assert await router.select_proxy_for_project("project-1", location=GB) is None
        assert not await router.are_all_proxies_quarantined("project-1", location=GB)
        assert router.traffic_limit_status("project-1", location=GB) is None
        assert router.reserved_by("project-1", None, GB) == ["GB pool"]
        assert router.reserved_by("project-1", None, US) == []

    async def test_a_quarantined_reservation_is_a_quarantine(self, feed: FeedBuilder) -> None:
        feed.quarantined["px-gb"] = utc_now() + timedelta(minutes=5)
        router = feed.router()
        assert await router.select_proxy_for_project("project-1", location=GB) is None
        assert await router.are_all_proxies_quarantined("project-1", location=GB)

    def test_a_reservation_over_its_traffic_limit_answers_its_limit(self, feed: FeedBuilder) -> None:
        feed.blocked_until["conn-gb"] = utc_now() + timedelta(hours=1)
        router = feed.router()
        assert _ids(router, GB) == set()
        assert router.traffic_limit_status("project-1", location=GB) == 509

    def test_a_deleted_connector_reserves_nothing(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-gb"].enabled = False
        feed.connectors["conn-gb"].pending_deletion = True
        assert _ids(feed.router(), GB) == {"px-mixed-gb", "gw"}

    def test_connectors_reserving_one_country_split_it_by_weight(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-dyn"].routing_config = {"reserved_countries": ["GB"], "weight": 3}
        router = feed.router()
        assert _ids(router, GB) == {"px-gb", "gw"}
        shares = {route.connector_id: route.share for route in router.route_preview("project-1", GB)}
        assert shares == {"conn-gb": 0.25, "conn-dyn": 0.75}

    def test_the_connect_hints_leave_out_what_a_reservation_holds_elsewhere(self, feed: FeedBuilder) -> None:
        feed.proxies["px-mixed-gb"].metadata["city"] = "london"
        countries, _any, places = feed.router().served_places("project-1")
        assert "GB" in countries and [place.country for place in places] == []
        # With the reserving connector disabled, nothing serves GB: it is no longer hinted.
        feed.connectors["conn-gb"].enabled = False
        assert "GB" not in feed.router().served_places("project-1")[0]

    def test_hosts_the_reservation_refuses_go_to_the_others(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-gb"].routing_config = {"reserved_countries": ["GB"], "domain_whitelist": ["bing.com"]}
        router = feed.router()
        assert _ids(router, GB, host="www.bing.com") == {"px-gb"}
        assert _ids(router, GB, host="google.com") == {"px-mixed-gb", "gw"}
        assert router.reserved_by("project-1", "google.com", GB) == []


class TestReservedDynamicCountries:
    """A dynamic gateway listing countries renders requests naming none in its unreserved countries only."""

    @pytest.fixture
    def router_for(self, feed: FeedBuilder) -> Callable[[list[str]], Router]:
        def build(reserved: list[str]) -> Router:
            feed.connectors.pop("conn-gb")
            feed.connectors.pop("conn-mixed")
            feed.connectors["conn-dyn"].config = {**DYNAMIC, "country_code": ["GB", "US"]}
            feed.connectors["conn-dyn"].routing_config = {"reserved_countries": reserved}
            return feed.router()

        return build

    async def test_requests_naming_no_country_render_an_unreserved_one(
        self, router_for: Callable[[list[str]], Router]
    ) -> None:
        router = router_for(["GB"])
        for session in ("a", "b", "c", "d"):
            selected = await router.select_proxy_for_project("project-1", session)
            assert selected is not None and selected.id == "gw" and selected.metadata["geo"] == "US"
        reserved = await router.select_proxy_for_project("project-1", "a", location=GB)
        assert reserved is not None and reserved.metadata["geo"] == "GB"

    def test_a_gateway_with_every_country_reserved_takes_no_untargeted_traffic(
        self, router_for: Callable[[list[str]], Router]
    ) -> None:
        router = router_for(["GB", "US"])
        assert "gw" not in _ids(router)
        assert _ids(router, GB) == {"gw"}


class TestExcludedCountry:
    def test_an_excluded_country_is_never_served_by_the_connector(self, feed: FeedBuilder) -> None:
        feed.connectors.pop("conn-gb")
        feed.connectors["conn-dyn"].routing_config = {"excluded_countries": ["GB"]}
        feed.connectors["conn-mixed"].routing_config = {"excluded_countries": ["GB"]}
        router = feed.router()
        assert _ids(router, GB) == set()
        assert _ids(router, US) == {"px-mixed-us", "gw"}
        # The exit known to be in GB takes no traffic at all.
        assert _ids(router) == {"px-mixed-us", "gw"}

    def test_an_all_countries_pool_does_not_provision_an_excluded_country(self, feed: FeedBuilder) -> None:
        feed.connectors.pop("conn-gb")
        feed.connectors.pop("conn-dyn")
        feed.add(_oxylabs("conn-pool", "Oxy pool", {"num_proxies": 1, "session_duration_minutes": 10}))
        router = feed.router()
        assert [route.connector_id for route in router.route_preview("project-1", LocationTarget(country="FR"))] == [
            "conn-pool"
        ]
        feed.connectors["conn-pool"].routing_config = {"excluded_countries": ["FR"]}
        assert feed.router().route_preview("project-1", LocationTarget(country="FR")) == []

    def test_only_reserving_pools_provision_a_reserved_country(self, feed: FeedBuilder) -> None:
        feed.connectors.pop("conn-gb")
        feed.connectors.pop("conn-dyn")
        feed.add(
            _oxylabs("conn-pool", "Oxy pool", {"num_proxies": 1, "session_duration_minutes": 10}),
            _oxylabs(
                "conn-fr", "Oxy FR", {"num_proxies": 1, "session_duration_minutes": 10},
                routing_config={"reserved_countries": ["FR"]},
            ),
        )
        routes = feed.router().route_preview("project-1", LocationTarget(country="FR"))
        assert [(route.connector_id, route.on_demand) for route in routes] == [("conn-fr", True)]


def test_countries_in_play_cover_lists_reservations_exclusions_and_exits(feed: FeedBuilder) -> None:
    feed.connectors["conn-dyn"].routing_config = {"excluded_countries": ["FR"]}
    snapshot: RoutingSnapshot = feed.snapshot()
    assert Router(snapshot).countries_in_play("project-1") == ["FR", "GB", "US"]


@pytest.mark.parametrize(
    ("proxy_type", "config", "provisions"),
    [
        # Slot groups per listed country.
        ("residential", {"num_proxies": 1, "session_duration_minutes": 10}, True),
        # Discovery keeps IPs per listed country.
        ("datacenter", {"num_proxies": 1}, True),
        # The country is rendered into each request: nothing is held per country.
        ("residential", DYNAMIC, False),
    ],
)
def test_which_providers_hold_proxies_per_country(proxy_type: str, config: dict[str, object], provisions: bool) -> None:
    feed = FeedBuilder().add(
        Project(id="project-1", name="P", username="p", password="pw"),
        Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": proxy_type, "username": "alice", "password": "s3cret"},
        ),
        _oxylabs("conn", "Oxy", config),
    )
    view = feed.snapshot().connector_view("conn")
    assert view is not None and view.provider is not None
    assert view.provider.provisions_per_country is provisions
