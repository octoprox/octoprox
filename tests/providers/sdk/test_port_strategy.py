# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Port-mode provisioning: Oxylabs sequential ports and Bright Data pinned IPs."""

from collections.abc import Callable

import httpx

from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider
from tests.providers.sdk.conftest import MockVendor, json_response, make_connector, make_credential


def _discovery_by_port(ips: dict[int, str | None]) -> Callable[[MockVendor], Callable[[httpx.Request], httpx.Response]]:
    """Build a discovery handler that answers according to the proxy port in use."""

    def factory(vendor: MockVendor) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            proxy_url, _ = vendor.discovery_requests[-1]
            port = int(proxy_url.rsplit(":", 1)[1])
            ip = ips.get(port)
            if ip is None:
                return httpx.Response(502)
            return json_response({"ip": ip})

        return handler

    return factory


class TestOxylabsSequentialPorts:
    def _provider(self, builtins: dict[str, ProviderDescriptor], vendor: MockVendor, num_proxies: int) -> DescriptorProvider:
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        connector = make_connector("oxylabs", {"num_proxies": num_proxies})
        return DescriptorProvider(builtins["oxylabs"], connector, credential, vendor.runtime())

    async def test_discovers_ip_per_sequential_port(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _discovery_by_port({8001: "1.1.1.1", 8002: "2.2.2.2", 8003: "3.3.3.3"})(vendor)
        provider = self._provider(builtins, vendor, 3)
        assert not provider.is_session_based()
        to_add, to_remove = await provider.sync_proxies([])
        assert to_remove == []
        assert [(p.port, p.display_host) for p in to_add] == [(8001, "1.1.1.1"), (8002, "2.2.2.2"), (8003, "3.3.3.3")]
        for proxy in to_add:
            assert proxy.host == "isp.oxylabs.io"
            assert proxy.username == "user-alice" and proxy.password == "{password}"
            assert proxy.status == ProxyStatus.HEALTHY
            assert proxy.metadata["discovered_ip"] == proxy.display_host
            assert proxy.metadata["port"] == str(proxy.port)
        # Discovery went through the proxy with the secret resolved, to the vendor's IP endpoint.
        proxy_url, request = vendor.discovery_requests[0]
        assert proxy_url == "http://user-alice:pw@isp.oxylabs.io:8001"
        assert str(request.url) == "https://ip.oxylabs.io/location"

    async def test_stops_at_first_failing_port(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _discovery_by_port({8001: "1.1.1.1", 8002: None, 8003: "3.3.3.3"})(vendor)
        to_add, _ = await self._provider(builtins, vendor, 3).sync_proxies([])
        assert [p.port for p in to_add] == [8001]

    async def test_skips_duplicate_ip_and_fills_missing_ports_only(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _discovery_by_port({8002: "1.1.1.1", 8003: "3.3.3.3", 8004: "4.4.4.4"})(vendor)
        existing = [
            Proxy(id="p1", host="isp.oxylabs.io", port=8001, connector_id="conn-1", metadata={"discovered_ip": "1.1.1.1"}),
            Proxy(id="old", host="isp.oxylabs.io", port=8009, connector_id="conn-1", metadata={"discovered_ip": "9.9.9.9"}),
        ]
        to_add, to_remove = await self._provider(builtins, vendor, 4).sync_proxies(existing)
        assert to_remove == ["old"]  # beyond the target range
        assert [p.port for p in to_add] == [8003, 8004]  # 8002 duplicated an existing IP

    async def test_refresh_updates_changed_ips_and_dedups(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _discovery_by_port({8001: "1.1.1.1", 8002: "1.1.1.1", 8003: "9.9.9.9"})(vendor)
        proxies = [
            Proxy(id="a", host="isp.oxylabs.io", port=8001, connector_id="conn-1", metadata={"discovered_ip": "1.1.1.1"}),
            Proxy(id="b", host="isp.oxylabs.io", port=8002, connector_id="conn-1", metadata={"discovered_ip": "2.2.2.2"}),
            Proxy(id="c", host="isp.oxylabs.io", port=8003, connector_id="conn-1", metadata={"discovered_ip": "3.3.3.3"}),
        ]
        updated, to_remove = await self._provider(builtins, vendor, 3).refresh_ips(proxies)
        assert to_remove == ["b"]
        assert [(p.id, p.metadata["discovered_ip"]) for p in updated] == [("a", "1.1.1.1"), ("c", "9.9.9.9")]
        assert updated[1].display_host == "9.9.9.9"


class TestBrightDataPinnedIps:
    def _provider(self, builtins: dict[str, ProviderDescriptor], vendor: MockVendor, connector_config: dict[str, object]) -> DescriptorProvider:
        credential = make_credential("brightdata", {"token": "T", "customer_id": "c_123"})
        connector = make_connector("brightdata", {"zone_name": "isp_zone", "zone_password": "zp", "proxy_type": "isp", **connector_config})
        return DescriptorProvider(builtins["brightdata"], connector, credential, vendor.runtime())

    async def test_assigns_known_ips_from_route_ips_api(self, builtins: dict[str, ProviderDescriptor]) -> None:
        def api(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/zone/route_ips"
            assert request.headers["Authorization"] == "Bearer T"
            assert request.url.params["zone"] == "isp_zone"
            assert request.url.params["country"] == "us"
            return json_response([{"ip": "5.5.5.5", "country": "us"}, {"ip": "6.6.6.6", "country": "us"}, {"ip": "7.7.7.7", "country": "us"}])

        vendor = MockVendor(api_handler=api)
        to_add, to_remove = await self._provider(builtins, vendor, {"num_proxies": 2, "country_code": "US"}).sync_proxies([])
        assert to_remove == []
        assert [p.display_host for p in to_add] == ["5.5.5.5", "6.6.6.6"]
        for index, proxy in enumerate(to_add):
            assert proxy.host == "brd.superproxy.io" and proxy.port == 44445
            assert proxy.username == f"brd-customer-c_123-zone-isp_zone-ip-{proxy.display_host}-country-us"
            assert proxy.password == "{zone_password}"
            assert proxy.metadata["hashed_ip"] == proxy.display_host
            assert proxy.metadata["country"] == "us"
            assert proxy.metadata["index"] == str(index)
            assert proxy.status == ProxyStatus.HEALTHY
        assert vendor.discovery_requests == []  # no per-proxy discovery needed

    async def test_falls_back_to_discovery_when_api_empty(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response([]))
        ips = iter(["8.8.8.8", "8.8.8.8", "9.9.9.9"])
        vendor.discovery_handler = lambda r: json_response({"ip": next(ips)})
        to_add, _ = await self._provider(builtins, vendor, {"num_proxies": 2}).sync_proxies([])
        assert [p.display_host for p in to_add] == ["8.8.8.8", "9.9.9.9"]
        assert to_add[1].username == "brd-customer-c_123-zone-isp_zone-ip-9.9.9.9"
        proxy_url, request = vendor.discovery_requests[0]
        # The pre-discovery username has no ip- segment and the zone password is resolved.
        assert proxy_url == "http://brd-customer-c_123-zone-isp_zone:zp@brd.superproxy.io:44445"
        assert str(request.url) == "https://lumtest.com/myip.json"

    async def test_refresh_removes_ips_no_longer_offered(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response([{"ip": "5.5.5.5", "country": "de"}]))
        proxies = [
            Proxy(id="keep", host="brd.superproxy.io", port=44445, connector_id="conn-1", metadata={"discovered_ip": "5.5.5.5", "country": "us"}),
            Proxy(id="gone", host="brd.superproxy.io", port=44445, connector_id="conn-1", metadata={"discovered_ip": "6.6.6.6"}),
        ]
        updated, to_remove = await self._provider(builtins, vendor, {"num_proxies": 2}).refresh_ips(proxies)
        assert to_remove == ["gone"]
        assert [p.id for p in updated] == ["keep"]
        assert updated[0].metadata["country"] == "de"

    async def test_scale_down_fixed_strategy(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response([]))
        proxies = [
            Proxy(id="ok", host="brd.superproxy.io", port=44445, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "1.1.1.1"}),
            Proxy(id="bad", host="brd.superproxy.io", port=44445, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, metadata={"discovered_ip": "2.2.2.2"}),
        ]
        to_add, to_remove = await self._provider(builtins, vendor, {"num_proxies": 1}).sync_proxies(proxies)
        assert to_add == [] and to_remove == ["bad"]

    async def test_residential_zone_uses_global_sessions(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response([]))
        credential = make_credential("brightdata", {"token": "T", "customer_id": "c_123"})
        connector = make_connector("brightdata", {"zone_name": "res", "zone_password": "zp", "proxy_type": "residential", "num_proxies": 1, "country_code": "GB"})
        provider = DescriptorProvider(builtins["brightdata"], connector, credential, vendor.runtime())
        assert provider.is_session_based()
        to_add, _ = await provider.sync_proxies([])
        username = to_add[0].username or ""
        assert username.startswith("brd-customer-c_123-zone-res-session-glob_")
        assert username.endswith("-country-gb")
        assert to_add[0].metadata["session_id"].startswith("glob_")


class TestDecodoGatewayPorts:
    async def test_isp_uses_sticky_ports_with_vendor_ip_endpoint(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        ips = iter(["5.5.5.1", "5.5.5.2"])
        vendor.discovery_handler = lambda r: json_response({"proxy": {"ip": next(ips)}, "country": {"code": "DE"}})
        credential = make_credential("decodo", {"proxy_type": "isp", "username": "smith", "password": "pw"})
        connector = make_connector("decodo", {"num_proxies": 2, "country_code": "DE"})
        to_add, _ = await DescriptorProvider(builtins["decodo"], connector, credential, vendor.runtime()).sync_proxies([])
        assert [(p.host, p.port, p.display_host) for p in to_add] == [("isp.decodo.com", 10001, "5.5.5.1"), ("isp.decodo.com", 10002, "5.5.5.2")]
        assert to_add[0].username == "user-smith-country-de" and to_add[0].password == "{password}"
        proxy_url, request = vendor.discovery_requests[0]
        assert proxy_url == "http://user-smith-country-de:pw@isp.decodo.com:10001"
        assert str(request.url) == "https://ip.decodo.com/json"
