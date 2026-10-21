# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for filling the credential placeholders of proxy rows, and for the credential a section carries.

Both functions are pure, so rows, credentials and connectors are built in
place, with placeholders such as ``{password}`` or ``{zone_password}`` as
provider templates store them.
"""

from collections import defaultdict
from typing import Any

import pytest

from api.feed.sections import resolve_credentials, routing_credential
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.sdk.descriptor import FieldSpec, ProviderDescriptor, split_scoped_key
from api.providers.sdk.loader import descriptor_from_dict, load_builtin_descriptors
from api.providers.sdk.provider import DescriptorProvider
from api.providers.sdk.strategies import META_DYNAMIC_SESSIONS

# The condition of the "-pinned" part of the vendor's password: the account has a PIN.
PIN_SET: dict[str, Any] = {"field": "credential.pin"}


def vendor(*, pinned_when: dict[str, Any] | list[dict[str, Any]] = PIN_SET) -> ProviderDescriptor:
    """A vendor whose account API token only the credential check reads.

    A connector may set its own password over the account's, so a rendered
    request's ``{password}`` is filled from either; the password ends in
    "-pinned" while ``pinned_when`` holds.
    """
    return descriptor_from_dict(
        {
            "id": "vendor",
            "name": "Vendor",
            "credential_fields": [
                {"key": "proxy_type", "label": "Type", "type": "select", "options": [{"value": "res", "label": "Res"}]},
                {"key": "username", "label": "User", "required": True},
                {"key": "password", "label": "Pass", "type": "password", "secret": True, "required": True},
                {"key": "pin", "label": "PIN", "type": "password", "secret": True},
                {"key": "api_key", "label": "API key", "type": "password", "secret": True},
                {"key": "note", "label": "Note"},
            ],
            "connector_fields": [{"key": "password", "label": "Own password", "type": "password", "secret": True}],
            "proxy_type_field": "credential.proxy_type",
            "proxy_types": [
                {
                    "key": "res",
                    "label": "Residential",
                    "mode": "session",
                    "host": "gw.vendor.test",
                    "port": 9000,
                    "username": "{credential.username}-session-{session_id}",
                    "password": {"parts": [{"text": "{connector.password}"}, {"text": "pinned", "when": pinned_when}]},
                }
            ],
            "validation": {
                "call": {"url": "https://api.vendor.test/me", "headers": {"Authorization": "Key {credential.api_key}"}}
            },
        }
    )


def make_connector(credential_type: str = CredentialType.STATIC_PROXY_PROVIDER, **config: Any) -> Connector:
    return Connector(
        id="connector-1",
        name="vendor",
        credential_id="credential-1",
        credential_type=credential_type,
        project_id="project-1",
        config=config,
    )


def make_credential(credential_type: str = CredentialType.STATIC_PROXY_PROVIDER, **config: Any) -> Credential:
    return Credential(id="credential-1", name="vendor", type=credential_type, project_id="project-1", config=config)


def make_proxy(username: str | None, password: str | None, host: str = "proxy.example.com", port: int = 8080) -> Proxy:
    return Proxy(
        host=host,
        port=port,
        protocol=ProxyProtocol.HTTP,
        username=username,
        password=password,
        connector_id="connector-1",
    )


class TestVendorRows:
    def test_oxylabs_session_rows(self) -> None:
        connector = make_connector("oxylabs", num_proxies=10, country_code="US")
        credential = make_credential("oxylabs", username="newuser", password="newpass")
        proxy = make_proxy("customer-{username}-cc-US-sessid-abc123", "{password}", "pr.oxylabs.io", 7777)

        resolved = resolve_credentials(proxy, credential, connector)

        assert (resolved.username, resolved.password) == ("customer-newuser-cc-US-sessid-abc123", "newpass")

    def test_oxylabs_port_rows(self) -> None:
        connector = make_connector("oxylabs", num_proxies=5)
        credential = make_credential("oxylabs", username="ispuser", password="isppass")
        proxy = make_proxy("user-{username}", "{password}", "isp.oxylabs.io", 8001)

        resolved = resolve_credentials(proxy, credential, connector)

        assert (resolved.username, resolved.password) == ("user-ispuser", "isppass")

    def test_brightdata_session_rows_draw_on_the_credential_and_the_connector(self) -> None:
        connector = make_connector(
            "brightdata",
            zone_name="zone1",
            zone_password="zonepass123",
            proxy_type="residential",
            num_proxies=10,
            country_code="GB",
        )
        credential = make_credential("brightdata", token="api_token", customer_id="cust_new123")
        proxy = make_proxy(
            "brd-customer-{customer_id}-zone-zone1-session-glob_xyz789-country-gb",
            "{zone_password}",
            "brd.superproxy.io",
            44445,
        )

        resolved = resolve_credentials(proxy, credential, connector)

        assert resolved.username == "brd-customer-cust_new123-zone-zone1-session-glob_xyz789-country-gb"
        assert resolved.password == "zonepass123"

    def test_brightdata_port_rows(self) -> None:
        connector = make_connector(
            "brightdata", zone_name="isp_zone", zone_password="ispzonepass", proxy_type="isp", num_proxies=5
        )
        credential = make_credential("brightdata", token="api_token", customer_id="cust_isp456")
        proxy = make_proxy(
            "brd-customer-{customer_id}-zone-isp_zone-ip-192.168.1.100", "{zone_password}", "brd.superproxy.io", 44445
        )

        resolved = resolve_credentials(proxy, credential, connector)

        assert resolved.username == "brd-customer-cust_isp456-zone-isp_zone-ip-192.168.1.100"
        assert resolved.password == "ispzonepass"

    def test_static_rows_take_the_credential_defaults(self) -> None:
        credential = make_credential(username="default_user", password="default_pass")

        resolved = resolve_credentials(make_proxy("{username}", "{password}"), credential, make_connector())

        assert (resolved.username, resolved.password) == ("default_user", "default_pass")


class TestWhatFillsAPlaceholder:
    def test_placeholders_nothing_fills_stay_as_they_are(self) -> None:
        # No credential (a section ships none for static and cloud connectors) and no connector value.
        resolved = resolve_credentials(make_proxy("{username}", "{password}"), None, make_connector())
        assert (resolved.username, resolved.password) == ("{username}", "{password}")

    def test_a_row_with_its_own_credentials_is_unchanged(self) -> None:
        credential = make_credential(username="default_user", password="default_pass")

        resolved = resolve_credentials(make_proxy("own_user", "own_pass"), credential, make_connector())

        assert (resolved.username, resolved.password) == ("own_user", "own_pass")

    def test_the_connector_wins_over_the_credential(self) -> None:
        credential = make_credential(username="credential-user", password="credential-pass")
        connector = make_connector(username="connector-user")

        resolved = resolve_credentials(make_proxy("{username}", "{password}"), credential, connector)

        assert (resolved.username, resolved.password) == ("connector-user", "credential-pass")

    def test_only_non_empty_strings_fill_a_placeholder(self) -> None:
        credential = make_credential(username="credential-user", zone="z1")
        # An empty or non-string connector value never hides the credential's.
        connector = make_connector(username="", zone=5, num_proxies=3)

        resolved = resolve_credentials(make_proxy("{username}-{zone}", "{num_proxies}"), credential, connector)

        assert (resolved.username, resolved.password) == ("credential-user-z1", "{num_proxies}")

    def test_no_username_or_password_stays_none(self) -> None:
        resolved = resolve_credentials(make_proxy(None, None), make_credential(username="user"), make_connector())
        assert (resolved.username, resolved.password) == (None, None)


class TestPurity:
    def test_the_row_passed_in_is_never_changed(self) -> None:
        proxy = make_proxy("{username}", "{password}")

        resolved = resolve_credentials(proxy, make_credential(username="user", password="pass"), make_connector())

        assert (proxy.username, proxy.password) == ("{username}", "{password}")
        assert (resolved.username, resolved.password) == ("user", "pass")

    def test_the_result_is_always_a_copy(self) -> None:
        proxy = make_proxy("own_user", "own_pass")

        resolved = resolve_credentials(proxy, None, make_connector())
        resolved.port = 9090

        assert resolved is not proxy
        assert proxy.port == 8080


def gateway_row(connector: Connector) -> Proxy:
    """The one row of a dynamic-sessions connector: every request re-renders its credentials and endpoint."""
    return Proxy(host="gateway", port=1, connector_id=connector.id, metadata={META_DYNAMIC_SESSIONS: "true"})


def rendered(provider: DescriptorProvider, credential: Credential) -> tuple[str | None, str | None, str, int]:
    """One request through ``provider``'s gateway row as a gateway routes it: rendered, then placeholders filled."""
    request = provider.render_request(
        gateway_row(provider.connector), sessid="order-1", location=LocationTarget(country="US"), scope="project-1"
    )
    proxy = resolve_credentials(request, credential, provider.connector)
    return proxy.username, proxy.password, proxy.host, proxy.port


