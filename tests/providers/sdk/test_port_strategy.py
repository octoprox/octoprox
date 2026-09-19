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
        # "a" is unchanged and therefore not reported; only "c" moved to a new IP.
        assert [(p.id, p.metadata["discovered_ip"]) for p in updated] == [("c", "9.9.9.9")]
        assert updated[0].display_host == "9.9.9.9"


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
            # The pinned IP fixes the exit, so no country is sent alongside it.
            assert proxy.username == f"brd-customer-c_123-zone-isp_zone-ip-{proxy.display_host}"
            assert proxy.password == "{zone_password}"
            assert proxy.metadata["hashed_ip"] == proxy.display_host
            assert proxy.metadata["country"] == "US"  # vendor said "us"; stored upper-case
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
        assert updated[0].metadata["country"] == "DE"  # vendor said "de"; stored upper-case

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
        connector = make_connector("decodo", {"num_proxies": 2})
        to_add, _ = await DescriptorProvider(builtins["decodo"], connector, credential, vendor.runtime()).sync_proxies([])
        assert [(p.host, p.port, p.display_host) for p in to_add] == [("isp.decodo.com", 10001, "5.5.5.1"), ("isp.decodo.com", 10002, "5.5.5.2")]
        assert to_add[0].username == "user-smith" and to_add[0].password == "{password}"
        assert to_add[0].metadata["country"] == "DE"
        proxy_url, request = vendor.discovery_requests[0]
        assert proxy_url == "http://user-smith:pw@isp.decodo.com:10001"
        assert str(request.url) == "https://ip.decodo.com/json"

    async def test_isp_listed_countries_filter_discovery_like_oxylabs(self, builtins: dict[str, ProviderDescriptor]) -> None:
        """Each port is pinned to an IP and its location, so the country never goes in the username."""
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        slots = {10001: ("5.5.5.1", "FR"), 10002: ("5.5.5.2", "DE"), 10003: ("5.5.5.3", "DE")}

        def handler(request: httpx.Request) -> httpx.Response:
            port = int(vendor.discovery_requests[-1][0].rsplit(":", 1)[1])
            ip, country = slots[port]
            return json_response({"proxy": {"ip": ip}, "country": {"code": country}})

        vendor.discovery_handler = handler
        credential = make_credential("decodo", {"proxy_type": "isp", "username": "smith", "password": "pw"})
        provider = DescriptorProvider(builtins["decodo"], make_connector("decodo", {"num_proxies": 1, "country_code": ["DE"]}), credential, vendor.runtime())
        assert provider.country_key is None and provider.filter_countries == ["DE"]
        to_add, _ = await provider.sync_proxies([])
        assert [(p.port, p.display_host, p.metadata["country"]) for p in to_add] == [(10002, "5.5.5.2", "DE")]
        assert all(p.username == "user-smith" for p in to_add)


