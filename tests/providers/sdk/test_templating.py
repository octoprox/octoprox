# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for descriptor template rendering."""

import pytest

from api.providers.sdk.descriptor import Condition, TemplatePart, TemplateSpec
from api.providers.sdk.templating import (
    RenderContext,
    TemplateError,
    TemplateRenderer,
    resolve_runtime_placeholders,
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
