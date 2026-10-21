# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Location routing on a gateway: -cc- eligibility, dynamic sessions, -st-/-city- places and on-demand groups.

The router routes on snapshots :class:`FeedBuilder` compiles as the controller
does, and on-demand country groups are tested on the router's side only, with
a fake that provisions a group as the provision job does and applies the next
snapshot as the follower would.
"""

import asyncio
import time
from collections.abc import Callable

import pytest

from api.geo.models import UnknownAction
from api.models.connector import Connector, ProxyTarget
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.routing import router as router_module
from api.routing.router import ON_DEMAND_WAIT_SECONDS, Router
from api.routing.snapshot import RoutingSnapshot
from tests.routing.builder import FeedBuilder, healthy
from tests.waits import until


def _ids(router: Router, location: LocationTarget | None = None) -> set[str]:
    return {p.id for p in router.get_routable_proxies_for_project("project-1", location=location)}


def _static_credential() -> Credential:
    return Credential(
        id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", config={},
    )


def _oxylabs_credential() -> Credential:
    return Credential(
        id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
        config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
    )


def _gateway_row() -> Proxy:
    """The one row of an Oxylabs dynamic-sessions connector, as the sync stores it."""
    return healthy(Proxy(
        id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
        username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}",
        connector_id="conn-dyn",
        metadata={
            "provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true",
        },
    ))


class TestEligibilityRules:
    """Which proxies a -cc- request may use."""

    @pytest.fixture
    def feed(self) -> FeedBuilder:
        feed = FeedBuilder()
        feed.add(Project(id="project-1", name="P", username="p", password="pw"), _static_credential())
        # Static connector declaring GB+IE, proxies without per-proxy country.
        feed.add(
            Connector(
                id="conn-uk", name="UK list", credential_id="cred-static",
                credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
                config={"countries": ["GB", "IE"]},
            ),
            healthy(Proxy(id="px-uk-1", host="uk1", port=1, connector_id="conn-uk")),
        )
        # Static connector with no countries but per-proxy discovered countries.
        feed.add(
            Connector(
                id="conn-mixed", name="Mixed", credential_id="cred-static",
                credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
            ),
            healthy(Proxy(id="px-fr", host="fr", port=1, connector_id="conn-mixed", metadata={"country": "fr"})),
            healthy(Proxy(id="px-de", host="de", port=1, connector_id="conn-mixed", metadata={"country": "DE"})),
            healthy(Proxy(id="px-unknown", host="x", port=1, connector_id="conn-mixed")),
        )
        # Connector declaring US whose one proxy was discovered in CA (vendor mismatch).
        feed.add(
            Connector(
                id="conn-us", name="US", credential_id="cred-static",
                credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
                config={"countries": ["US"]},
            ),
            healthy(Proxy(id="px-us-ca", host="ca", port=1, connector_id="conn-us", metadata={"country": "CA"})),
        )
        return feed

    def test_no_country_returns_everything(self, feed: FeedBuilder) -> None:
        router = feed.router()
        # px-us-ca made no vendor claim, so the connector's list binds it only for a named country:
        # it serves untargeted traffic, and never -cc-us, since its resolved country is CA.
        assert _ids(router) == {"px-uk-1", "px-fr", "px-de", "px-unknown", "px-us-ca"}
        assert "px-us-ca" not in _ids(router, LocationTarget(country="US"))

    def test_connector_list_covers_unlabelled_proxies(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _ids(router, LocationTarget(country="GB")) == {"px-uk-1"}
        assert _ids(router, LocationTarget(country="IE")) == {"px-uk-1"}

    def test_per_proxy_country_matches_case_insensitively(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _ids(router, LocationTarget(country="FR")) == {"px-fr"}
        assert _ids(router, LocationTarget(country="DE")) == {"px-de"}

    async def test_unlabelled_proxy_on_unlisted_connector_never_matches(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert "px-unknown" not in _ids(router, LocationTarget(country="FR"))
        assert _ids(router, LocationTarget(country="JP")) == set()
        assert not await router.are_all_proxies_quarantined("project-1", location=LocationTarget(country="JP"))

    def test_vendor_mismatch_serves_neither_country(self, feed: FeedBuilder) -> None:
        router = feed.router()
        # Declared US, discovered in CA: the reported country rules it out for US,
        # and the connector's list keeps it out of CA requests.
        assert _ids(router, LocationTarget(country="US")) == set()
        assert _ids(router, LocationTarget(country="CA")) == set()

    async def test_select_respects_country(self, feed: FeedBuilder) -> None:
        router = feed.router()
        for _ in range(3):
            selected = await router.select_proxy_for_project("project-1", location=LocationTarget(country="GB"))
            assert selected is not None and selected.id == "px-uk-1"

    async def test_sticky_session_rebinds_when_country_changes(self, feed: FeedBuilder) -> None:
        feed.projects["project-1"].routing_strategy = "sticky"
        router = feed.router()
        first = await router.select_proxy_for_project("project-1", "sess-1", location=LocationTarget(country="GB"))
        second = await router.select_proxy_for_project("project-1", "sess-1", location=LocationTarget(country="FR"))
        assert first is not None and first.id == "px-uk-1"
        assert second is not None and second.id == "px-fr"


class TestDynamicSessions:
    """A dynamic-sessions connector: one gateway row, credentials rendered per request."""

    @pytest.fixture
    def feed(self) -> FeedBuilder:
        return FeedBuilder().add(
            Project(id="project-1", name="P", username="p", password="pw"),
            _oxylabs_credential(),
            Connector(
                id="conn-dyn", name="Oxy dynamic", credential_id="cred-oxy", credential_type="oxylabs",
                project_id="project-1", config={"session_mode": "dynamic", "session_duration_minutes": 10},
            ),
            _gateway_row(),
        )

    def test_gateway_serves_any_country(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert [p.id for p in router.get_routable_proxies_for_project("project-1")] == ["gw"]
        assert _ids(router, LocationTarget(country="DE")) == {"gw"}
        view = router.snapshot.connector_view("conn-dyn")
        assert view is not None and not view.accepts_request_country
        assert view.provider is not None
        assert view.provider.proxy_target(list(view.proxies)) == ProxyTarget(total=1, dynamic=True, exit_sample_percent=5)

    def test_allow_list_filters_at_the_connector(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-dyn"].config["country_code"] = ["US", "DE"]
        router = feed.router()
        assert _ids(router, LocationTarget(country="DE")) == {"gw"}
        assert router.get_routable_proxies_for_project("project-1", location=LocationTarget(country="FR")) == []
        assert [p.id for p in router.get_routable_proxies_for_project("project-1")] == ["gw"]

    async def test_explicit_session_is_derived_and_stable(self, feed: FeedBuilder) -> None:
        router = feed.router()
        first = await router.select_proxy_for_project("project-1", "order-1", location=LocationTarget(country="DE"))
        second = await router.select_proxy_for_project("project-1", "order-1", location=LocationTarget(country="DE"))
        assert first is not None and second is not None
        assert first.id == "gw" and first.username == second.username
        assert first.username is not None
        assert first.username.startswith("customer-alice-cc-DE-sessid-") and first.username.endswith("-sesstime-10")
        assert "order-1" not in first.username
        assert first.password == "s3cret"  # resolved after rendering
        assert first.metadata["session_id"] in first.username and first.metadata["geo"] == "DE"
        stored = router.snapshot.proxy("gw")
        # The feed's row is untouched.
        assert stored is not None and stored.username == "customer-alice-sessid-probe0probe0-sesstime-10"

    async def test_placeholders_are_filled_after_rendering(self, feed: FeedBuilder) -> None:
        """Rendering re-renders the templates, which put the secret back as a placeholder."""
        router = feed.router()
        view = router.snapshot.connector_view("conn-dyn")
        gateway = router.snapshot.proxy("gw")
        assert view is not None and view.provider is not None and gateway is not None
        assert gateway.password == "s3cret"  # the compiler resolved the stored row
        rendered = view.provider.render_request(gateway, sessid="order-1", location=None, scope="project-1")
        assert rendered.password == "{password}"
        selected = await router.select_proxy_for_project("project-1", "order-1")
        assert selected is not None and selected.password == "s3cret"

    async def test_no_session_id_does_not_pin_a_vendor_session(self, feed: FeedBuilder) -> None:
        router = feed.router()
        # A request without -sessid- has no routing key at all, so the vendor rotates.
        first = await router.select_proxy_for_project("project-1", None)
        second = await router.select_proxy_for_project("project-1", None)
        assert first is not None and second is not None
        assert first.username != second.username
        assert "session_id" not in first.metadata and "-cc-" not in (first.username or "")


class RecordingOnDemand:
    """An ``OnDemand`` that records what the router asks for and never delivers."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []
        self.waits: list[float] = []

    async def request(self, connector_id: str, country: str) -> None:
        self.requested.append((connector_id, country))

    async def wait(self, predicate: Callable[[RoutingSnapshot], bool], timeout: float) -> bool:
        self.waits.append(timeout)
        return False