class TestDiscoveryCountry:
    async def test_oxylabs_discovery_records_country(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = lambda r: json_response({"ip": "1.1.1.1", "providers": {"maxmind": {"country": "gb"}}})
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        provider = DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {"num_proxies": 1}), credential, vendor.runtime())
        to_add, _ = await provider.sync_proxies([])
        assert to_add[0].metadata["country"] == "GB"
        assert to_add[0].country == "GB"

    async def test_decodo_discovery_records_country(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = lambda r: json_response({"proxy": {"ip": "5.5.5.1"}, "country": {"code": "DE", "name": "Germany"}})
        credential = make_credential("decodo", {"proxy_type": "isp", "username": "smith", "password": "pw"})
        provider = DescriptorProvider(builtins["decodo"], make_connector("decodo", {"num_proxies": 1}), credential, vendor.runtime())
        to_add, _ = await provider.sync_proxies([])
        assert to_add[0].metadata["country"] == "DE"
        assert "geo" not in to_add[0].metadata


def _geo_discovery_by_port(slots: dict[int, tuple[str, str] | None]) -> Callable[[MockVendor], Callable[[httpx.Request], httpx.Response]]:
    """Discovery handler answering Oxylabs-style ``{ip, providers.maxmind.country}`` per port."""

    def factory(vendor: MockVendor) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            proxy_url, _ = vendor.discovery_requests[-1]
            port = int(proxy_url.rsplit(":", 1)[1])
            slot = slots.get(port)
            if slot is None:
                return httpx.Response(502)
            ip, country = slot
            return json_response({"ip": ip, "providers": {"maxmind": {"country": country}}})

        return handler

    return factory


class TestOxylabsCountryFilteredPorts:
    """Oxylabs ISP cannot geo-target its username, so listed countries filter discovery."""

    def _provider(self, builtins: dict[str, ProviderDescriptor], vendor: MockVendor, config: dict[str, object]) -> DescriptorProvider:
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        return DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", config), credential, vendor.runtime())

    async def test_keeps_only_listed_countries_per_country_count(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({
            8001: ("1.1.1.1", "FR"), 8002: ("2.2.2.2", "US"), 8003: ("3.3.3.3", "DE"), 8004: ("4.4.4.4", "US"), 8005: ("5.5.5.5", "DE"),
        })(vendor)
        provider = self._provider(builtins, vendor, {"num_proxies": 1, "country_code": ["US", "DE"]})
        assert provider.filter_countries == ["US", "DE"]
        to_add, to_remove = await provider.sync_proxies([])
        assert to_remove == []
        assert [(p.port, p.display_host, p.metadata["country"]) for p in to_add] == [(8002, "2.2.2.2", "US"), (8003, "3.3.3.3", "DE")]
        # Stopped once both countries were satisfied: port 8004 was never probed.
        probed = [int(url.rsplit(":", 1)[1]) for url, _ in vendor.discovery_requests]
        assert probed == [8001, 8002, 8003]

    async def test_scans_past_target_to_fill_countries(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({
            8001: ("1.1.1.1", "FR"), 8002: ("2.2.2.2", "FR"), 8003: ("3.3.3.3", "FR"), 8004: ("4.4.4.4", "US"), 8005: ("5.5.5.5", "US"),
        })(vendor)
        provider = self._provider(builtins, vendor, {"num_proxies": 2, "country_code": ["US"]})
        to_add, _ = await provider.sync_proxies([])
        assert [p.port for p in to_add] == [8004, 8005]

    async def test_stops_when_ports_run_out(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        # Only one US port exists; the rest fail (allocation exhausted).
        vendor.discovery_handler = _geo_discovery_by_port({8001: ("1.1.1.1", "US")})(vendor)
        provider = self._provider(builtins, vendor, {"num_proxies": 3, "country_code": ["US"]})
        to_add, _ = await provider.sync_proxies([])
        assert [p.port for p in to_add] == [8001]
        # Oxylabs allows one consecutive failure before giving up.
        assert len(vendor.discovery_requests) == 2

    async def test_stops_on_repeated_known_ips(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({p: ("1.1.1.1", "US") for p in range(8001, 8020)})(vendor)
        existing = [Proxy(id="have", host="isp.oxylabs.io", port=8001, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "1.1.1.1", "country": "US"})]
        provider = self._provider(builtins, vendor, {"num_proxies": 2, "country_code": ["US"]})
        to_add, to_remove = await provider.sync_proxies(existing)
        assert to_add == [] and to_remove == []
        duplicates_limit = builtins["oxylabs"].get_proxy_type("isp").discovery.max_consecutive_duplicates  # type: ignore[union-attr]
        assert len(vendor.discovery_requests) == duplicates_limit

    async def test_removes_proxies_outside_listed_countries(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({8001: ("1.1.1.1", "FR"), 8002: ("2.2.2.2", "US"), 8003: ("3.3.3.3", "DE")})(vendor)
        existing = [
            Proxy(id="fr", host="isp.oxylabs.io", port=8001, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "1.1.1.1", "country": "FR"}),
            Proxy(id="us", host="isp.oxylabs.io", port=8002, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "2.2.2.2", "country": "US"}),
        ]
        provider = self._provider(builtins, vendor, {"num_proxies": 1, "country_code": ["US", "DE"]})
        to_add, to_remove = await provider.sync_proxies(existing)
        assert to_remove == ["fr"]
        assert [(p.port, p.metadata["country"]) for p in to_add] == [(8003, "DE")]
        # Port 8002 is already held, so the scan skipped it.
        probed = [int(url.rsplit(":", 1)[1]) for url, _ in vendor.discovery_requests]
        assert probed == [8001, 8003]

    async def test_no_countries_keeps_plain_sequential_behaviour(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({8001: ("1.1.1.1", "FR"), 8002: ("2.2.2.2", "US")})(vendor)
        provider = self._provider(builtins, vendor, {"num_proxies": 2})
        assert provider.filter_countries is None
        to_add, _ = await provider.sync_proxies([])
        assert [(p.port, p.metadata["country"]) for p in to_add] == [(8001, "FR"), (8002, "US")]


class TestFixedPortCountryFilter:
    """Fixed-port types without a country in the username pick wanted-country IPs from the vendor list."""

    @staticmethod
    def _descriptor() -> ProviderDescriptor:
        return ProviderDescriptor.model_validate({
            "id": "pinned",
            "name": "Pinned",
            "description": "fixed port, known ips, no country in username",
            "credential_fields": [{"key": "token", "label": "Token", "secret": True, "required": True}],
            "connector_fields": [
                {"key": "num_proxies", "label": "N", "type": "number", "default": 1},
                {"key": "countries", "label": "Countries", "type": "country"},
            ],
            "proxy_types": [{
                "key": "isp",
                "label": "ISP",
                "mode": "port",
                "port_strategy": "fixed",
                "host": "gw.pinned.test",
                "port": 9000,
                "username": {"separator": "-", "parts": [{"text": "user"}, {"text": "ip-{discovered_ip}", "when": {"field": "discovered_ip"}}]},
                "password": "{credential.token}",
                "discovery": {"url": "https://ip.pinned.test/json", "ip_path": "ip", "country_path": "country"},
                "known_ips": {"call": {"url": "https://api.pinned.test/ips", "headers": {"Authorization": "{credential.token}"}}, "items": "@", "ip": "ip", "country": "country"},
            }],
        })

    async def test_picks_known_ips_per_country(self) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response([
            {"ip": "1.1.1.1", "country": "fr"}, {"ip": "2.2.2.2", "country": "us"}, {"ip": "3.3.3.3", "country": "us"}, {"ip": "4.4.4.4", "country": "de"}, {"ip": "5.5.5.5", "country": "us"},
        ]))
        provider = DescriptorProvider(self._descriptor(), make_connector("pinned", {"num_proxies": 2, "countries": ["US", "DE"]}), make_credential("pinned", {"token": "t"}), vendor.runtime())
        to_add, to_remove = await provider.sync_proxies([])
        assert to_remove == []
        assert sorted((p.display_host, p.metadata["country"]) for p in to_add) == [("2.2.2.2", "US"), ("3.3.3.3", "US"), ("4.4.4.4", "DE")]
        assert all(p.username == f"user-ip-{p.display_host}" for p in to_add)


class TestRefreshDropsRelocatedProxies:
    """Correctness first: a proxy whose exit moved out of its country is removed and replaced."""

    async def test_filtered_oxylabs_isp_drops_relocated_ip_and_reconciles(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        # Port 8001 used to be US; it now exits from FR. Port 8002 is a fresh US IP.
        vendor.discovery_handler = _geo_discovery_by_port({8001: ("1.1.1.1", "FR"), 8002: ("2.2.2.2", "US")})(vendor)
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        provider = DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {"num_proxies": 1, "country_code": ["US"]}), credential, vendor.runtime())
        assert provider.needs_periodic_sync()
        held = Proxy(id="p1", host="isp.oxylabs.io", port=8001, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "1.1.1.1", "country": "US"})

        updated, to_remove = await provider.refresh_ips([held])
        assert updated == [] and to_remove == ["p1"]

        # The syncer reconciles right after: a replacement is discovered on the next port.
        to_add, _ = await provider.sync_proxies([])
        assert [(p.port, p.metadata["country"]) for p in to_add] == [(8002, "US")]

    async def test_group_slot_drops_vendor_mismatch(self, builtins: dict[str, ProviderDescriptor]) -> None:
        # Bright Data ISP pins IPs per country group; the vendor list now places the IP elsewhere.
        vendor = MockVendor(api_handler=lambda r: json_response([{"ip": "5.5.5.1", "country": "nl"}]))
        credential = make_credential("brightdata", {"token": "T", "customer_id": "c_123"})
        connector = make_connector("brightdata", {"zone_name": "isp_zone", "zone_password": "zp", "proxy_type": "isp", "num_proxies": 1, "country_code": ["DE"]})
        provider = DescriptorProvider(builtins["brightdata"], connector, credential, vendor.runtime())
        assert provider.needs_periodic_sync()
        held = Proxy(
            id="de1", host="brd.superproxy.io", port=44445, connector_id="conn-1", status=ProxyStatus.HEALTHY,
            username="brd-customer-c_123-zone-isp_zone-ip-5.5.5.1", password="{zone_password}",
            metadata={"discovered_ip": "5.5.5.1", "hashed_ip": "5.5.5.1", "country": "de", "geo": "DE", "index": "0"},
        )
        updated, to_remove = await provider.refresh_ips([held])
        assert updated == [] and to_remove == ["de1"]

    async def test_unconstrained_port_type_keeps_relocated_proxy(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({8001: ("1.1.1.1", "FR")})(vendor)
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        provider = DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {"num_proxies": 1}), credential, vendor.runtime())
        assert not provider.needs_periodic_sync()
        held = Proxy(id="p1", host="isp.oxylabs.io", port=8001, connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"discovered_ip": "1.1.1.1", "country": "US"})
        updated, to_remove = await provider.refresh_ips([held])
        assert to_remove == [] and updated[0].metadata["country"] == "FR"


