# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""List-mode provisioning through the Webshare descriptor."""

import httpx

from api.models.proxy import Proxy, ProxyProtocol
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider
from tests.providers.sdk.conftest import MockVendor, json_response, make_connector, make_credential

PAGE_1 = {
    "count": 3,
    "next": "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=2&page_size=100",
    "results": [
        {"id": "a", "username": "u1", "password": "p1", "proxy_address": "1.1.1.1", "port": 8000, "valid": True, "country_code": "US", "city_name": "New York"},
        {"id": "b", "username": "u2", "password": "p2", "proxy_address": "2.2.2.2", "port": 8001, "valid": False, "country_code": "FR"},
    ],
}
PAGE_2 = {
    "count": 3,
    "next": None,
    "results": [
        {"id": "c", "username": "u3", "password": "p3", "proxy_address": "3.3.3.3", "port": 8002, "valid": True, "country_code": "DE"},
    ],
}


def _api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Token KEY"
    assert request.url.params["mode"] == "direct"
    assert "plan_id" not in request.url.params  # empty optional param dropped
    return json_response(PAGE_2 if request.url.params.get("page") == "2" else PAGE_1)


def _provider(builtins: dict[str, ProviderDescriptor], vendor: MockVendor, connector_config: dict[str, object]) -> DescriptorProvider:
    credential = make_credential("webshare", {"api_key": "KEY"})
    connector = make_connector("webshare", {"mode": "direct", **connector_config})
    return DescriptorProvider(builtins["webshare"], connector, credential, vendor.runtime())


async def test_mirrors_paginated_list_with_filter(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_api)
    provider = _provider(builtins, vendor, {})
    assert not provider.is_session_based() and provider.needs_periodic_sync()
    to_add, to_remove = await provider.sync_proxies([])
    assert to_remove == []
    assert [(p.host, p.port, p.username, p.password) for p in to_add] == [
        ("1.1.1.1", 8000, "u1", "p1"),
        ("3.3.3.3", 8002, "u3", "p3"),  # 'b' filtered out: valid == False
    ]
    assert to_add[0].protocol == ProxyProtocol.HTTP
    assert to_add[0].metadata["list_identity"] == "a"
    assert to_add[0].metadata["country"] == "US"
    assert to_add[0].tags == ["webshare"]
    assert len(vendor.api_requests) == 2


async def test_sync_diff_and_cap(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_api)
    existing = [
        Proxy(id="keep", host="1.1.1.1", port=8000, connector_id="conn-1", metadata={"list_identity": "a"}),
        Proxy(id="stale", host="9.9.9.9", port=1, connector_id="conn-1", metadata={"list_identity": "zzz"}),
    ]
    to_add, to_remove = await _provider(builtins, vendor, {}).sync_proxies(existing)
    assert to_remove == ["stale"]
    assert [p.metadata["list_identity"] for p in to_add] == ["c"]

    capped_add, _ = await _provider(builtins, vendor, {"num_proxies": 1}).sync_proxies([])
    assert [p.metadata["list_identity"] for p in capped_add] == ["a"]


async def test_cap_stops_paging_once_reached(builtins: dict[str, ProviderDescriptor]) -> None:
    """A cap the first page fills is one request; one it does not fill reads on, counting only kept entries."""
    one_page = MockVendor(api_handler=_api)
    capped, _ = await _provider(builtins, one_page, {"num_proxies": 1}).sync_proxies([])
    assert [p.metadata["list_identity"] for p in capped] == ["a"]
    assert len(one_page.api_requests) == 1

    two_pages = MockVendor(api_handler=_api)
    capped, _ = await _provider(builtins, two_pages, {"num_proxies": 2}).sync_proxies([])
    assert [p.metadata["list_identity"] for p in capped] == ["a", "c"]  # 'b' is not valid, so page 2 is read
    assert len(two_pages.api_requests) == 2


async def test_cap_cuts_a_page_short(builtins: dict[str, ProviderDescriptor]) -> None:
    """A cap below one page's kept entries mirrors the first of them, from that one page."""
    entries = [
        {"id": name, "username": "u", "password": "p", "proxy_address": f"10.0.0.{n}", "port": 8000 + n, "valid": True}
        for n, name in enumerate(["x", "y", "z"], start=1)
    ]
    vendor = MockVendor(api_handler=lambda request: json_response({"count": 6, "next": PAGE_1["next"], "results": entries}))
    capped, _ = await _provider(builtins, vendor, {"num_proxies": 2}).sync_proxies([])
    assert [p.metadata["list_identity"] for p in capped] == ["x", "y"]
    assert len(vendor.api_requests) == 1


