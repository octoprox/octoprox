# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for descriptor template rendering."""

import pytest

from api.models.location import LocationTarget
from api.providers.sdk.descriptor import (
    Condition,
    ProxyTypeSpec,
    TargetingSpec,
    TemplatePart,
    TemplateSpec,
)
from api.providers.sdk.templating import (
    RenderContext,
    TargetingSupport,
    TemplateError,
    TemplateRenderer,
    country_field_key,
    resolve_runtime_placeholders,
    targeting_support,
)


@pytest.fixture
def ctx() -> RenderContext:
    return RenderContext(
        credential={"username": "alice", "password": "s3cret", "token": "tok"},
        connector={"country_code": "US", "num_proxies": 3, "zone_password": "zp"},
        secret_keys=frozenset({"password", "token", "zone_password"}),
        session_id="abc123",
        index=2,
        port=8003,
    )


class TestRenderString:
    def test_substitutes_namespaced_variables(self, ctx: RenderContext) -> None:
        renderer = TemplateRenderer()
        assert renderer.render_string("customer-{credential.username}-cc-{connector.country_code}", ctx) == (
            "customer-alice-cc-US"
        )

    def test_scalars_and_filters(self, ctx: RenderContext) -> None:
        renderer = TemplateRenderer()
        assert renderer.render_string("{session_id}/{index}/{port}", ctx) == "abc123/2/8003"
        assert renderer.render_string("{connector.country_code|lower}", ctx) == "us"
        assert renderer.render_string("{credential.username|upper}", ctx) == "ALICE"
        assert renderer.render_string("{connector.missing|or:any}", ctx) == "any"
        assert renderer.render_string("{connector.country_code|lower|or:any}", ctx) == "us"
        assert renderer.render_string("{credential.username|urlencode}", RenderContext(credential={"username": "a b/c"})) == "a%20b%2Fc"

    def test_unknown_variable_renders_empty(self, ctx: RenderContext) -> None:
        assert TemplateRenderer().render_string("x{nothing.here}y{bogus}z", ctx) == "xyz"

    def test_unknown_filter_raises(self, ctx: RenderContext) -> None:
        with pytest.raises(TemplateError):
            TemplateRenderer().render_string("{credential.username|shout}", ctx)

    def test_proxy_mode_keeps_secrets_as_runtime_placeholders(self, ctx: RenderContext) -> None:
        renderer = TemplateRenderer()
        assert renderer.render_string("{credential.username}:{credential.password}", ctx, "proxy") == "alice:{password}"
        assert renderer.render_string("{connector.zone_password}", ctx, "proxy") == "{zone_password}"
        # Full mode substitutes everything (used for vendor API calls).
        assert renderer.render_string("{credential.password}", ctx, "full") == "s3cret"

    def test_secret_values_for_redaction(self, ctx: RenderContext) -> None:
        values = ctx.secret_values()
        assert set(values) == {"s3cret", "tok", "zp"}
        assert "jwt-1" in ctx.with_auth({"token": "jwt-1"}).secret_values()


class TestComposedTemplates:
    def test_parts_are_joined_and_conditional(self, ctx: RenderContext) -> None:
        template = TemplateSpec(
            separator="-",
            parts=[
                TemplatePart(text="customer-{credential.username}"),
                TemplatePart(text="cc-{connector.country_code}", when=Condition(field="connector.country_code")),
                TemplatePart(text="sessid-{session_id}"),
            ],
        )
        renderer = TemplateRenderer()
        assert renderer.render(template, ctx) == "customer-alice-cc-US-sessid-abc123"
        no_country = ctx.with_slot()
        no_country.connector = {}
        assert renderer.render(template, no_country) == "customer-alice-sessid-abc123"

    def test_condition_operators(self) -> None:
        assert Condition(field="x", equals="a").evaluate("a")
        assert not Condition(field="x", equals="a").evaluate("b")
        assert Condition.model_validate({"field": "x", "in": ["a", "b"]}).evaluate("b")
        assert Condition(field="x", negate=True).evaluate("")
        assert not Condition(field="x").evaluate(None)

    def test_empty_parts_are_dropped(self) -> None:
        template = TemplateSpec(separator="_", parts=[TemplatePart(text="{credential.password}"), TemplatePart(text="{connector.missing}"), TemplatePart(text="session-{session_id}")])
        ctx = RenderContext(credential={"password": "pw"}, session_id="s1")
        assert TemplateRenderer().render(template, ctx) == "pw_session-s1"

    def test_referenced_paths(self) -> None:
        template = TemplateSpec(parts=[TemplatePart(text="{credential.username}"), TemplatePart(text="ip-{discovered_ip}")])
        assert TemplateRenderer.referenced_paths(template) == {"credential.username", "discovered_ip"}
        assert TemplateRenderer.referenced_paths("{connector.zone_name|lower}") == {"connector.zone_name"}


