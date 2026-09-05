# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider registry."""

import pytest

from api.models.connector import Connector
from api.models.credential import Credential
from api.providers.registry import ProviderRegistry, UnknownProviderError, build_registry
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider
from api.providers.sdk.validation import ConfigValidationError


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


def _custom(provider_id: str = "acme") -> ProviderDescriptor:
    return ProviderDescriptor.model_validate(
        {
            "id": provider_id,
            "name": "Acme",
            "credential_fields": [{"key": "username", "label": "User", "required": True}, {"key": "password", "label": "Pass", "type": "password", "secret": True, "required": True}],
            "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number", "default": 1, "min": 1}],
            "proxy_types": [{"key": "res", "label": "Res", "mode": "session", "host": "gw.acme.test", "port": 9000, "username": "{credential.username}-{session_id}", "password": "{credential.password}"}],
        }
    )


def test_default_registry_contents(registry: ProviderRegistry) -> None:
    ids = registry.ids()
    assert {"static_proxy_provider", "aws", "gcp", "azure", "oxylabs", "brightdata", "decodo", "webshare", "iproyal", "netnut"} <= ids
    assert registry.is_cloud("aws") and not registry.is_cloud("oxylabs")
    assert registry.is_syncable("oxylabs") and not registry.is_syncable("aws") and not registry.is_syncable("static_proxy_provider")
    assert registry.get("oxylabs") is not None and registry.get("oxylabs").source == "builtin"  # type: ignore[union-attr]
    assert not registry.get("oxylabs").editable  # type: ignore[union-attr]
    assert registry.get("aws").kind == "code"  # type: ignore[union-attr]
    # Code types come first, then descriptors alphabetically.
    assert [t.kind for t in registry.list()][:4] == ["code"] * 4


def test_code_type_validation_delegates_to_pydantic(registry: ProviderRegistry) -> None:
    assert registry.validate_credential_config("aws", {"access_key": "a", "secret_key": "s"}) == {"access_key": "a", "secret_key": "s"}
    with pytest.raises(ValueError):
        registry.validate_credential_config("aws", {"access_key": "", "secret_key": "s"})
    assert registry.validate_connector_config("static_proxy_provider", {"anything": 1}) == {}
    with pytest.raises(UnknownProviderError):
        registry.validate_credential_config("nope", {})


def test_descriptor_validation_and_provider_creation(registry: ProviderRegistry) -> None:
    config = registry.validate_credential_config("oxylabs", {"proxy_type": "residential", "username": " u ", "password": "p"})
    assert config == {"proxy_type": "residential", "username": "u", "password": "p"}
    with pytest.raises(ConfigValidationError):
        registry.validate_credential_config("oxylabs", {"proxy_type": "residential"})
    connector_config = registry.validate_connector_config("oxylabs", {"num_proxies": "2", "country_code": "us", "session_duration_minutes": "5"}, config)
    assert connector_config == {"num_proxies": 2, "country_code": "US", "session_duration_minutes": 5}
    # Hidden session fields are dropped for port-based types.
    isp = registry.validate_connector_config("oxylabs", {"num_proxies": 2, "country_code": "US"}, {"proxy_type": "isp"})
    assert isp == {"num_proxies": 2}
    # Captured keys survive credential validation.
    assert registry.validate_credential_config("brightdata", {"token": "t", "customer_id": "c"}) == {"token": "t", "customer_id": "c"}

    credential = Credential(id="c", name="c", type="oxylabs", project_id="p", config=config)
    connector = Connector(id="k", name="k", credential_id="c", credential_type="oxylabs", project_id="p", config=connector_config)
    provider = registry.create_provider(connector, credential)
    assert isinstance(provider, DescriptorProvider) and provider.proxy_type.key == "residential"
    assert registry.create_provider(connector, Credential(id="x", name="x", type="aws", project_id="p")) is None


def test_custom_descriptor_lifecycle(registry: ProviderRegistry) -> None:
    registry.replace_custom([_custom()])
    ptype = registry.get("acme")
    assert ptype is not None and ptype.editable and ptype.source == "custom"
    assert registry.validate_credential_config("acme", {"username": "u", "password": "p"}) == {"username": "u", "password": "p"}
    registry.replace_custom([])
    assert registry.get("acme") is None


def test_custom_cannot_shadow_builtin(registry: ProviderRegistry) -> None:
    registry.replace_custom([_custom("oxylabs")])
    assert registry.get("oxylabs").source == "builtin"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already taken"):
        registry.register_descriptor(_custom("aws"), "custom")