class TestRefreshReportsOnlyChanges:
    async def test_stable_pool_reports_nothing_and_one_stale_country_is_fixed(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({p: (f"1.1.1.{p - 8000}", "US") for p in range(8001, 8011)})(vendor)
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        provider = DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {"num_proxies": 10}), credential, vendor.runtime())
        proxies = [
            Proxy(id=f"p{p}", host="isp.oxylabs.io", port=p, connector_id="conn-1", status=ProxyStatus.HEALTHY,
                  metadata={"discovered_ip": f"1.1.1.{p - 8000}", "country": "US"})
            for p in range(8001, 8011)
        ]
        proxies[3].metadata["country"] = "CA"  # stale location, as after a release that added country_path

        updated, to_remove = await provider.refresh_ips(proxies)

        assert len(vendor.discovery_requests) == 10  # every proxy probed
        assert to_remove == []
        assert [p.id for p in updated] == ["p8004"] and updated[0].metadata["country"] == "US"

    async def test_failed_probe_leaves_proxy_untouched(self, builtins: dict[str, ProviderDescriptor]) -> None:
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        vendor.discovery_handler = _geo_discovery_by_port({8001: None, 8002: ("2.2.2.2", "DE")})(vendor)
        credential = make_credential("oxylabs", {"proxy_type": "isp", "username": "alice", "password": "pw"})
        provider = DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {"num_proxies": 2}), credential, vendor.runtime())
        proxies = [
            Proxy(id="a", host="isp.oxylabs.io", port=8001, connector_id="conn-1", metadata={"discovered_ip": "1.1.1.1", "country": "US"}),
            Proxy(id="b", host="isp.oxylabs.io", port=8002, connector_id="conn-1", metadata={"discovered_ip": "2.2.2.2"}),
        ]
        updated, to_remove = await provider.refresh_ips(proxies)
        assert to_remove == []
        assert [p.id for p in updated] == ["b"] and updated[0].metadata["country"] == "DE"
        assert proxies[0].metadata == {"discovered_ip": "1.1.1.1", "country": "US"}