class TestRuntimePlaceholders:
    def test_resolves_flat_keys(self) -> None:
        assert resolve_runtime_placeholders("user-{username}", {"username": "bob", "n": 1}) == "user-bob"
        assert resolve_runtime_placeholders("plain", {"username": "bob"}) == "plain"
        assert resolve_runtime_placeholders(None, {}) is None


class TestCountryFieldKey:
    """country_field_key() finds the connector field a proxy type geo-targets with."""

    def test_builtin_session_types_expose_country(self, builtins: dict) -> None:
        oxy = builtins["oxylabs"]
        assert country_field_key(oxy, oxy.get_proxy_type("residential")) == "country_code"
        assert country_field_key(oxy, oxy.get_proxy_type("mobile")) == "country_code"
        iproyal = builtins["iproyal"]
        assert country_field_key(iproyal, iproyal.proxy_types[0]) == "country_code"

    def test_port_types_without_country_in_credentials_return_none(self, builtins: dict) -> None:
        oxy = builtins["oxylabs"]
        assert country_field_key(oxy, oxy.get_proxy_type("isp")) is None
        assert country_field_key(oxy, oxy.get_proxy_type("datacenter")) is None

    def test_list_mode_filter_field_is_not_a_credential_country(self, builtins: dict) -> None:
        webshare = builtins["webshare"]
        for ptype in webshare.proxy_types:
            assert country_field_key(webshare, ptype) is None


class TestListValuesAndSlotCountry:
    def test_list_values_collapse(self) -> None:
        ctx = RenderContext(connector={"country_code": ["US"], "many": ["US", "DE"], "none": []})
        renderer = TemplateRenderer()
        assert renderer.render_string("{connector.country_code}", ctx) == "US"
        assert renderer.render_string("{connector.many|lower}", ctx) == "us,de"
        assert renderer.render_string("{connector.none|or:any}", ctx) == "any"

    def test_with_place_targets_the_slot_and_leaves_the_field_alone(self, ctx: RenderContext) -> None:
        narrowed = ctx.with_place(LocationTarget(country="DE"))
        assert narrowed.lookup("geo.country") == "DE"
        assert narrowed.target_country == "DE"
        assert narrowed.with_slot(session_id="zzz").target_country == "DE"
        # The connector field is the list the admin configured, never a slot's country.
        assert narrowed.lookup("connector.country_code") == "US" and ctx.lookup("geo.country") is None
        cleared = ctx.with_place(None)
        assert cleared.lookup("geo.country") is None and cleared.target_country is None
        assert TemplateRenderer().evaluate(Condition(field="geo.country"), cleared) is False


class TestListConditions:
    def test_all_conditions_must_hold(self, ctx: RenderContext) -> None:
        renderer = TemplateRenderer()
        spec = TemplateSpec(separator="-", parts=[
            TemplatePart(text="ip-{discovered_ip}", when=Condition(field="discovered_ip")),
            TemplatePart(text="country-{connector.country_code|lower}", when=[
                Condition(field="connector.country_code"),
                Condition(field="discovered_ip", negate=True),
            ]),
        ])
        assert renderer.render(spec, ctx) == "country-us"
        pinned = ctx.with_slot(discovered_ip="1.2.3.4")
        assert renderer.render(spec, pinned) == "ip-1.2.3.4"