async def test_refresh_and_sync_read_one_listing(builtins: dict[str, ProviderDescriptor]) -> None:
    """A sync refreshes the stored rows and then adds the new ones; the vendor's list is fetched once for both."""
    vendor = MockVendor(api_handler=_api)
    provider = _provider(builtins, vendor, {})
    stored = [Proxy(id="a", host="1.1.1.1", port=8000, username="u1", password="p1", connector_id="conn-1", metadata={"list_identity": "a"})]
    _updated, removed = await provider.refresh_ips(stored)
    to_add, _ = await provider.sync_proxies(stored)
    assert removed == []
    assert [p.metadata["list_identity"] for p in to_add] == ["c"]
    assert len(vendor.api_requests) == 2  # pages 1 and 2, once


async def test_refresh_updates_credentials_in_place(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_api)
    proxies = [
        Proxy(id="a", host="1.1.1.1", port=8000, username="old", password="old", connector_id="conn-1", metadata={"list_identity": "a"}),
        Proxy(id="c", host="3.3.3.3", port=8002, username="u3", password="p3", connector_id="conn-1", metadata={"list_identity": "c", "country": "DE", "vendor_country": "DE"}),
        Proxy(id="gone", host="4.4.4.4", port=1, connector_id="conn-1", metadata={"list_identity": "nope"}),
    ]
    updated, to_remove = await _provider(builtins, vendor, {}).refresh_ips(proxies)
    assert to_remove == ["gone"]
    assert [p.id for p in updated] == ["a"]
    assert (updated[0].username, updated[0].password) == ("u1", "p1")


async def test_listed_city_is_the_vendor_claim(builtins: dict[str, ProviderDescriptor]) -> None:
    """Webshare names a city per IP; it is recorded as the vendor's claim, as its country is."""
    vendor = MockVendor(api_handler=_api)
    to_add, _ = await _provider(builtins, vendor, {}).sync_proxies([])
    by_host = {p.host: p.metadata for p in to_add}
    assert by_host["1.1.1.1"]["vendor_country"] == "US" and by_host["1.1.1.1"]["vendor_city"] == "new_york"
    assert "vendor_city" not in by_host["3.3.3.3"] and by_host["3.3.3.3"]["vendor_country"] == "DE"


def _by_country(request: httpx.Request) -> httpx.Response:
    """Two valid proxies per country asked for, then a next page that must not be read under a cap of 1."""
    country = request.url.params["country_code__in"]
    assert request.url.params.get("page") is None, "the cap was reached on the first page"
    return json_response({
        "next": f"https://proxy.webshare.io/api/v2/proxy/list/?country_code__in={country}&page=2",
        "results": [
            {"id": f"{country}-{n}", "username": "u", "password": "p", "proxy_address": f"10.0.{n}.{len(country)}", "port": 8000 + n, "valid": True, "country_code": country}
            for n in (1, 2)
        ],
    })


async def test_countries_are_the_connector_country_list(builtins: dict[str, ProviderDescriptor]) -> None:
    """The countries dropdown is the connector's country list, so routing and the Countries page know it."""
    provider = _provider(builtins, MockVendor(api_handler=_by_country), {"country_code": ["AU"]})
    assert provider.countries == ["AU"]
    assert provider.geo_targeted and provider.provisions_per_country
    assert not provider.accepts_request_country()


async def test_each_listed_country_is_read_on_its_own_up_to_the_cap(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_by_country)
    to_add, _ = await _provider(builtins, vendor, {"country_code": ["AU", "US"], "num_proxies": 1}).sync_proxies([])
    assert [(p.metadata["list_identity"], p.metadata["geo"]) for p in to_add] == [("AU-1", "AU"), ("US-1", "US")]
    assert [request.url.params["country_code__in"] for request in vendor.api_requests] == ["AU", "US"]


async def test_refresh_and_sync_read_each_country_once(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_by_country)
    provider = _provider(builtins, vendor, {"country_code": ["AU", "US"], "num_proxies": 1})
    stored = [Proxy(id="au", host="10.0.1.2", port=8001, username="u", password="p", connector_id="conn-1", metadata={"list_identity": "AU-1", "geo": "AU"})]
    _updated, removed = await provider.refresh_ips(stored)
    to_add, _ = await provider.sync_proxies(stored)
    assert removed == []
    assert [p.metadata["list_identity"] for p in to_add] == ["US-1"]
    assert len(vendor.api_requests) == 2  # one per country, for the refresh and the sync together


async def test_clearing_the_countries_drops_their_groups_for_the_whole_list(builtins: dict[str, ProviderDescriptor]) -> None:
    """With no countries the whole list is mirrored; rows kept per country before go, or they would be there twice."""
    vendor = MockVendor(api_handler=_api)
    stored = [Proxy(id="au", host="1.1.1.1", port=8000, connector_id="conn-1", metadata={"list_identity": "a", "geo": "AU"})]
    to_add, to_remove = await _provider(builtins, vendor, {}).sync_proxies(stored)
    assert to_remove == ["au"]
    assert [p.metadata["list_identity"] for p in to_add] == ["a", "c"]
    assert all("geo" not in p.metadata for p in to_add)
