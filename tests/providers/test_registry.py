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
    validated = registry.validate_credential_config("aws", {"access_key": "a", "secret_key": "s"})
    assert validated == {"auth_mode": "access_keys", "access_key": "a", "secret_key": "s"}
    # The defaulted mode is stored as a plain string, not the enum member.
    assert type(validated["auth_mode"]) is str
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
    assert connector_config == {
        "num_proxies": 2, "session_mode": "pool", "country_code": ["US"], "session_duration_minutes": 5
    }
    # Hidden session fields are dropped for port-based types; countries stay (they filter discovery).
    isp = registry.validate_connector_config(
        "oxylabs", {"num_proxies": 2, "country_code": "US", "session_duration_minutes": 5}, {"proxy_type": "isp"}
    )
    assert isp == {"num_proxies": 2, "country_code": ["US"]}
    # Captured keys survive credential validation.
    assert registry.validate_credential_config("brightdata", {"token": "t", "customer_id": "c"}) == {"token": "t", "customer_id": "c"}

    credential = Credential(id="c", name="c", type="oxylabs", project_id="p", config=config)
    connector = Connector(id="k", name="k", credential_id="c", credential_type="oxylabs", project_id="p", config=connector_config)
    provider = registry.create_provider(connector, credential)
    assert isinstance(provider, DescriptorProvider) and provider.proxy_type.key == "residential"
    assert registry.create_provider(connector, Credential(id="x", name="x", type="aws", project_id="p")) is None


def test_custom_descriptor_lifecycle(registry: ProviderRegistry) -> None:
    assert registry.replace_custom([_custom()]) == (["acme"], [], [])
    ptype = registry.get("acme")
    assert ptype is not None and ptype.editable and ptype.source == "custom"
    assert registry.validate_credential_config("acme", {"username": "u", "password": "p"}) == {"username": "u", "password": "p"}

    # Re-syncing an identical set is a no-op: same registration object, nothing reported.
    assert registry.replace_custom([_custom()]) == ([], [], [])
    assert registry.get("acme") is ptype

    changed = _custom().model_copy(update={"name": "Acme v2"})
    assert registry.replace_custom([changed]) == ([], ["acme"], [])
    assert registry.get("acme").name == "Acme v2"  # type: ignore[union-attr]

    assert registry.replace_custom([]) == ([], [], ["acme"])
    assert registry.get("acme") is None


def test_custom_cannot_shadow_builtin(registry: ProviderRegistry) -> None:
    registry.replace_custom([_custom("oxylabs")])
    assert registry.get("oxylabs").source == "builtin"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already taken"):
        registry.register_descriptor(_custom("aws"), "custom")



OXYLABS = {"proxy_type": "residential", "username": "u", "password": "p"}


class _OwnProvider:
    """A plugin's own provider class: it syncs as a provider does, but is no descriptor provider."""

    def __init__(self, connector: Connector, credential: Credential) -> None:
        self.connector = connector


def fail_to_build(registry: ProviderRegistry, type_id: str, error: Exception) -> None:
    """Make every provider of ``type_id`` fail to build with ``error``, as a plugin's own provider class can."""
    ptype = registry.get(type_id)
    assert ptype is not None and ptype.descriptor is not None

    class Failing:
        def __init__(self, connector: Connector, credential: Credential) -> None:
            raise error

    registry.unregister(type_id)
    registry.register_descriptor(ptype.descriptor, "plugin", provider_class=Failing)


def _connector(credential_type: str) -> Connector:
    return Connector(id="k", name="k", credential_id="c", credential_type=credential_type, project_id="p")


def _credential(credential_type: str, config: dict[str, str] | None = None) -> Credential:
    return Credential(id="c", name="c", type=credential_type, project_id="p", config=config or {})


class TestDescriptorProvider:
    """The one way the compiler, the routing snapshot, the read model and provision build a connector's provider."""

    def test_a_descriptor_type_builds_its_provider(self, registry: ProviderRegistry) -> None:
        provider = registry.descriptor_provider(_connector("oxylabs"), _credential("oxylabs", OXYLABS))

        assert isinstance(provider, DescriptorProvider) and provider.proxy_type.key == "residential"

    def test_none_without_a_credential(self, registry: ProviderRegistry) -> None:
        assert registry.descriptor_provider(_connector("oxylabs"), None) is None

    def test_none_for_a_code_type(self, registry: ProviderRegistry) -> None:
        """A code type registers no factory: its connectors' rows carry everything."""
        aws = registry.get("aws")
        assert aws is not None and aws.factory is None

        assert registry.descriptor_provider(_connector("aws"), _credential("aws")) is None

    def test_none_for_a_provider_that_is_not_a_descriptor_provider(self, registry: ProviderRegistry) -> None:
        registry.register_descriptor(_custom("own"), "plugin", provider_class=_OwnProvider)
        connector, credential = _connector("own"), _credential("own", {"username": "u", "password": "p"})
        assert isinstance(registry.create_provider(connector, credential), _OwnProvider)

        assert registry.descriptor_provider(connector, credential) is None

    def test_what_the_factory_raises_is_raised(self, registry: ProviderRegistry) -> None:
        """Each caller keeps its own rule for a provider that cannot be built, so none is applied here."""
        connector = _connector("oxylabs")
        with pytest.raises(ValueError, match="unknown proxy type 'nonsense'"):
            registry.descriptor_provider(connector, _credential("oxylabs", {**OXYLABS, "proxy_type": "nonsense"}))

        fail_to_build(registry, "oxylabs", RuntimeError("the vendor SDK is broken"))
        with pytest.raises(RuntimeError, match="the vendor SDK is broken"):
            registry.descriptor_provider(connector, _credential("oxylabs", OXYLABS))
