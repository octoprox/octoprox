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
        {"id": "a", "username": "u1", "password": "p1", "proxy_address": "1.1.1.1", "port": 8000, "valid": True, "country_code": "US"},
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


async def test_refresh_updates_credentials_in_place(builtins: dict[str, ProviderDescriptor]) -> None:
    vendor = MockVendor(api_handler=_api)
    proxies = [
        Proxy(id="a", host="1.1.1.1", port=8000, username="old", password="old", connector_id="conn-1", metadata={"list_identity": "a"}),
        Proxy(id="c", host="3.3.3.3", port=8002, username="u3", password="p3", connector_id="conn-1", metadata={"list_identity": "c", "country": "DE"}),
        Proxy(id="gone", host="4.4.4.4", port=1, connector_id="conn-1", metadata={"list_identity": "nope"}),
    ]
    updated, to_remove = await _provider(builtins, vendor, {}).refresh_ips(proxies)
    assert to_remove == ["gone"]
    assert [p.id for p in updated] == ["a"]
    assert (updated[0].username, updated[0].password) == ("u1", "p1")
