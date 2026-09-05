# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Every shipped descriptor must load, be internally consistent and round-trip through YAML."""

import pytest

from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.loader import (
    descriptor_from_yaml,
    descriptor_to_yaml,
    load_builtin_descriptors,
)
from api.providers.sdk.templating import TemplateRenderer

EXPECTED_IDS = {"oxylabs", "brightdata", "decodo", "webshare", "iproyal", "netnut"}


def test_all_builtins_load() -> None:
    loaded = {d.descriptor.id for d in load_builtin_descriptors()}
    assert loaded == EXPECTED_IDS


@pytest.mark.parametrize("provider_id", sorted(EXPECTED_IDS))
def test_builtin_round_trips_through_yaml(builtins: dict[str, ProviderDescriptor], provider_id: str) -> None:
    descriptor = builtins[provider_id]
    again = descriptor_from_yaml(descriptor_to_yaml(descriptor))
    assert again == descriptor


@pytest.mark.parametrize("provider_id", sorted(EXPECTED_IDS))
def test_builtin_templates_reference_declared_fields(builtins: dict[str, ProviderDescriptor], provider_id: str) -> None:
    descriptor = builtins[provider_id]
    known = {f"credential.{f.key}" for f in descriptor.credential_fields} | {
        f"connector.{f.key}" for f in descriptor.connector_fields
    }
    if descriptor.validation is not None:
        known |= {f"credential.{k}" for k in descriptor.validation.capture}
    for ptype in descriptor.proxy_types:
        for template in (ptype.host, ptype.username, ptype.password):
            for path in TemplateRenderer.referenced_paths(template):
                if path.startswith(("credential.", "connector.")):
                    assert path in known, f"{provider_id}/{ptype.key} references undeclared {path}"


def test_legacy_ids_and_config_keys_are_preserved(builtins: dict[str, ProviderDescriptor]) -> None:
    """Existing Oxylabs/BrightData rows are adopted without a data migration."""
    oxylabs = builtins["oxylabs"]
    assert {f.key for f in oxylabs.credential_fields} == {"proxy_type", "username", "password"}
    assert {f.key for f in oxylabs.connector_fields} == {"num_proxies", "country_code", "session_duration_minutes"}
    assert {t.key for t in oxylabs.proxy_types} == {
        "residential", "mobile", "isp", "dedicated_isp", "datacenter", "datacenter_dedicated"
    }
    brightdata = builtins["brightdata"]
    assert {f.key for f in brightdata.credential_fields} == {"token"}
    assert brightdata.validation is not None and set(brightdata.validation.capture) == {"customer_id"}
    assert {f.key for f in brightdata.connector_fields} == {
        "zone_name", "zone_password", "proxy_type", "num_proxies", "country_code", "healthcheck_url"
    }


def test_egress_and_gateway_hosts(builtins: dict[str, ProviderDescriptor]) -> None:
    assert builtins["brightdata"].egress_hosts() == ["api.brightdata.com"]
    assert builtins["oxylabs"].egress_hosts() == []
    assert builtins["oxylabs"].discovery_hosts() == ["ip.oxylabs.io"]
    assert "brd.superproxy.io" in builtins["brightdata"].gateway_hosts()
    assert builtins["webshare"].egress_hosts() == ["proxy.webshare.io"]


def test_new_vendor_product_lines(builtins: dict[str, ProviderDescriptor]) -> None:
    decodo = builtins["decodo"]
    assert {t.key: t.mode for t in decodo.proxy_types} == {
        "residential": "session", "mobile": "session", "isp": "port", "datacenter": "port"
    }
    assert decodo.get_proxy_type("isp").host == "isp.decodo.com"  # type: ignore[union-attr]
    assert decodo.get_proxy_type("datacenter").port == 10001  # type: ignore[union-attr]
    netnut = builtins["netnut"]
    assert {t.key for t in netnut.proxy_types} == {"residential", "static_residential", "mobile", "datacenter"}
    assert all(t.host == "gw.netnut.net" for t in netnut.proxy_types)


def test_resolve_proxy_type(builtins: dict[str, ProviderDescriptor]) -> None:
    oxylabs = builtins["oxylabs"]
    assert oxylabs.resolve_proxy_type({"proxy_type": "isp"}, {}).mode == "port"
    assert oxylabs.resolve_proxy_type({}, {}).key == "residential"  # field default
    with pytest.raises(ValueError):
        oxylabs.resolve_proxy_type({"proxy_type": "nope"}, {})
    assert builtins["webshare"].resolve_proxy_type({}, {}).mode == "list"
    assert builtins["brightdata"].resolve_proxy_type({}, {"proxy_type": "datacenter"}).port_strategy == "fixed"


def test_secret_keys(builtins: dict[str, ProviderDescriptor]) -> None:
    assert builtins["oxylabs"].secret_keys() == {"password"}
    assert builtins["brightdata"].secret_keys() == {"token", "zone_password"}


class TestDescriptorValidation:
    def test_rejects_unknown_references(self) -> None:
        with pytest.raises(ValueError, match="unknown options source"):
            ProviderDescriptor.model_validate(
                {
                    "id": "x1",
                    "name": "X",
                    "connector_fields": [{"key": "zone", "label": "Zone", "type": "select", "options_from": "zones"}],
                    "proxy_types": [{"key": "a", "label": "A", "mode": "session", "host": "h", "port": 1, "username": "u"}],
                }
            )

    def test_requires_selector_for_multiple_types(self) -> None:
        with pytest.raises(ValueError, match="proxy_type_field is required"):
            ProviderDescriptor.model_validate(
                {
                    "id": "x2",
                    "name": "X",
                    "proxy_types": [
                        {"key": "a", "label": "A", "mode": "session", "host": "h", "port": 1, "username": "u"},
                        {"key": "b", "label": "B", "mode": "session", "host": "h", "port": 1, "username": "u"},
                    ],
                }
            )

    def test_mode_requirements(self) -> None:
        with pytest.raises(ValueError, match="needs host and port"):
            ProviderDescriptor.model_validate(
                {"id": "x3", "name": "X", "proxy_types": [{"key": "a", "label": "A", "mode": "session", "username": "u"}]}
            )
        with pytest.raises(ValueError, match="needs a list source"):
            ProviderDescriptor.model_validate(
                {"id": "x4", "name": "X", "proxy_types": [{"key": "a", "label": "A", "mode": "list"}]}
            )

    def test_rejects_bad_ids_and_urls(self) -> None:
        with pytest.raises(ValueError, match="id must be"):
            ProviderDescriptor.model_validate(
                {"id": "Bad Id", "name": "X", "proxy_types": [{"key": "a", "label": "A", "mode": "session", "host": "h", "port": 1, "username": "u"}]}
            )
        with pytest.raises(ValueError, match="http"):
            ProviderDescriptor.model_validate(
                {
                    "id": "x5",
                    "name": "X",
                    "validation": {"call": {"url": "ftp://x"}},
                    "proxy_types": [{"key": "a", "label": "A", "mode": "session", "host": "h", "port": 1, "username": "u"}],
                }
            )
