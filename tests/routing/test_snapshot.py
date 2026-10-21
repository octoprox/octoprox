# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the routing snapshot: what one applied feed indexes, and what an update rebuilds.

Reuse is checked as a gateway applies an update: unchanged sections are the very objects of the previous mapping.
"""

from datetime import datetime, timedelta

import pytest
import structlog

from api.core import utc_now
from api.feed.models import GLOBAL_SECTION, GlobalSection, connector_section
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.registry import build_registry
from api.routing.snapshot import RoutingSnapshot
from tests.providers.test_registry import fail_to_build
from tests.routing.builder import FeedBuilder, healthy

_CREATED_AT = datetime(2026, 10, 1, 9, 0, 0)


@pytest.fixture
def feed() -> FeedBuilder:
    """A static connector, a dynamic Oxylabs connector and a second project with nothing in it."""
    return FeedBuilder().add(
        Project(id="project-1", name="P", username="client", password="pw"),
        Project(id="project-2", name="Q", username="other", password="pw"),
        Credential(id="cred-static", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1"),
        Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ),
        Connector(
            id="static", name="Static", credential_id="cred-static", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="project-1", config={"countries": ["GB"]}, created_at=_CREATED_AT + timedelta(seconds=1),
            routing_config={"domain_whitelist": ["example.com"], "weight": 3},
            rate_limit_config={
                "max_requests": 5, "window_seconds": 60, "quarantine_seconds_min": 30,
                "quarantine_seconds_max": 30, "sticky_quarantine": True,
            },
            traffic_config={"limit_bytes": 1000, "action": "block", "limit_status": 429},
        ),
        Connector(
            id="dyn", name="Dynamic", credential_id="cred-oxy", credential_type="oxylabs", project_id="project-1",
            config={"session_mode": "dynamic", "session_duration_minutes": 10}, created_at=_CREATED_AT,
        ),
        healthy(Proxy(id="px-1", host="h1", port=1, connector_id="static", created_at=_CREATED_AT)),
        healthy(Proxy(id="px-2", host="h2", port=1, connector_id="static", created_at=_CREATED_AT - timedelta(hours=1))),
        healthy(Proxy(
            id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP, connector_id="dyn",
            username="customer-{username}-sessid-x-sesstime-10", password="{password}",
            metadata={"session_id": "x", "dynamic_sessions": "true", "geo": "DE"},
        )),
    )


class TestIndexes:
    def test_projects_by_id_and_by_username(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        project = snapshot.project("project-1")
        assert project is not None and project.name == "P"
        assert snapshot.project_by_username("client") is project
        assert snapshot.project_by_username("nobody") is None and snapshot.project("gone") is None

    def test_connectors_of_a_project_in_creation_order(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        assert [view.connector.id for view in snapshot.connector_views("project-1")] == ["dyn", "static"]
        assert snapshot.connector_views("project-2") == ()

    def test_configs_are_parsed_once_into_the_view(self, feed: FeedBuilder) -> None:
        view = feed.snapshot().connector_view("static")
        assert view is not None
        assert view.routing is not None and view.routing.domain_whitelist == ["example.com"]
        assert view.weight == 3
        assert view.rate_limit is not None and view.rate_limit.sticky_quarantine is True
        assert (view.traffic.limit_bytes, view.traffic.limit_status) == (1000, 429)
        assert view.declared_countries == ("GB",)
        assert view.allows_host("www.example.com") and not view.allows_host("other.com")

    def test_proxies_keep_feed_order_and_their_credentials_resolved(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        view = snapshot.connector_view("static")
        assert view is not None and [proxy.id for proxy in view.proxies] == ["px-2", "px-1"]
        gateway = snapshot.proxy("gw")
        assert gateway is not None
        assert (gateway.username, gateway.password) == ("customer-alice-sessid-x-sesstime-10", "s3cret")

    def test_providers_only_for_descriptor_connectors(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        static, dynamic = snapshot.connector_view("static"), snapshot.connector_view("dyn")
        assert static is not None and static.provider is None and static.credential is None
        assert dynamic is not None and dynamic.provider is not None and dynamic.provider.is_dynamic
        assert dynamic.credential is not None and dynamic.credential.id == "cred-oxy"

    def test_proxy_lookups(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        assert set(snapshot.proxy_ids) == {"px-1", "px-2", "gw"}
        assert set(snapshot.connector_ids) == {"static", "dyn"}
        view = snapshot.view_of("px-1")
        assert view is not None and view is snapshot.connector_view("static")
        assert snapshot.view_of("gone") is None and snapshot.proxy("gone") is None

    def test_slot_groups_the_connector_holds(self, feed: FeedBuilder) -> None:
        snapshot = feed.snapshot()
        assert snapshot.has_geo_group("dyn", "DE")
        assert not snapshot.has_geo_group("dyn", "FR") and not snapshot.has_geo_group("gone", "DE")

    def test_runtime_decisions(self, feed: FeedBuilder) -> None:
        now = utc_now()
        feed.quarantined["px-1"] = now + timedelta(minutes=5)
        feed.blocked_until["static"] = now + timedelta(days=1)
        feed.blocked_until["dyn"] = now - timedelta(seconds=1)
        snapshot = feed.snapshot()
        assert dict(snapshot.quarantined) == {"px-1": now + timedelta(minutes=5)}
        assert snapshot.is_traffic_blocked("static")
        # A block that ended is no block, even before the controller drops it.
        assert not snapshot.is_traffic_blocked("dyn") and not snapshot.is_traffic_blocked("gone")

    def test_an_unreadable_routing_config_serves_no_named_host(self, feed: FeedBuilder) -> None:
        feed.connectors["static"].routing_config = {"domain_whitelist": ["a.com"], "domain_blacklist": ["b.com"]}
        view = feed.snapshot().connector_view("static")
        assert view is not None and view.routing is None and view.weight == 1
        assert not view.allows_host("a.com")

    def test_an_unreadable_rate_limit_holds_no_session(self, feed: FeedBuilder) -> None:
        feed.connectors["static"].rate_limit_config = {"max_requests": 0}
        view = feed.snapshot().connector_view("static")
        assert view is not None and view.rate_limit is None

    def test_a_provider_type_this_process_lacks_leaves_the_connector_without_one(self, feed: FeedBuilder) -> None:
        """A plugin installed where the controller compiles but not on this gateway: rows route as the feed has them."""
        registry = build_registry(include_plugins=False)
        registry.unregister("oxylabs")
        snapshot = RoutingSnapshot.from_sections(feed.sections(), registry=registry)
        view = snapshot.connector_view("dyn")
        assert view is not None and view.provider is None and not view.accepts_request_country
        assert [proxy.id for proxy in view.proxies] == ["gw"]

    def test_a_provider_that_cannot_be_built_is_logged_and_its_rows_route_as_the_feed_has_them(
        self, feed: FeedBuilder
    ) -> None:
        """Whatever the error: one connector's provider never stops a gateway applying its feed."""
        registry = build_registry(include_plugins=False)
        fail_to_build(registry, "oxylabs", RuntimeError("the vendor SDK is broken"))

        with structlog.testing.capture_logs() as logs:
            snapshot = RoutingSnapshot.from_sections(feed.sections(), registry=registry)

        view = snapshot.connector_view("dyn")
        assert view is not None and view.provider is None and not view.accepts_request_country
        assert [proxy.id for proxy in view.proxies] == ["gw"]
        assert [(log["event"], log["connector_id"], log["provider"], log["error"]) for log in logs] == [
            ("Connector provider could not be built", "dyn", "oxylabs", "the vendor SDK is broken")
        ]
        assert [log["log_level"] for log in logs] == ["warning"]

    def test_a_provider_error_without_text_is_logged_by_its_type(self, feed: FeedBuilder) -> None:
        """A plugin's bare NotImplementedError has no text: the log line names its type."""
        registry = build_registry(include_plugins=False)
        fail_to_build(registry, "oxylabs", NotImplementedError())

        with structlog.testing.capture_logs() as logs:
            RoutingSnapshot.from_sections(feed.sections(), registry=registry)

        assert [log["error"] for log in logs if log["event"] == "Connector provider could not be built"] == [
            "NotImplementedError"
        ]

    def test_a_feed_without_global_section_routes_nothing(self, feed: FeedBuilder) -> None:
        sections = feed.sections()
        del sections[GLOBAL_SECTION]
        snapshot = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        assert snapshot.project("project-1") is None and snapshot.connector_view("static") is not None


