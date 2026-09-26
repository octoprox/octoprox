# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Dynamic sessions: one gateway row per connector, vendor credentials rendered per request."""

import re

import pytest

from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import build_registry
from api.providers.sdk.descriptor import ProviderDescriptor, SessionIdSpec
from api.providers.sdk.provider import DescriptorProvider, SdkRuntime
from api.providers.sdk.session_ids import SessionIdGenerator
from api.providers.sdk.strategies import (
    META_DYNAMIC_SESSIONS,
    META_EXIT_SAMPLE_PERCENT,
    META_GEO,
    META_SESSION_ID,
)
from api.providers.sdk.validation import ConfigValidationError
from tests.providers.sdk.conftest import TEST_POLICY, make_connector, make_credential


@pytest.fixture
def runtime() -> SdkRuntime:
    return SdkRuntime(egress_policy=TEST_POLICY)


def _oxylabs(builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, **config: object) -> DescriptorProvider:
    credential = make_credential("oxylabs", {"proxy_type": "residential", "username": "alice", "password": "pw"})
    connector_config: dict[str, object] = {"session_mode": "dynamic", "session_duration_minutes": 10}
    connector_config.update(config)
    return DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", connector_config), credential, runtime)


class TestSessionIdDerivation:
    def test_derive_is_deterministic_and_fits_the_spec(self) -> None:
        generator = SessionIdGenerator(SessionIdSpec(length=12, alphabet="lower_digits"))
        first = generator.derive("proj:order-123")
        assert first == generator.derive("proj:order-123")
        assert first != generator.derive("proj:order-124")
        assert first != generator.derive("other:order-123")
        assert re.fullmatch(r"[a-z0-9]{12}", first)

    def test_digits_alphabet_never_starts_with_zero(self) -> None:
        generator = SessionIdGenerator(SessionIdSpec(length=8, alphabet="digits"))
        ids = {generator.derive(f"seed-{i}") for i in range(300)}
        assert all(re.fullmatch(r"[1-9][0-9]{7}", sid) for sid in ids)
        assert len(ids) > 290  # no accidental collapse
        assert generator.derive("x") == generator.derive("x")

    def test_prefix_is_kept(self) -> None:
        generator = SessionIdGenerator(SessionIdSpec(length=4, alphabet="lower", prefix="s_"))
        assert generator.derive("a").startswith("s_") and len(generator.derive("a")) == 6
        assert generator.generate().startswith("s_")


