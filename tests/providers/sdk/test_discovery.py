# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Options resolution, credential validation and the admin tester."""

import httpx

from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.discovery import (
    CredentialValidator,
    DescriptorTester,
    OptionsCache,
    OptionsResolver,
    ResolvedOption,
)
from tests.providers.sdk.conftest import MockVendor, json_response

ZONES = [
    {"name": "res_zone", "type": "res_rotating"},
    {"name": "isp_zone", "type": "res_static"},
    {"name": "dc_zone", "type": "dc_shared"},
    {"name": "unblocker", "type": "unblocker"},
    {"name": "nopass", "type": "res_rotating"},
]


def brightdata_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer T"
    path = request.url.path
    if path == "/zone/get_active_zones":
        return json_response(ZONES)
    if path == "/zone":
        zone = request.url.params["zone"]
        return json_response({"password": [] if zone == "nopass" else [f"pw-{zone}"]})
    if path == "/zone/route_ips":
        return json_response([{"ip": "1.1.1.1", "country": "us"}, {"ip": "2.2.2.2", "country": "de"}])
    if path == "/status":
        return json_response({"status": "active", "customer": "c_777"})
    raise AssertionError(path)


class TestOptionsResolver:
    async def test_brightdata_zones_are_mapped_enriched_and_filtered(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=brightdata_api)
        resolver = OptionsResolver(builtins["brightdata"], vendor.runtime(), cache=OptionsCache())
        outcome = await resolver.resolve("zones", {"token": "T"})
        assert outcome.ok, outcome.message
        options: list[ResolvedOption] = outcome.result
        by_value = {o.value: o for o in options}
        assert set(by_value) == {"res_zone", "isp_zone", "dc_zone"}  # unblocker unmapped, nopass filtered
        assert by_value["res_zone"].extra["proxy_type"] == "residential"
        assert by_value["res_zone"].extra["password"] == "pw-res_zone"
        assert "total_ips" not in by_value["res_zone"].extra
        assert by_value["isp_zone"].extra["proxy_type"] == "isp"
        assert by_value["isp_zone"].extra["total_ips"] == 2
        assert by_value["dc_zone"].description == "datacenter (dc_shared) — 2 IPs"
        assert by_value["res_zone"].description == "residential (res_rotating)"
        # get_active_zones + 5 passwords + 2 route_ips
        assert len(vendor.api_requests) == 8
        assert all(t.headers.get("Authorization") == "***" for t in outcome.traces)

    async def test_zone_countries_are_grouped_with_counts(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def api(request: httpx.Request) -> httpx.Response:
            assert request.url.params["zone"] == "isp_zone"
            return json_response([{"ip": "1.1.1.1", "country": "us"}, {"ip": "2.2.2.2", "country": "de"}, {"ip": "3.3.3.3", "country": "us"}])

        vendor = MockVendor(api_handler=api)
        resolver = OptionsResolver(builtins["brightdata"], vendor.runtime(), cache=OptionsCache())
        outcome = await resolver.resolve("zone_countries", {"token": "T"}, {"zone_name": "isp_zone"})
        assert outcome.ok, outcome.message
        options: list[ResolvedOption] = outcome.result
        assert [(o.value, o.extra["count"], o.description) for o in options] == [("us", 2, "2 IPs"), ("de", 1, "1 IPs")]

    async def test_results_are_cached(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=brightdata_api)
        resolver = OptionsResolver(builtins["brightdata"], vendor.runtime(), cache=OptionsCache())
        await resolver.resolve("zones", {"token": "T"})
        calls = len(vendor.api_requests)
        second = await resolver.resolve("zones", {"token": "T"})
        assert second.ok and len(vendor.api_requests) == calls and second.traces == []
        third = await resolver.resolve("zones", {"token": "T"}, use_cache=False)
        assert len(vendor.api_requests) > calls and third.ok

    async def test_unknown_source_and_api_failure(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({"detail": "denied"}, 403))
        resolver = OptionsResolver(builtins["brightdata"], vendor.runtime(), cache=OptionsCache())
        assert not (await resolver.resolve("nope", {"token": "T"})).ok
        outcome = await resolver.resolve("zones", {"token": "T"})
        assert not outcome.ok and "HTTP 403" in outcome.message and outcome.traces


class TestCredentialValidator:
    async def test_captures_customer_id(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=brightdata_api)
        validator = CredentialValidator(builtins["brightdata"], vendor.runtime())
        assert validator.enabled and validator.applies({"token": "T"})
        outcome = await validator.validate({"token": "T"})
        assert outcome.ok and outcome.result == {"token": "T", "customer_id": "c_777"}

    async def test_success_predicate_and_status(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({"status": "suspended", "customer": "c"}))
        outcome = await CredentialValidator(builtins["brightdata"], vendor.runtime()).validate({"token": "T"})
        assert not outcome.ok and "Invalid Bright Data" in outcome.message
        vendor = MockVendor(api_handler=lambda r: json_response({}, 401))
        outcome = await CredentialValidator(builtins["brightdata"], vendor.runtime()).validate({"token": "T"})
        assert not outcome.ok and "HTTP 401" in outcome.message

    async def test_conditional_validation_is_skipped_without_token(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}, 500))
        validator = CredentialValidator(builtins["iproyal"], vendor.runtime())
        assert not validator.applies({"username": "u", "password": "p"})
        outcome = await validator.validate({"username": "u", "password": "p"})
        assert outcome.ok and vendor.api_requests == []
        assert validator.applies({"username": "u", "password": "p", "api_token": "x"})

    async def test_provider_without_validation(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        validator = CredentialValidator(builtins["oxylabs"], vendor.runtime())
        assert not validator.enabled
        assert (await validator.validate({"username": "u"})).ok


class TestDescriptorTester:
    async def test_actions(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=brightdata_api)
        tester = DescriptorTester(builtins["brightdata"], vendor.runtime())
        validate = await tester.run("validate", {"token": "T"}, {})
        assert validate.ok and validate.result == {"captured": {"customer_id": "c_777"}}
        options = await tester.run("options", {"token": "T"}, {}, option_name="zones")
        assert options.ok and options.message == "3 option(s)" and len(options.result) == 3
        assert not (await tester.run("options", {"token": "T"}, {})).ok
        # zone_countries needs the zone; say so instead of letting the vendor return 400.
        needs_zone = await tester.run("options", {"token": "T"}, {}, option_name="zone_countries")
        assert not needs_zone.ok and needs_zone.message == "Missing connector values: zone_name"
        with_zone = await tester.run("options", {"token": "T"}, {"zone_name": "isp_zone"}, option_name="zone_countries")
        assert with_zone.ok
        assert not (await tester.run("list_proxies", {"token": "T"}, {"proxy_type": "isp"})).ok
        assert not (await tester.run("bogus", {}, {})).ok
        no_validation = await DescriptorTester(builtins["oxylabs"], vendor.runtime()).run("validate", {}, {})
        assert not no_validation.ok and "no credential validation" in no_validation.message

    async def test_list_proxies_preview(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def api(request: httpx.Request) -> httpx.Response:
            return json_response({"results": [{"id": 1, "proxy_address": "1.1.1.1", "port": 80, "username": "u", "password": "p", "valid": True, "country_code": "US"}], "next": None})

        vendor = MockVendor(api_handler=api)
        outcome = await DescriptorTester(builtins["webshare"], vendor.runtime()).run("list_proxies", {"api_key": "K"}, {"mode": "direct"})
        assert outcome.ok and outcome.message == "1 proxies"
        assert outcome.result == [{"host": "1.1.1.1", "port": 80, "username": "u", "country": "US", "identity": "1"}]