class TestIncrementalBuild:
    def test_unchanged_sections_keep_their_index(self, feed: FeedBuilder) -> None:
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        feed.proxies["px-1"].port = 2
        changed = {**sections, connector_section("static"): feed.connector_section("static")}

        second = RoutingSnapshot.from_sections(changed, registry=feed.registry, previous=first)

        assert second.connector_view("dyn") is first.connector_view("dyn")
        assert second.connector_view("static") is not first.connector_view("static")
        proxy = second.proxy("px-1")
        assert proxy is not None and proxy.port == 2
        # The previous snapshot is untouched: a request that started on it finishes on it.
        old = first.proxy("px-1")
        assert old is not None and old.port == 1

    def test_a_changed_global_section_rebuilds_every_connector(self, feed: FeedBuilder) -> None:
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        project = Project(id="project-1", name="R", username="client", password="pw")
        renamed = {**sections, GLOBAL_SECTION: GlobalSection(projects=[project])}

        second = RoutingSnapshot.from_sections(renamed, registry=feed.registry, previous=first)

        first_dynamic, second_dynamic = first.connector_view("dyn"), second.connector_view("dyn")
        assert first_dynamic is not None and second_dynamic is not None
        assert second_dynamic is not first_dynamic and second_dynamic.provider is not first_dynamic.provider
        assert second.project("project-1") is project

    def test_providers_follow_the_registry_with_the_global_section(self, feed: FeedBuilder) -> None:
        """Custom descriptors travel in the global section; the gateway applies them to its registry first."""
        registry = build_registry(include_plugins=False)
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=registry)
        registry.unregister("oxylabs")

        same_global = RoutingSnapshot.from_sections(dict(sections), registry=registry, previous=first)
        new_global = RoutingSnapshot.from_sections(
            {**sections, GLOBAL_SECTION: sections[GLOBAL_SECTION].model_copy()}, registry=registry, previous=first
        )

        kept, rebuilt = same_global.connector_view("dyn"), new_global.connector_view("dyn")
        assert kept is not None and kept.provider is not None
        assert rebuilt is not None and rebuilt.provider is None

    def test_removed_sections_are_gone(self, feed: FeedBuilder) -> None:
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        del sections[connector_section("static")]

        second = RoutingSnapshot.from_sections(sections, registry=feed.registry, previous=first)

        assert second.connector_view("static") is None and second.proxy("px-1") is None
        assert set(second.connector_ids) == {"dyn"}