class TestGeoNamespace:
    def test_with_place_exposes_the_place(self, ctx: RenderContext) -> None:
        target = LocationTarget(country="US", state="NY", city="new_york")
        request = ctx.with_place(target)
        assert request.lookup("geo.country") == "US"
        assert request.lookup("geo.state") == "NY"
        assert request.lookup("geo.state_name") == "new_york"
        assert request.lookup("geo.city") == "new_york"
        assert ctx.lookup("geo.city") is None  # original untouched
        # Copies keep the request; clearing it drops every key.
        assert request.with_slot(session_id="x").lookup("geo.city") == "new_york"
        assert request.with_place(None).lookup("geo.city") is None
        # A non-US state has no name on record: empty, so a part conditioned on it is dropped.
        assert ctx.with_place(LocationTarget(country="GB", state="ENG")).lookup("geo.state_name") == ""
        # Slot-group rendering targets the country alone.
        assert ctx.with_place(LocationTarget(country="DE")).lookup("geo.country") == "DE"
        assert ctx.with_place(LocationTarget(country="DE")).lookup("geo.city") == ""

    def test_geo_parts_render_and_filter(self, ctx: RenderContext) -> None:
        template = TemplateSpec(
            separator="-",
            parts=[
                TemplatePart(text="cc-{geo.country}"),
                TemplatePart(text="st-{geo.country|lower}_{geo.state_name}", when=Condition(field="geo.state_name")),
                TemplatePart(text="city-{geo.city|nospace}", when=Condition(field="geo.city")),
            ],
        )
        renderer = TemplateRenderer()
        assert renderer.render(template, ctx.with_place(LocationTarget(country="US", state="CA", city="los_angeles"))) == "cc-US-st-us_california-city-losangeles"
        assert renderer.render(template, ctx.with_place(LocationTarget(country="US"))) == "cc-US"
        assert renderer.render_string("{geo.city|nospace}", ctx.with_place(LocationTarget(country="FR", city="saint_denis"))) == "saintdenis"


class TestTargetingSupport:
    def test_builtins(self, builtins: dict) -> None:
        oxy = targeting_support(builtins["oxylabs"].get_proxy_type("residential"))
        assert oxy == TargetingSupport(state=True, city=True, state_by_name=True)
        brd = targeting_support(builtins["brightdata"].get_proxy_type("residential"))
        assert brd == TargetingSupport(state=True, city=True, state_by_name=False)
        assert targeting_support(builtins["netnut"].get_proxy_type("residential")) == TargetingSupport(state=True, city=True, state_by_name=True)
        assert targeting_support(builtins["iproyal"].get_proxy_type("residential")).city
        assert targeting_support(builtins["decodo"].get_proxy_type("mobile")).state
        # Port and list types carry nothing of the request.
        assert targeting_support(builtins["oxylabs"].get_proxy_type("isp")) == TargetingSupport()
        assert targeting_support(builtins["webshare"].get_proxy_type("proxies")) == TargetingSupport()

    def test_serves(self, builtins: dict) -> None:
        oxy_type = builtins["oxylabs"].get_proxy_type("residential")
        oxy = targeting_support(oxy_type)
        assert oxy.serves(LocationTarget(country="US", state="CA", city="los_angeles"), oxy_type)
        assert oxy.serves(LocationTarget(country="DE", city="berlin"), oxy_type)
        # Oxylabs names the state, so only US states can be said.
        assert not oxy.serves(LocationTarget(country="GB", state="ENG"), oxy_type)
        brd_type = builtins["brightdata"].get_proxy_type("residential")
        assert targeting_support(brd_type).serves(LocationTarget(country="GB", state="ENG"), brd_type)
        # NetNut cannot say a city without its state.
        netnut_type = builtins["netnut"].get_proxy_type("residential")
        netnut = targeting_support(netnut_type)
        assert not netnut.serves(LocationTarget(country="US", city="dallas"), netnut_type)
        assert netnut.serves(LocationTarget(country="US", state="TX", city="dallas"), netnut_type)
        # A type with nothing of the request serves the country alone.
        isp_type = builtins["oxylabs"].get_proxy_type("isp")
        assert targeting_support(isp_type).serves(LocationTarget(country="US"), isp_type)
        assert not targeting_support(isp_type).serves(LocationTarget(country="US", state="NY"), isp_type)

    def test_state_or_city_constraint(self) -> None:
        spec = ProxyTypeSpec(
            key="r", label="R", mode="session", host="h", port=1,
            username="{credential.u}-{geo.state}-{geo.city}",
            targeting=TargetingSpec(state_or_city=True),
        )
        support = targeting_support(spec)
        assert support.serves(LocationTarget(country="US", state="AZ"), spec)
        assert support.serves(LocationTarget(country="US", city="houston"), spec)
        assert not support.serves(LocationTarget(country="US", state="TX", city="houston"), spec)
