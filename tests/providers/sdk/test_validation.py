# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for field-driven config validation."""

import pytest

from api.providers.sdk.descriptor import Condition, FieldSpec, OptionSpec
from api.providers.sdk.validation import ConfigValidationError, FieldSetValidator

FIELDS = [
    FieldSpec(key="username", label="Username", required=True),
    FieldSpec(key="password", label="Password", type="password", secret=True, required=True),
    FieldSpec(key="num_proxies", label="Proxies", type="number", default=1, min=1, max=10),
    FieldSpec(key="country_code", label="Country", type="country"),
    FieldSpec(key="mode", label="Mode", type="select", options=[OptionSpec(value="a", label="A"), OptionSpec(value="b", label="B")], default="a"),
    FieldSpec(key="enabled_thing", label="Thing", type="boolean", default=False),
    FieldSpec(key="lifetime", label="Lifetime", pattern=r"^\d+[smh]$"),
    FieldSpec(key="upper_me", label="Upper", transform="upper"),
]


def test_defaults_coercion_and_normalisation() -> None:
    validator = FieldSetValidator(FIELDS, "connector")
    result = validator.validate(
        {"username": " alice ", "password": "pw", "num_proxies": "3", "country_code": "us", "enabled_thing": "true", "lifetime": "10m", "upper_me": "abc", "unknown": "dropped"}
    )
    assert result == {
        "username": "alice",
        "password": "pw",
        "num_proxies": 3,
        "country_code": "US",
        "mode": "a",
        "enabled_thing": True,
        "lifetime": "10m",
        "upper_me": "ABC",
    }


def test_required_and_range_errors() -> None:
    validator = FieldSetValidator(FIELDS, "connector")
    with pytest.raises(ConfigValidationError) as excinfo:
        validator.validate({"num_proxies": 50, "country_code": "USA", "mode": "zzz", "lifetime": "soon"})
    message = str(excinfo.value)
    assert "Username is required" in message
    assert "Password is required" in message
    assert "Proxies must be at most 10" in message
    assert "2-letter country code" in message
    assert "Mode must be one of" in message
    assert "Lifetime has an invalid format" in message


def test_show_when_hides_and_drops_fields() -> None:
    fields = [
        FieldSpec(key="num_proxies", label="Proxies", type="number", required=True),
        FieldSpec(
            key="session_minutes",
            label="Session",
            type="number",
            required=True,
            show_when=Condition.model_validate({"field": "credential.proxy_type", "in": ["residential", "mobile"]}),
        ),
    ]
    validator = FieldSetValidator(fields, "connector")
    # Hidden for ISP: not required and any stale value is dropped.
    assert validator.validate({"num_proxies": 2, "session_minutes": 30}, {"proxy_type": "isp"}) == {"num_proxies": 2}
    # Visible for residential: required.
    with pytest.raises(ConfigValidationError):
        validator.validate({"num_proxies": 2}, {"proxy_type": "residential"})
    assert validator.validate({"num_proxies": 2, "session_minutes": 5}, {"proxy_type": "residential"}) == {"num_proxies": 2, "session_minutes": 5}


def test_extra_allowed_keys_survive() -> None:
    validator = FieldSetValidator([FieldSpec(key="token", label="Token", required=True)], "credential", extra_allowed={"customer_id"})
    assert validator.validate({"token": "t", "customer_id": "c1", "other": "x"}) == {"token": "t", "customer_id": "c1"}


def test_preset_options_membership() -> None:
    validator = FieldSetValidator(
        [FieldSpec(key="country_code", label="Country", type="select", options_preset="countries")],
        "connector",
        presets={"countries": [OptionSpec(value="", label="All"), OptionSpec(value="US", label="United States")]},
    )
    assert validator.validate({"country_code": ""}) == {}
    assert validator.validate({"country_code": "US"}) == {"country_code": "US"}
    with pytest.raises(ConfigValidationError):
        validator.validate({"country_code": "XX"})


def test_remote_options_skip_static_membership_only_when_active() -> None:
    from api.providers.sdk.descriptor import Condition

    field = FieldSpec(
        key="country_code",
        label="Country",
        type="select",
        options_preset="countries",
        options_from="zone_countries",
        options_from_when=Condition.model_validate({"field": "connector.proxy_type", "in": ["isp"]}),
    )
    validator = FieldSetValidator([field], "connector", presets={"countries": [OptionSpec(value="US", label="US")]})
    # ISP: options come from the vendor, so any code is accepted.
    assert validator.validate({"country_code": "xx", "proxy_type": "isp"}) == {"country_code": "xx"}
    # Residential: static preset applies.
    with pytest.raises(ConfigValidationError):
        validator.validate({"country_code": "xx", "proxy_type": "residential"})
    assert validator.validate({"country_code": "us", "proxy_type": "residential"}) == {"country_code": "US"}


def test_field_spec_option_source_rules() -> None:
    with pytest.raises(ValueError, match="requires options_from_when"):
        FieldSpec(key="a", label="A", type="select", options_preset="countries", options_from="x")
    with pytest.raises(ValueError, match="only applies to number"):
        FieldSpec(key="a", label="A", max_from_option=[{"field": "b", "extra": "n"}])  # type: ignore[list-item]