class TestGatewayRow:
    async def test_sync_creates_exactly_one_gateway(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime, num_proxies=25, country_code=["US", "DE"])
        assert provider.is_dynamic and provider.is_session_based()
        assert not provider.accepts_request_country()
        to_add, to_remove = await provider.sync_proxies([])
        assert to_remove == [] and len(to_add) == 1
        gateway = to_add[0]
        assert gateway.host == "pr.oxylabs.io" and gateway.port == 7777
        assert gateway.status == ProxyStatus.HEALTHY
        assert gateway.metadata[META_DYNAMIC_SESSIONS] == "true"
        assert META_GEO not in gateway.metadata
        # Stored with a random session for the health probe and no country.
        assert re.fullmatch(r"customer-alice-sessid-[a-z0-9]{12}-sesstime-10", gateway.username or "")
        assert gateway.password == "{password}"
        # A second sync keeps it and adds nothing.
        assert await provider.sync_proxies(to_add) == ([], [])
        assert await provider.refresh_ips(to_add) == ([], [])

    async def test_switching_a_pool_to_dynamic_replaces_its_slots(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime)
        slots = [
            Proxy(id=f"slot-{i}", host="pr.oxylabs.io", port=7777, connector_id="conn-1", status=ProxyStatus.HEALTHY,
                  metadata={META_SESSION_ID: f"s{i}", "geo": "US"})
            for i in range(3)
        ]
        to_add, to_remove = await provider.sync_proxies(slots)
        assert len(to_add) == 1 and to_add[0].metadata[META_DYNAMIC_SESSIONS] == "true"
        assert set(to_remove) == {"slot-0", "slot-1", "slot-2"}

    async def test_duplicate_gateways_collapse_to_the_healthiest(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime)
        rows = [
            Proxy(id="sick", host="h", port=1, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, metadata={META_DYNAMIC_SESSIONS: "true"}),
            Proxy(id="fine", host="h", port=1, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={META_DYNAMIC_SESSIONS: "true"}),
            Proxy(id="slot", host="h", port=1, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={META_SESSION_ID: "x"}),
        ]
        to_add, to_remove = await provider.sync_proxies(rows)
        assert to_add == [] and set(to_remove) == {"sick", "slot"}

    def test_target_is_one_dynamic_row(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime, num_proxies=25, country_code=["US", "DE"])
        target = provider.proxy_target([])
        assert target.dynamic and target.total == 1 and target.countries == ["US", "DE"]
        assert target.per_country is None and target.on_demand == []
        assert target.exit_sample_percent == 5  # descriptor default
        assert _oxylabs(builtins, runtime, exit_sample_percent=100).proxy_target([]).exit_sample_percent == 100
        assert _oxylabs(builtins, runtime, exit_sample_percent="250").exit_sample_percent == 100  # clamped
        assert _oxylabs(builtins, runtime, exit_sample_percent="junk").exit_sample_percent == 5

    async def test_provision_country_is_a_no_op(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime)
        assert await provider.provision_country([], "DE") == []

    async def test_pool_mode_is_unchanged(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime, session_mode="pool", num_proxies=2)
        assert not provider.is_dynamic and provider.accepts_request_country()
        to_add, _ = await provider.sync_proxies([])
        assert len(to_add) == 2 and all(META_DYNAMIC_SESSIONS not in p.metadata for p in to_add)


