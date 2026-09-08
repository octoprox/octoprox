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
from api.providers.sdk.provider import SdkRuntime
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
        assert by_value["dc_zone"].description == "datacenter (dc_shared) - 2 IPs"
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


def _ip_echo(ip: str) -> httpx.Response:
    return json_response({"origin": ip})


class TestProxyRequest:
    """``proxy_request``: provision one endpoint in memory and fetch a URL through it."""

    async def test_session_mode_sends_request_through_gateway(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=lambda r: _ip_echo("9.9.9.9"))
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request",
            {"proxy_type": "residential", "username": "alice", "password": "pw"},
            {"num_proxies": 25, "country_code": "US"},
        )
        assert outcome.ok, outcome.message
        assert outcome.message.startswith("HTTP 200 through pr.oxylabs.io:7777 in") and "exit IP 9.9.9.9" in outcome.message
        # Exactly one request, through the fully resolved proxy URL, to the default healthcheck URL.
        assert len(vendor.discovery_requests) == 1
        proxy_url, request = vendor.discovery_requests[0]
        assert proxy_url.startswith("http://customer-alice-cc-US-sessid-") and proxy_url.endswith(":pw@pr.oxylabs.io:7777")
        assert str(request.url) == "https://httpbin.org/ip"
        result = outcome.result
        assert result["proxy"]["host"] == "pr.oxylabs.io" and result["proxy"]["port"] == 7777
        assert result["proxy"]["username"].startswith("customer-alice-cc-US-sessid-")
        assert "password" not in result["proxy"]  # never echoed, even as a placeholder
        assert result["proxy"]["metadata"]["proxy_type"] == "residential"
        assert result["status"] == 200 and result["exit_ip"] == "9.9.9.9" and result["target_url"] == "https://httpbin.org/ip"
        assert [t.as_dict()["url"] for t in outcome.traces] == ["https://httpbin.org/ip"] and outcome.traces[0].status == 200

    async def test_custom_target_url_and_failure_status(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=lambda r: httpx.Response(403, text="blocked"))
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request",
            {"proxy_type": "residential", "username": "alice", "password": "pw"},
            {"num_proxies": 1},
            target_url="https://example.com/",
        )
        assert not outcome.ok
        assert outcome.message == "HTTP 403 from https://example.com/ through pr.oxylabs.io:7777"
        assert str(vendor.discovery_requests[0][1].url) == "https://example.com/"
        assert outcome.result["body"] == "blocked" and outcome.traces[0].status == 403

    async def test_proxy_errors_are_reported_and_redacted(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ProxyError("407 Proxy Authentication Required for alice:pw")

        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=refuse)
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request", {"proxy_type": "residential", "username": "alice", "password": "pw"}, {"num_proxies": 1}
        )
        assert not outcome.ok
        assert outcome.message == "Proxy error through pr.oxylabs.io:7777: 407 Proxy Authentication Required for alice:***"
        assert outcome.traces[0].error == "407 Proxy Authentication Required for alice:***"

    async def test_port_mode_discovers_one_slot_then_requests(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=lambda r: json_response({"ip": "1.1.1.1", "origin": "1.1.1.1"}))
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request", {"proxy_type": "isp", "username": "alice", "password": "pw"}, {"num_proxies": 10}
        )
        assert outcome.ok, outcome.message
        # One discovery call (a single slot despite num_proxies=10), then the test request, both through port 8001.
        assert [url for url, _ in vendor.discovery_requests] == ["http://user-alice:pw@isp.oxylabs.io:8001"] * 2
        assert [str(r.url) for _, r in vendor.discovery_requests] == ["https://ip.oxylabs.io/location", "https://httpbin.org/ip"]
        assert outcome.result["proxy"]["metadata"]["discovered_ip"] == "1.1.1.1"

    async def test_port_mode_explains_failed_discovery(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=lambda r: httpx.Response(502))
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request", {"proxy_type": "isp", "username": "alice", "password": "pw"}, {"num_proxies": 1}
        )
        assert not outcome.ok and "IP discovery through isp.oxylabs.io:8001 failed" in outcome.message

    async def test_list_mode_uses_first_listed_proxy(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def api(request: httpx.Request) -> httpx.Response:
            return json_response({"results": [
                {"id": 1, "proxy_address": "1.1.1.1", "port": 80, "username": "u1", "password": "p1", "valid": True, "country_code": "US"},
                {"id": 2, "proxy_address": "2.2.2.2", "port": 80, "username": "u2", "password": "p2", "valid": True, "country_code": "DE"},
            ], "next": None})

        vendor = MockVendor(api_handler=api, discovery_handler=lambda r: _ip_echo("1.1.1.1"))
        tester = DescriptorTester(builtins["webshare"], vendor.runtime())
        outcome = await tester.run("proxy_request", {"api_key": "K"}, {"mode": "direct", "num_proxies": 50})
        assert outcome.ok, outcome.message
        assert vendor.discovery_requests[0][0] == "http://u1:p1@1.1.1.1:80"
        assert outcome.result["proxy"] == {
            "host": "1.1.1.1", "port": 80, "protocol": "http", "username": "u1",
            "metadata": {"provider": "webshare", "proxy_type": "proxies", "list_identity": "1", "country": "US"},
        }

    async def test_list_mode_surfaces_vendor_errors(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: httpx.Response(401, json={"detail": "bad key"}))
        outcome = await DescriptorTester(builtins["webshare"], vendor.runtime()).run("proxy_request", {"api_key": "K"}, {"mode": "direct"})
        assert not outcome.ok and outcome.traces and outcome.traces[0].status == 401
        assert vendor.discovery_requests == []

    async def test_missing_values_and_bad_targets_are_rejected_before_any_request(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        tester = DescriptorTester(builtins["oxylabs"], vendor.runtime())
        unknown = await tester.run("proxy_request", {"proxy_type": "bogus"}, {})
        assert not unknown.ok and "unknown proxy type" in unknown.message
        # Webshare's list call reads {connector.mode}, which is required.
        missing = await DescriptorTester(builtins["webshare"], vendor.runtime()).run("proxy_request", {"api_key": "K"}, {})
        assert not missing.ok and missing.message == "Missing connector values: mode"
        # A strict egress policy refuses private targets before provisioning anything.
        strict = DescriptorTester(builtins["oxylabs"], SdkRuntime(proxied_client_factory=vendor.runtime().proxied_client_factory))
        private = await strict.run(
            "proxy_request", {"proxy_type": "residential", "username": "a", "password": "p"}, {}, target_url="https://10.0.0.1/"
        )
        assert not private.ok and private.message.startswith("target URL rejected")
        plain = await strict.run("proxy_request", {"proxy_type": "residential", "username": "a", "password": "p"}, {}, target_url="http://example.com/")
        assert not plain.ok and "plain http" in plain.message
        assert vendor.discovery_requests == [] and vendor.api_requests == []

    async def test_captured_credential_values_reach_the_endpoint(self, builtins: dict[str, ProviderDescriptor]) -> None:
        """Bright Data's username needs the customer id, which only the validation call provides."""
        vendor = MockVendor(api_handler=brightdata_api, discovery_handler=lambda r: _ip_echo("1.1.1.1"))
        tester = DescriptorTester(builtins["brightdata"], vendor.runtime())
        outcome = await tester.run(
            "proxy_request",
            {"token": "T"},
            {"zone_name": "isp_zone", "zone_password": "zp", "proxy_type": "isp", "num_proxies": 3, "country_code": "US"},
        )
        assert outcome.ok, outcome.message
        assert outcome.result["proxy"]["username"] == "brd-customer-c_777-zone-isp_zone-ip-1.1.1.1-country-us"
        assert vendor.discovery_requests[0][0] == "http://brd-customer-c_777-zone-isp_zone-ip-1.1.1.1-country-us:zp@brd.superproxy.io:44445"
        # Validation trace first (redacted), then the proxied request.
        assert [t.url for t in outcome.traces] == ["https://api.brightdata.com/status", "https://httpbin.org/ip"]
        assert outcome.traces[0].headers["Authorization"] == "***"

    async def test_failed_validation_stops_before_provisioning(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def api(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/status"
            return json_response({"status": "inactive"})

        vendor = MockVendor(api_handler=api)
        outcome = await DescriptorTester(builtins["brightdata"], vendor.runtime()).run(
            "proxy_request", {"token": "T"}, {"zone_name": "z", "zone_password": "zp", "proxy_type": "isp"}
        )
        assert not outcome.ok and outcome.message == "Invalid Bright Data API token or inactive account"
        assert len(outcome.traces) == 1 and vendor.discovery_requests == []