def vendor_credential(**config: Any) -> Credential:
    return make_credential("vendor", proxy_type="res", username="alice", password="s3cret", **config)


class TestRoutingCredential:
    def test_a_secret_only_the_vendor_api_reads_stays_in_postgres(self) -> None:
        """Validation, option lists and syncs run in jobs that read Postgres; a gateway only renders requests."""
        credential = vendor_credential(pin="1234", api_key="account-token", note="team a")

        shipped = routing_credential(credential, DescriptorProvider(vendor(), make_connector("vendor"), credential))

        assert shipped.config == {
            "proxy_type": "res", "username": "alice", "password": "s3cret", "pin": "1234", "note": "team a"
        }
        assert credential.config["api_key"] == "account-token"

    @pytest.mark.parametrize(
        "pinned_when", [PIN_SET, [{"field": "credential.username"}, PIN_SET]], ids=["condition", "list of conditions"]
    )
    def test_a_request_renders_alike_from_what_travels(self, pinned_when: Any) -> None:
        """The password comes back through the connector's placeholder, and the PIN only decides a template part."""
        descriptor = vendor(pinned_when=pinned_when)
        connector = make_connector("vendor", session_mode="dynamic")
        credential = vendor_credential(pin="1234", api_key="account-token")
        provider = DescriptorProvider(descriptor, connector, credential)

        shipped = routing_credential(credential, provider)
        on_gateway = DescriptorProvider(descriptor, connector, shipped)

        assert on_gateway.is_dynamic
        assert rendered(on_gateway, shipped) == rendered(provider, credential)
        assert rendered(on_gateway, shipped)[1] == "s3cret-pinned"

    def test_a_credential_holding_nothing_to_leave_out_travels_as_it_is(self) -> None:
        credential = vendor_credential()
        provider = DescriptorProvider(vendor(), make_connector("vendor"), credential)

        assert routing_credential(credential, provider) is credential

    def test_built_in_vendors_keep_back_their_api_tokens_and_render_alike(self) -> None:
        """Every type of every built-in vendor: a gateway builds the same type, and a dynamic request renders alike."""
        left_out: dict[str, set[str]] = defaultdict(set)
        for loaded in load_builtin_descriptors():
            descriptor = loaded.descriptor
            for ptype in descriptor.proxy_types:
                credential, connector = built_in_pair(descriptor, ptype.key)
                provider = DescriptorProvider(descriptor, connector, credential)

                shipped = routing_credential(credential, provider)
                on_gateway = DescriptorProvider(descriptor, connector, shipped)

                assert on_gateway.proxy_type.key == ptype.key
                if ptype.mode == "session":
                    assert on_gateway.is_dynamic
                    assert rendered(on_gateway, shipped) == rendered(provider, credential)
                left_out[descriptor.id] |= credential.config.keys() - shipped.config.keys()

        assert dict(left_out) == {
            "brightdata": {"token"},
            "decodo": {"api_key"},
            "iproyal": {"api_token"},
            "netnut": set(),
            "oxylabs": set(),
            "webshare": {"api_key"},
        }


def built_in_pair(descriptor: ProviderDescriptor, type_key: str) -> tuple[Credential, Connector]:
    """A credential and a dynamic-sessions connector of ``descriptor``, every field set, of the type ``type_key``."""
    chosen: dict[str, dict[str, Any]] = {"credential": {}, "connector": {"session_mode": "dynamic"}}
    if descriptor.proxy_type_field is not None:
        scope, key = split_scoped_key(descriptor.proxy_type_field)
        chosen[scope][key] = type_key
    credential = make_credential(descriptor.id, **{**filled(descriptor.credential_fields), **chosen["credential"]})
    connector = make_connector(descriptor.id, **{**filled(descriptor.connector_fields), **chosen["connector"]})
    return credential, connector


def filled(fields: list[FieldSpec]) -> dict[str, Any]:
    """A value for every field, of its type: what a saved form holds."""
    values: dict[str, Any] = {}
    for field in fields:
        if field.options:
            values[field.key] = field.options[0].value
        elif field.type == "country" or field.options_preset == "countries":
            values[field.key] = ["US"]
        elif field.type == "number":
            values[field.key] = 10
        elif field.type == "boolean":
            values[field.key] = True
        else:
            values[field.key] = f"{field.key}-value"
    return values
