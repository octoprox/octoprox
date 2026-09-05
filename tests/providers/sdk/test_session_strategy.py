# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Session-mode provisioning through the built-in descriptors (Oxylabs, IPRoyal, NetNut)."""

import re

import pytest

from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider, SdkRuntime
from tests.providers.sdk.conftest import TEST_POLICY, make_connector, make_credential


@pytest.fixture
def runtime() -> SdkRuntime:
    return SdkRuntime(egress_policy=TEST_POLICY)


class TestOxylabsResidential:
    def _provider(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime, connector_config: dict[str, object]) -> DescriptorProvider:
        credential = make_credential("oxylabs", {"proxy_type": "residential", "username": "alice", "password": "pw"})
        connector = make_connector("oxylabs", connector_config)
        return DescriptorProvider(builtins["oxylabs"], connector, credential, runtime)

    async def test_creates_session_proxies_with_placeholders(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = self._provider(builtins, runtime, {"num_proxies": 3, "country_code": "US", "session_duration_minutes": 10})
        assert provider.is_session_based()
        to_add, to_remove = await provider.sync_proxies([])
        assert to_remove == [] and len(to_add) == 3
        for proxy in to_add:
            assert proxy.host == "pr.oxylabs.io" and proxy.port == 7777
            assert proxy.protocol == ProxyProtocol.HTTP
            assert proxy.status == ProxyStatus.HEALTHY
            assert proxy.password == "{password}"  # secret stays a runtime placeholder
            assert re.fullmatch(r"customer-alice-cc-US-sessid-[a-z0-9]{12}-sesstime-10", proxy.username or "")
            assert proxy.metadata["provider"] == "oxylabs"
            assert proxy.metadata["proxy_type"] == "residential"
            assert proxy.metadata["session_id"] in (proxy.username or "")
            assert proxy.tags == ["oxylabs", "residential"]
        assert len({p.metadata["session_id"] for p in to_add}) == 3

    async def test_country_is_omitted_when_unset(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = self._provider(builtins, runtime, {"num_proxies": 1})
        to_add, _ = await provider.sync_proxies([])
        assert re.fullmatch(r"customer-alice-sessid-[a-z0-9]{12}", to_add[0].username or "")

    async def test_scales_down_unhealthy_first(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        provider = self._provider(builtins, runtime, {"num_proxies": 1})
        existing = [
            Proxy(id="healthy", host="pr.oxylabs.io", port=7777, connector_id="conn-1", status=ProxyStatus.HEALTHY),
            Proxy(id="sick", host="pr.oxylabs.io", port=7777, connector_id="conn-1", status=ProxyStatus.UNHEALTHY),
            Proxy(id="unknown", host="pr.oxylabs.io", port=7777, connector_id="conn-1", status=ProxyStatus.UNKNOWN),
        ]
        to_add, to_remove = await provider.sync_proxies(existing)
        assert to_add == []
        assert set(to_remove) == {"sick", "unknown"}

    async def test_legacy_rows_are_adopted_without_churn(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        """Proxies created by the old OxylabsProvider count as existing slots."""
        provider = self._provider(builtins, runtime, {"num_proxies": 2, "country_code": "US"})
        legacy = [
            Proxy(host="pr.oxylabs.io", port=7777, username="customer-{username}-cc-US-sessid-abc", password="{password}",
                  connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"session_id": "abc", "proxy_type": "residential"}),
            Proxy(host="pr.oxylabs.io", port=7777, username="customer-{username}-cc-US-sessid-def", password="{password}",
                  connector_id="conn-1", status=ProxyStatus.HEALTHY, metadata={"session_id": "def", "proxy_type": "residential"}),
        ]
        assert await provider.sync_proxies(legacy) == ([], [])
        assert await provider.refresh_ips(legacy) == ([], [])

    async def test_requires_credential(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        with pytest.raises(ValueError, match="requires a credential"):
            DescriptorProvider(builtins["oxylabs"], make_connector("oxylabs", {}), None, runtime)


class TestPasswordEncodedProviders:
    async def test_iproyal_encodes_targeting_in_password(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        credential = make_credential("iproyal", {"username": "royal", "password": "pw"})
        connector = make_connector("iproyal", {"num_proxies": 1, "country_code": "GB", "session_lifetime": "30m"})
        provider = DescriptorProvider(builtins["iproyal"], connector, credential, runtime)
        to_add, _ = await provider.sync_proxies([])
        proxy = to_add[0]
        assert proxy.host == "geo.iproyal.com" and proxy.port == 12321
        assert proxy.username == "royal"
        assert re.fullmatch(r"\{password\}_country-gb_session-[a-z0-9]{8}_lifetime-30m", proxy.password or "")

    async def test_iproyal_uses_selected_entry_node(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        credential = make_credential("iproyal", {"username": "royal", "password": "pw", "api_token": "t"})
        connector = make_connector("iproyal", {"num_proxies": 1, "entry_node": "proxy.iproyal.com"})
        provider = DescriptorProvider(builtins["iproyal"], connector, credential, runtime)
        to_add, _ = await provider.sync_proxies([])
        assert to_add[0].host == "proxy.iproyal.com"

    async def test_netnut_defaults_country_to_any(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        credential = make_credential("netnut", {"username": "nn", "password": "pw"})
        provider = DescriptorProvider(builtins["netnut"], make_connector("netnut", {"num_proxies": 1}), credential, runtime)
        to_add, _ = await provider.sync_proxies([])
        assert re.fullmatch(r"nn-res-any-sid-[1-9][0-9]{7}", to_add[0].username or "")
        provider = DescriptorProvider(
            builtins["netnut"], make_connector("netnut", {"num_proxies": 1, "country_code": "DE"}), credential, runtime
        )
        to_add, _ = await provider.sync_proxies([])
        assert (to_add[0].username or "").startswith("nn-res-de-sid-")

    async def test_netnut_product_tokens(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        for proxy_type, token in (("static_residential", "stc"), ("mobile", "mob"), ("datacenter", "dc")):
            credential = make_credential("netnut", {"proxy_type": proxy_type, "username": "nn", "password": "pw"})
            provider = DescriptorProvider(builtins["netnut"], make_connector("netnut", {"num_proxies": 1, "country_code": "US"}), credential, runtime)
            to_add, _ = await provider.sync_proxies([])
            assert re.fullmatch(rf"nn-{token}-us-sid-[1-9][0-9]{{7}}", to_add[0].username or "")

    async def test_decodo_username(self, builtins: dict[str, ProviderDescriptor], runtime: SdkRuntime) -> None:
        credential = make_credential("decodo", {"username": "smith", "password": "pw"})
        connector = make_connector("decodo", {"num_proxies": 1, "country_code": "US", "session_duration_minutes": 90})
        to_add, _ = await DescriptorProvider(builtins["decodo"], connector, credential, runtime).sync_proxies([])
        assert re.fullmatch(r"user-smith-country-us-session-[a-z0-9]{12}-sessionduration-90", to_add[0].username or "")
        assert to_add[0].host == "gate.decodo.com" and to_add[0].port == 7000