class TestRenderRequest:
    @pytest.fixture
    async def gateway(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> Proxy:
        to_add, _ = await _oxylabs(builtins, runtime).sync_proxies([])
        return to_add[0]

    async def test_client_session_derives_the_same_vendor_session(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, gateway: Proxy) -> None:
        provider = _oxylabs(builtins, runtime)
        first = provider.render_request(gateway, sessid="order-123", country=None, scope="proj-1")
        second = provider.render_request(gateway, sessid="order-123", country=None, scope="proj-1")
        assert first.username == second.username
        assert re.fullmatch(r"customer-alice-sessid-[a-z0-9]{12}-sesstime-10", first.username or "")
        assert "order-123" not in (first.username or "")  # the client's string never reaches the vendor
        assert first.metadata[META_SESSION_ID] in (first.username or "")
        assert first.metadata[META_DYNAMIC_SESSIONS] == "true"
        assert first.metadata[META_EXIT_SAMPLE_PERCENT] == 5  # preflight reads the share off the request
        assert first.password == "{password}"  # secrets stay for the proxy manager
        assert first.id == gateway.id and gateway.username != first.username  # the stored row is untouched
        other_project = provider.render_request(gateway, sessid="order-123", country=None, scope="proj-2")
        assert other_project.username != first.username

    async def test_no_client_session_rotates(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, gateway: Proxy) -> None:
        provider = _oxylabs(builtins, runtime)
        rendered = [provider.render_request(gateway, sessid=None, country=None, scope="p") for _ in range(5)]
        assert len({r.username for r in rendered}) == 5
        assert all(META_SESSION_ID not in r.metadata for r in rendered)
        assert provider.render_request(gateway, sessid="", country=None, scope="p").metadata.get(META_SESSION_ID) is None

    async def test_requested_country_is_rendered(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, gateway: Proxy) -> None:
        provider = _oxylabs(builtins, runtime)
        rendered = provider.render_request(gateway, sessid="s", country="de", scope="p")
        assert (rendered.username or "").startswith("customer-alice-cc-DE-sessid-")
        assert rendered.metadata[META_GEO] == "DE"
        untargeted = provider.render_request(gateway, sessid="s", country=None, scope="p")
        assert "-cc-" not in (untargeted.username or "") and META_GEO not in untargeted.metadata

    async def test_client_session_holds_one_vendor_session_per_country(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, gateway: Proxy) -> None:
        provider = _oxylabs(builtins, runtime)
        de = provider.render_request(gateway, sessid="order-1", country="de", scope="p")
        us = provider.render_request(gateway, sessid="order-1", country="us", scope="p")
        plain = provider.render_request(gateway, sessid="order-1", country=None, scope="p")
        sessions = {r.metadata[META_SESSION_ID] for r in (de, us, plain)}
        assert len(sessions) == 3  # the vendor is never asked to move a placed session to another country
        again = provider.render_request(gateway, sessid="order-1", country="DE", scope="p")
        assert again.username == de.username  # coming back finds the same session and exit

    async def test_explicit_country_meets_the_allow_list_pick(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime, country_code=["US", "DE"])
        gateway = (await provider.sync_proxies([]))[0][0]
        picked = provider.render_request(gateway, sessid="order-2", country=None, scope="p")
        explicit = provider.render_request(gateway, sessid="order-2", country=picked.metadata[META_GEO], scope="p")
        assert explicit.username == picked.username

    async def test_allow_list_picks_a_listed_country_when_none_requested(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, gateway: Proxy) -> None:
        provider = _oxylabs(builtins, runtime, country_code=["US", "DE"])
        seen = {provider.render_request(gateway, sessid=None, country=None, scope="p").metadata[META_GEO] for _ in range(40)}
        assert seen <= {"US", "DE"} and len(seen) == 2
        assert provider.render_request(gateway, sessid=None, country="us", scope="p").metadata[META_GEO] == "US"
        # An unlisted request never gets here (routing filters the connector) but renders nothing rather than lying.
        outside = provider.render_request(gateway, sessid=None, country="FR", scope="p")
        assert META_GEO not in outside.metadata and "-cc-" not in (outside.username or "")

    async def test_password_encoded_vendor(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        credential = make_credential("iproyal", {"username": "royal", "password": "pw"})
        connector = make_connector("iproyal", {"session_mode": "dynamic", "session_lifetime": "30m"})
        provider = DescriptorProvider(builtins["iproyal"], connector, credential, runtime)
        to_add, _ = await provider.sync_proxies([])
        rendered = provider.render_request(to_add[0], sessid="abc", country="gb", scope="p")
        assert rendered.username == "royal"
        assert re.fullmatch(r"\{password\}_country-gb_session-[a-z0-9]{8}_lifetime-30m", rendered.password or "")


def test_validation_hides_num_proxies_when_dynamic() -> None:
    registry = build_registry(include_plugins=False)
    credential = {"proxy_type": "residential", "username": "u", "password": "p"}
    dynamic = registry.validate_connector_config("oxylabs", {"session_mode": "dynamic"}, credential)
    assert dynamic == {"session_mode": "dynamic", "exit_sample_percent": 5, "session_duration_minutes": 10}
    with pytest.raises(ConfigValidationError):
        registry.validate_connector_config("oxylabs", {"session_mode": "dynamic", "exit_sample_percent": 101}, credential)
    pool = registry.validate_connector_config("oxylabs", {}, credential)
    assert pool == {"num_proxies": 1, "session_mode": "pool", "session_duration_minutes": 10}
    # Port-based types never see the field.
    isp = registry.validate_connector_config("oxylabs", {}, {**credential, "proxy_type": "isp"})
    assert isp == {"num_proxies": 1}


class TestReviewFindings:
    async def test_pool_connector_drops_a_leftover_gateway(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        """Switching dynamic back to pool must not keep the gateway as an always-healthy, any-country slot."""
        gateway = Proxy(id="gw", host="pr.oxylabs.io", port=7777, connector_id="conn-1", status=ProxyStatus.HEALTHY,
                        metadata={META_DYNAMIC_SESSIONS: "true", META_SESSION_ID: "x"})
        # Ungeo-targeted pool.
        provider = _oxylabs(builtins, runtime, session_mode="pool", num_proxies=2)
        to_add, to_remove = await provider.sync_proxies([gateway])
        assert to_remove == ["gw"] and len(to_add) == 2
        # Per-country pool.
        provider = _oxylabs(builtins, runtime, session_mode="pool", num_proxies=1, country_code=["US"])
        to_add, to_remove = await provider.sync_proxies([gateway])
        assert to_remove == ["gw"] and len(to_add) == 1 and to_add[0].metadata[META_GEO] == "US"

    async def test_client_session_keeps_one_allow_list_country(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = _oxylabs(builtins, runtime, country_code=["US", "DE", "GB", "FR"])
        gateway = (await provider.sync_proxies([]))[0][0]
        countries = {provider.render_request(gateway, sessid="order-9", country=None, scope="p").metadata[META_GEO] for _ in range(30)}
        assert len(countries) == 1
        # Different sessions spread over the list; rotating requests too.
        spread = {provider.render_request(gateway, sessid=f"s{i}", country=None, scope="p").metadata[META_GEO] for i in range(60)}
        assert len(spread) > 1

    async def test_allow_list_edits_move_few_sessions(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        """Adding a country moves only the sessions that land on it; removing one moves only its own."""
        sessions = [f"order-{i}" for i in range(400)]

        def picks(countries: list[str]) -> dict[str, str]:
            provider = _oxylabs(builtins, runtime, country_code=countries)
            gateway = Proxy(id="gw", host="h", port=1, connector_id="conn-1", metadata={META_DYNAMIC_SESSIONS: "true"})
            return {s: provider.render_request(gateway, sessid=s, country=None, scope="p").metadata[META_GEO] for s in sessions}

        three = picks(["US", "DE", "GB"])
        four = picks(["US", "DE", "GB", "FR"])
        moved = [s for s in sessions if three[s] != four[s]]
        assert moved and all(four[s] == "FR" for s in moved)  # only sessions now on FR changed
        assert len(moved) < len(sessions) // 2
        reordered = picks(["GB", "US", "DE"])
        assert reordered == three  # list order is irrelevant
        removed = picks(["US", "GB"])
        assert all(removed[s] == three[s] for s in sessions if three[s] != "DE")

    async def test_host_and_port_templates_follow_the_request(self, runtime: SdkRuntime) -> None:
        from api.providers.sdk.loader import descriptor_from_dict
        spec = {
            "id": "geohost", "name": "Geo host", "proxy_type_field": None,
            "credential_fields": [{"key": "username", "label": "U", "required": True}, {"key": "password", "label": "P", "type": "password", "secret": True, "required": True}],
            "connector_fields": [
                {"key": "session_mode", "label": "S", "type": "select", "default": "pool", "options": [{"value": "pool", "label": "p"}, {"value": "dynamic", "label": "d"}]},
                {"key": "country_code", "label": "C", "type": "country"},
                {"key": "num_proxies", "label": "N", "type": "number", "default": 1},
            ],
            "proxy_types": [{
                "key": "res", "label": "Res", "mode": "session",
                "host": "{connector.country_code|lower|or:any}.gw.example", "port": "{connector.country_code|or:9000}",
                "username": "{credential.username}-{session_id}", "password": "{credential.password}",
            }],
        }
        # A port template that is a country would not be a number; use a port that depends on nothing but keep the host geo-aware.
        spec["proxy_types"][0]["port"] = 9000
        descriptor = descriptor_from_dict(spec)
        credential = make_credential("geohost", {"username": "u", "password": "p"})
        provider = DescriptorProvider(descriptor, make_connector("geohost", {"session_mode": "dynamic"}), credential, runtime)
        gateway = (await provider.sync_proxies([]))[0][0]
        assert gateway.host == "any.gw.example"
        rendered = provider.render_request(gateway, sessid="s", country="de", scope="p")
        assert rendered.host == "de.gw.example" and rendered.port == 9000

    def test_long_derived_ids_have_no_fixed_padding(self) -> None:
        generator = SessionIdGenerator(SessionIdSpec(length=64, alphabet="alnum"))
        ids = [generator.derive(f"seed-{i}") for i in range(20)]
        assert all(len(i) == 64 for i in ids)
        tails = {i[-10:] for i in ids}
        assert len(tails) == 20  # every tail differs: no shared padding