class TestPlaceRouting:
    """-st- and -city-: fixed exits match their known place; dynamic gateways render it."""

    @pytest.fixture
    def feed(self) -> FeedBuilder:
        feed = FeedBuilder()
        feed.add(
            Project(id="project-1", name="P", username="p", password="pw"),
            _static_credential(),
            Connector(
                id="conn-static", name="Static", credential_id="cred-static",
                credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
            ),
        )
        places = {
            "px-ny": {"country": "US", "location": {"state_code": "NY", "city": "New York"}},
            "px-la": {"country": "US", "location": {"state_code": "CA", "city": "Los Angeles"}},
            "px-us-unknown": {"country": "US"},
            # The databases say Austin; the operator pinned Buffalo, and the pin wins.
            "px-pinned": {
                "country": "US",
                "location": {"state_code": "TX", "city": "Austin"},
                "manual_location": {"state_code": "NY", "city": "buffalo"},
            },
        }
        for proxy_id, metadata in places.items():
            feed.add(healthy(Proxy(id=proxy_id, host=proxy_id, port=1, connector_id="conn-static", metadata=metadata)))
        feed.add(
            _oxylabs_credential(),
            Connector(
                id="conn-dyn", name="Oxy dynamic", credential_id="cred-oxy", credential_type="oxylabs",
                project_id="project-1", config={"session_mode": "dynamic", "session_duration_minutes": 10},
            ),
            _gateway_row(),
            Connector(
                id="conn-pool", name="Oxy pool", credential_id="cred-oxy", credential_type="oxylabs",
                project_id="project-1", config={"num_proxies": 1, "session_duration_minutes": 10, "country_code": ["US"]},
            ),
            healthy(Proxy(
                id="slot", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
                username="customer-alice-cc-US-sessid-aaa-sesstime-10", password="{password}", connector_id="conn-pool",
                metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": "aaa", "geo": "US"},
            )),
        )
        return feed

    def test_country_alone_is_unchanged(self, feed: FeedBuilder) -> None:
        served = _ids(feed.router(), LocationTarget(country="US"))
        assert served == {"px-ny", "px-la", "px-us-unknown", "px-pinned", "gw", "slot"}

    def test_state_matches_known_regions_and_the_gateway(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _ids(router, LocationTarget(country="US", state="NY")) == {"px-ny", "px-pinned", "gw"}
        assert _ids(router, LocationTarget(country="US", state="CA")) == {"px-la", "gw"}

    def test_city_matches_the_slug(self, feed: FeedBuilder) -> None:
        router = feed.router()
        assert _ids(router, LocationTarget(country="US", city="new_york")) == {"px-ny", "gw"}
        assert _ids(router, LocationTarget(country="US", state="NY", city="buffalo")) == {"px-pinned", "gw"}
        assert _ids(router, LocationTarget(country="US", state="TX", city="austin")) == {"gw"}

    def test_unknown_places_and_pooled_slots_are_never_widened(self, feed: FeedBuilder) -> None:
        narrowed = _ids(feed.router(), LocationTarget(country="US", state="CA"))
        assert "px-us-unknown" not in narrowed and "slot" not in narrowed

    def test_gateway_serves_only_what_it_can_say(self, feed: FeedBuilder) -> None:
        router = feed.router()
        # Oxylabs names US states; England has no name on record and nothing else is there.
        assert _ids(router, LocationTarget(country="GB", state="ENG")) == set()
        assert _ids(router, LocationTarget(country="DE", city="berlin")) == {"gw"}

    def test_refusing_unknown_exits_judges_only_rows_that_carry_a_place(self, feed: FeedBuilder) -> None:
        # The gateway row carries no state or city: the vendor renders the place
        # per request and preflight judges it. A fixed exit whose place is known
        # matches as before; one without a place is out by the match alone.
        feed.projects["project-1"].location_unknown = UnknownAction.REFUSE
        router = feed.router()
        assert _ids(router, LocationTarget(country="US", state="NY")) == {"px-ny", "px-pinned", "gw"}
        assert _ids(router, LocationTarget(country="DE", city="berlin")) == {"gw"}

    async def test_select_renders_the_place_and_never_provisions(self, feed: FeedBuilder) -> None:
        on_demand = RecordingOnDemand()
        router = feed.router(on_demand=on_demand)
        berlin = LocationTarget(country="DE", city="berlin")
        selected = await router.select_proxy_for_project("project-1", "order-1", location=berlin)
        assert selected is not None and selected.id == "gw" and "-city-berlin-" in (selected.username or "")
        assert selected.metadata["geo_city"] == "berlin"
        assert await router.select_proxy_for_project("project-1", location=LocationTarget(country="FR", state="IDF")) is None
        assert on_demand.requested == [] and on_demand.waits == []
        # A message for the client names the whole place.
        assert not await router.are_all_proxies_quarantined("project-1", location=LocationTarget(country="FR", state="IDF"))


class ProvisioningOnDemand:
    """The gateway's ``OnDemand`` with the cluster behind it, in one process.

    A wait provisions every group asked for as the provision job does (only a
    country no row of the connector carries yet) and applies the next snapshot
    as the follower would, unless ``arrives`` is off and it lasts its whole
    timeout; ``release`` holds waits until a test lets them go.
    """

    def __init__(self, feed: FeedBuilder, *, arrives: bool = True) -> None:
        self.feed = feed
        self.arrives = arrives
        self.router: Router | None = None
        self.requested: list[tuple[str, str]] = []
        self.waits: list[float] = []
        self.release = asyncio.Event()
        self.release.set()

    async def request(self, connector_id: str, country: str) -> None:
        self.requested.append((connector_id, country))

    async def wait(self, predicate: Callable[[RoutingSnapshot], bool], timeout: float) -> bool:
        assert self.router is not None
        self.waits.append(timeout)
        await self.release.wait()
        if not self.arrives:
            await asyncio.sleep(timeout)
            return False
        for connector_id, country in self.requested:
            await self._provision(connector_id, country)
        self.router.swap(self.feed.snapshot(previous=self.router.snapshot))
        return predicate(self.router.snapshot)

    async def _provision(self, connector_id: str, country: str) -> None:
        assert self.router is not None
        existing = [p for p in self.feed.proxies.values() if p.connector_id == connector_id]
        if any(p.metadata.get("geo") == country for p in existing):
            return
        view = self.router.snapshot.connector_view(connector_id)
        assert view is not None and view.provider is not None
        self.feed.add(*await view.provider.provision_country(existing, country))


class TestOnDemandCountryGroups:
    """"All countries" residential pools: a country nothing serves is asked for, awaited, then routed."""

    @pytest.fixture
    def feed(self) -> FeedBuilder:
        feed = FeedBuilder().add(
            Project(id="project-1", name="P", username="p", password="pw"),
            _oxylabs_credential(),
            Connector(
                id="conn-any", name="Oxy any", credential_id="cred-oxy", credential_type="oxylabs",
                project_id="project-1", config={"num_proxies": 2, "session_duration_minutes": 10},
            ),
        )
        # Two existing non-geo slots, as the sync would have created them.
        for sid in ("aaa", "bbb"):
            feed.add(healthy(Proxy(
                id=f"px-{sid}", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
                username=f"customer-alice-sessid-{sid}-sesstime-10", password="{password}",
                connector_id="conn-any",
                metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": sid},
            )))
        return feed

    @staticmethod
    def _router(feed: FeedBuilder, on_demand: ProvisioningOnDemand) -> Router:
        router = feed.router(on_demand=on_demand)
        on_demand.router = router
        return router

    def test_pool_accepts_request_country(self, feed: FeedBuilder) -> None:
        view = feed.snapshot().connector_view("conn-any")
        assert view is not None and view.accepts_request_country

    def test_pinned_pool_does_not(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-any"].config["country_code"] = ["US"]
        view = feed.snapshot().connector_view("conn-any")
        assert view is not None and not view.accepts_request_country

    async def test_first_request_waits_for_the_group_and_selects_from_it(self, feed: FeedBuilder) -> None:
        on_demand = ProvisioningOnDemand(feed)
        router = self._router(feed, on_demand)
        assert router.get_routable_proxies_for_project("project-1", location=LocationTarget(country="DE")) == []

        selected = await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE"))

        assert on_demand.requested == [("conn-any", "DE")] and on_demand.waits == [ON_DEMAND_WAIT_SECONDS]
        assert selected is not None
        assert selected.metadata["geo"] == "DE"
        assert selected.username is not None
        assert selected.username.startswith("customer-alice-cc-DE-sessid-")
        assert selected.username.endswith("-sesstime-10")
        assert selected.password == "s3cret"  # the group arrives through the feed with its credentials resolved
        de_group = [p for p in feed.proxies.values() if p.metadata.get("geo") == "DE"]
        assert len(de_group) == 2  # num_proxies is per country
        assert all(p.status == ProxyStatus.HEALTHY for p in de_group)

    async def test_a_group_is_asked_for_once_while_requests_wait(self, feed: FeedBuilder) -> None:
        on_demand = ProvisioningOnDemand(feed)
        on_demand.release.clear()
        router = self._router(feed, on_demand)
        location = LocationTarget(country="DE")

        waiting = [asyncio.create_task(router.select_proxy_for_project("project-1", location=location)) for _ in range(3)]
        await until(lambda: len(on_demand.waits) == 3)
        assert on_demand.requested == [("conn-any", "DE")]
        on_demand.release.set()
        selected = await asyncio.gather(*waiting)

        assert all(proxy is not None and proxy.metadata["geo"] == "DE" for proxy in selected)
        # The group is in the feed now: the next request neither asks nor waits.
        assert await router.select_proxy_for_project("project-1", location=location) is not None
        assert on_demand.requested == [("conn-any", "DE")] and len(on_demand.waits) == 3

    async def test_a_group_that_never_comes_is_asked_for_again_while_misses_keep_coming(
        self, feed: FeedBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provision job that failed or was lost is asked for anew, though the waits of the misses overlap."""
        monkeypatch.setattr(router_module, "ON_DEMAND_WAIT_SECONDS", 0.1)
        on_demand = ProvisioningOnDemand(feed, arrives=False)
        router = self._router(feed, on_demand)
        location = LocationTarget(country="DE")

        started = time.monotonic()
        misses = []
        # A miss every 25 ms, each waiting 100 ms: some request is always waiting.
        for _ in range(12):
            misses.append(asyncio.create_task(router.select_proxy_for_project("project-1", location=location)))
            await asyncio.sleep(0.025)
        assert await asyncio.gather(*misses) == [None] * 12
        elapsed = time.monotonic() - started

        # Asked again once a wait had passed without the group, and never more than once a wait.
        assert 2 <= len(on_demand.requested) <= elapsed / 0.1 + 1
        assert set(on_demand.requested) == {("conn-any", "DE")}

    async def test_a_failed_ask_refuses_every_request_sharing_it_at_once(self, feed: FeedBuilder) -> None:
        class Failing(ProvisioningOnDemand):
            answered = asyncio.Event()

            async def request(self, connector_id: str, country: str) -> None:
                self.requested.append((connector_id, country))
                await self.answered.wait()
                raise ConnectionError("Redis is down")

        on_demand = Failing(feed)
        router = self._router(feed, on_demand)
        location = LocationTarget(country="DE")
        misses = [
            asyncio.create_task(router.select_proxy_for_project("project-1", location=location)) for _ in range(3)
        ]
        await until(lambda: len(on_demand.requested) == 1)

        on_demand.answered.set()
        assert await asyncio.wait_for(asyncio.gather(*misses), 1.0) == [None] * 3
        # Nobody waited for a group nobody had asked for, and the next miss asks again at once.
        assert on_demand.waits == []
        assert await router.select_proxy_for_project("project-1", location=location) is None
        assert on_demand.requested == [("conn-any", "DE")] * 2

    async def test_an_existing_group_is_no_miss_whatever_its_state(self, feed: FeedBuilder) -> None:
        feed.add(Proxy(
            id="px-de", host="pr.oxylabs.io", port=7777, connector_id="conn-any",
            metadata={"session_id": "de1", "geo": "DE"}, status=ProxyStatus.UNHEALTHY,
        ))
        on_demand = ProvisioningOnDemand(feed)
        router = self._router(feed, on_demand)
        assert await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE")) is None
        assert on_demand.requested == [] and on_demand.waits == []

    async def test_a_group_that_does_not_arrive_in_time_is_refused(
        self, feed: FeedBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router_module, "ON_DEMAND_WAIT_SECONDS", 0.05)
        on_demand = ProvisioningOnDemand(feed, arrives=False)
        router = self._router(feed, on_demand)
        assert await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE")) is None
        assert on_demand.requested == [("conn-any", "DE")] and on_demand.waits == [0.05]
        assert not any(p.metadata.get("geo") for p in feed.proxies.values())
        # The ask is spent with the wait: the next miss asks again.
        await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE"))
        assert on_demand.requested == [("conn-any", "DE")] * 2

    async def test_a_failed_request_refuses_without_waiting(self, feed: FeedBuilder) -> None:
        class Failing(ProvisioningOnDemand):
            async def request(self, connector_id: str, country: str) -> None:
                raise ConnectionError("Redis is down")

        on_demand = Failing(feed)
        router = self._router(feed, on_demand)
        assert await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE")) is None
        assert on_demand.waits == []

    async def test_requests_without_country_skip_geo_groups(self, feed: FeedBuilder) -> None:
        router = self._router(feed, ProvisioningOnDemand(feed))
        await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE"))
        assert _ids(router) == {"px-aaa", "px-bbb"}

    async def test_geo_group_serves_only_its_country(self, feed: FeedBuilder) -> None:
        router = self._router(feed, ProvisioningOnDemand(feed))
        await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE"))
        assert router.get_routable_proxies_for_project("project-1", location=LocationTarget(country="FR")) == []

    async def test_disabled_pools_are_not_asked(self, feed: FeedBuilder) -> None:
        feed.connectors["conn-any"].enabled = False
        on_demand = ProvisioningOnDemand(feed)
        router = self._router(feed, on_demand)
        assert await router.select_proxy_for_project("project-1", location=LocationTarget(country="DE")) is None
        assert on_demand.requested == []
