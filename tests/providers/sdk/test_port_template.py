# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy type ports given as templates (a connector field overriding the vendor default)."""

import re
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from api.models.proxy import ProxyStatus
from api.providers.sdk.descriptor import ProviderDescriptor, ProxyTypeSpec
from api.providers.sdk.discovery import DescriptorTester
from api.providers.sdk.provider import DescriptorProvider, SdkRuntime
from tests.providers.sdk.conftest import (
    TEST_POLICY,
    MockVendor,
    json_response,
    make_connector,
    make_credential,
)


def _descriptor(**ptype: Any) -> ProviderDescriptor:
    base: dict[str, Any] = {
        "key": "res",
        "label": "Res",
        "mode": "session",
        "host": "{connector.gateway_host|or:gw.vendor.test}",
        "port": "{connector.gateway_port|or:9000}",
        "username": "{credential.username}-session-{session_id}",
        "password": "{credential.password}",
    }
    base.update(ptype)
    return ProviderDescriptor.model_validate(
        {
            "id": "vendor",
            "name": "Vendor",
            "credential_fields": [
                {"key": "username", "label": "U", "required": True},
                {"key": "password", "label": "P", "type": "password", "secret": True, "required": True},
            ],
            "connector_fields": [
                {"key": "num_proxies", "label": "N", "type": "number", "default": 1},
                {"key": "gateway_host", "label": "Host"},
                {"key": "gateway_port", "label": "Port", "type": "number"},
            ],
            "proxy_types": [base],
        }
    )


def _spec(port: Any) -> ProxyTypeSpec:
    return ProxyTypeSpec(key="res", label="Res", mode="session", host="gw", port=port, username="u")


class TestPortField:
    def test_literal_and_numeric_string_ports(self) -> None:
        assert _spec(9000).port == 9000
        assert _spec(" 9000 ").port == 9000

    def test_template_is_kept_as_text(self) -> None:
        spec = _spec("{connector.gateway_port|or:9000}")
        assert spec.port == "{connector.gateway_port|or:9000}"
        assert spec.port_template == spec.port
        assert _spec(9000).port_template is None

    @pytest.mark.parametrize("port", [0, 70000, "70000", "nine thousand", ""])
    def test_rejects_bad_ports(self, port: Any) -> None:
        with pytest.raises(ValidationError):
            _spec(port)


class TestSessionMode:
    async def test_falls_back_to_the_default_port(self) -> None:
        provider = DescriptorProvider(
            _descriptor(), make_connector("vendor", {"num_proxies": 2}),
            make_credential("vendor", {"username": "u", "password": "p"}), SdkRuntime(egress_policy=TEST_POLICY),
        )
        to_add, _ = await provider.sync_proxies([])
        assert [(p.host, p.port) for p in to_add] == [("gw.vendor.test", 9000)] * 2
        assert all(re.fullmatch(r"u-session-[a-z0-9]{12}", p.username or "") for p in to_add)

    async def test_connector_overrides_host_and_port(self) -> None:
        provider = DescriptorProvider(
            _descriptor(), make_connector("vendor", {"num_proxies": 1, "gateway_host": "eu.vendor.test", "gateway_port": 10500}),
            make_credential("vendor", {"username": "u", "password": "p"}), SdkRuntime(egress_policy=TEST_POLICY),
        )
        to_add, _ = await provider.sync_proxies([])
        assert (to_add[0].host, to_add[0].port) == ("eu.vendor.test", 10500)

    async def test_rendered_port_must_be_a_number(self) -> None:
        provider = DescriptorProvider(
            _descriptor(port="{connector.gateway_host}"), make_connector("vendor", {"num_proxies": 1, "gateway_host": "eu.vendor.test"}),
            make_credential("vendor", {"username": "u", "password": "p"}), SdkRuntime(egress_policy=TEST_POLICY),
        )
        with pytest.raises(ValueError, match="rendered to 'eu.vendor.test', not a number"):
            await provider.sync_proxies([])

    async def test_rendered_port_must_be_in_range(self) -> None:
        provider = DescriptorProvider(
            _descriptor(), make_connector("vendor", {"num_proxies": 1, "gateway_port": 70000}),
            make_credential("vendor", {"username": "u", "password": "p"}), SdkRuntime(egress_policy=TEST_POLICY),
        )
        with pytest.raises(ValueError, match="out of range"):
            await provider.sync_proxies([])


class TestPortMode:
    async def test_template_sets_the_first_port_of_the_sequence(self) -> None:
        seen: list[str] = []

        def discovery(request: httpx.Request) -> httpx.Response:
            return json_response({"ip": f"10.0.0.{len(seen) + 1}"})

        vendor = MockVendor(api_handler=lambda r: json_response({}), discovery_handler=discovery)
        descriptor = _descriptor(
            mode="port", port_strategy="sequential", username="{credential.username}",
            discovery={"url": "https://echo.vendor.test/ip", "ip_path": "ip"},
        )
        provider = DescriptorProvider(
            descriptor, make_connector("vendor", {"num_proxies": 2, "gateway_port": 20001}),
            make_credential("vendor", {"username": "u", "password": "p"}), vendor.runtime(),
        )

        def counting(request: httpx.Request) -> httpx.Response:
            response = discovery(request)
            seen.append(request.url.host)
            return response

        vendor.discovery_handler = counting
        to_add, _ = await provider.sync_proxies([])
        assert sorted(p.port for p in to_add) == [20001, 20002]
        assert all(p.status == ProxyStatus.HEALTHY for p in to_add)
        assert sorted(url.split(":")[-1] for url, _ in vendor.discovery_requests) == ["20001", "20002"]


class TestDescriptorTester:
    async def test_required_port_field_is_reported_missing(self) -> None:
        descriptor = _descriptor(port="{connector.gateway_port}")
        descriptor.find_field("connector", "gateway_port").required = True  # type: ignore[union-attr]
        vendor = MockVendor(api_handler=lambda r: json_response({}))
        outcome = await DescriptorTester(descriptor, vendor.runtime()).run(
            "proxy_request", {"username": "u", "password": "p"}, {"num_proxies": 1}
        )
        assert not outcome.ok and outcome.message == "Missing connector values: gateway_port"
