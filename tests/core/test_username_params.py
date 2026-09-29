# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for username parameter parsing."""

from api.core.username_params import (
    LOCATION_INVALID_CITY,
    LOCATION_INVALID_COUNTRY,
    LOCATION_INVALID_STATE,
    LOCATION_NEEDS_COUNTRY,
    UsernameParams,
    parse_proxy_username,
    parse_username_params,
)
from api.models.location import LocationTarget


class TestParseProxyUsername:
    """Tests for parse_proxy_username() function."""

    def test_plain_username(self) -> None:
        """Test plain username without session ID."""
        assert parse_proxy_username("myuser") == ("myuser", None)

    def test_username_with_sessid(self) -> None:
        """Test username with session ID."""
        assert parse_proxy_username("myuser-sessid-abc123") == ("myuser", "abc123")

    def test_hyphenated_username_with_sessid(self) -> None:
        """Test hyphenated username with session ID."""
        assert parse_proxy_username("my-project-sessid-abc123") == ("my-project", "abc123")

    def test_sessid_value_with_hyphens(self) -> None:
        """Test that session ID can contain hyphens."""
        assert parse_proxy_username("myuser-sessid-abc-123-def") == ("myuser", "abc-123-def")

    def test_empty_sessid_value(self) -> None:
        """Test that empty session ID value is treated as absent."""
        assert parse_proxy_username("myuser-sessid-") == ("myuser", None)

    def test_no_username_before_separator(self) -> None:
        """Test that missing username before separator returns raw string."""
        assert parse_proxy_username("-sessid-abc123") == ("-sessid-abc123", None)

    def test_partial_keyword_no_match(self) -> None:
        """Test that partial keyword like -sess- does not trigger parsing."""
        assert parse_proxy_username("myuser-sess-abc123") == ("myuser-sess-abc123", None)

    def test_multiple_sessid_separators(self) -> None:
        """Test that only the first -sessid- separator is used."""
        assert parse_proxy_username("a-sessid-b-sessid-c") == ("a", "b-sessid-c")

    def test_empty_username(self) -> None:
        """Test empty string input."""
        assert parse_proxy_username("") == ("", None)

    def test_sessid_keyword_alone(self) -> None:
        """Test that 'sessid' alone is treated as a plain username."""
        assert parse_proxy_username("sessid") == ("sessid", None)

    def test_username_ending_with_sessid(self) -> None:
        """Test username that ends with 'sessid' but no separator."""
        assert parse_proxy_username("myuser-sessid") == ("myuser-sessid", None)


class TestParseUsernameParams:
    """Tests for parse_username_params() (sessid + country)."""

    def test_plain_username(self) -> None:
        assert parse_username_params("myuser") == UsernameParams("myuser", None, None)

    def test_sessid_only(self) -> None:
        assert parse_username_params("myuser-sessid-abc-123") == UsernameParams("myuser", "abc-123", None)

    def test_country_only(self) -> None:
        assert parse_username_params("myuser-cc-us") == UsernameParams("myuser", None, LocationTarget(country="US"))

    def test_country_is_upper_cased(self) -> None:
        assert parse_username_params("myuser-cc-Gb").country == "GB"

    def test_uk_is_an_alias_of_gb(self) -> None:
        assert parse_username_params("myuser-cc-uk").country == "GB"

    def test_malformed_country_is_an_error(self) -> None:
        params = parse_username_params("myuser-cc-usa")
        assert params.country == "USA" and params.location_error == LOCATION_INVALID_COUNTRY

    def test_sessid_then_country(self) -> None:
        assert parse_username_params("myuser-sessid-abc-cc-gb") == UsernameParams("myuser", "abc", LocationTarget(country="GB"))

    def test_country_then_sessid(self) -> None:
        assert parse_username_params("myuser-cc-gb-sessid-abc") == UsernameParams("myuser", "abc", LocationTarget(country="GB"))

    def test_sessid_with_hyphens_before_country(self) -> None:
        assert parse_username_params("myuser-sessid-a-b-c-cc-de") == UsernameParams("myuser", "a-b-c", LocationTarget(country="DE"))

    def test_hyphenated_username_with_both(self) -> None:
        assert parse_username_params("my-project-cc-fr-sessid-x") == UsernameParams("my-project", "x", LocationTarget(country="FR"))

    def test_empty_country_value(self) -> None:
        assert parse_username_params("myuser-cc-") == UsernameParams("myuser", None, None)

    def test_empty_country_before_sessid(self) -> None:
        assert parse_username_params("myuser-cc--sessid-abc") == UsernameParams("myuser", "abc", None)

    def test_empty_sessid_before_country(self) -> None:
        assert parse_username_params("myuser-sessid--cc-us") == UsernameParams("myuser", None, LocationTarget(country="US"))

    def test_no_username_before_country_separator(self) -> None:
        assert parse_username_params("-cc-us") == UsernameParams("-cc-us", None, None)

    def test_partial_keyword_no_match(self) -> None:
        assert parse_username_params("myuser-ccx-us") == UsernameParams("myuser-ccx-us", None, None)

    def test_repeated_country_separator_keeps_first(self) -> None:
        # The value runs to the end and is not a code, so the request is refused rather than guessed at.
        params = parse_username_params("a-cc-us-cc-gb")
        assert params.username == "a" and params.country == "US-CC-GB" and params.location_error == LOCATION_INVALID_COUNTRY

    def test_legacy_wrapper_ignores_country(self) -> None:
        assert parse_proxy_username("myuser-cc-us-sessid-abc") == ("myuser", "abc")

    def test_state_and_city(self) -> None:
        params = parse_username_params("myuser-cc-us-st-ny-city-new_york")
        assert params.location == LocationTarget(country="US", state="NY", city="new_york")
        assert params.location_error is None and params.country == "US"

    def test_any_order_with_sessid(self) -> None:
        params = parse_username_params("myuser-city-berlin-sessid-a-cc-de")
        assert params == UsernameParams("myuser", "a", LocationTarget(country="DE", city="berlin"))

    def test_city_is_slugified(self) -> None:
        assert parse_username_params("myuser-cc-fr-city-Saint-Denis").location.city == "saint_denis"
        assert parse_username_params("myuser-cc-us-city-LOS ANGELES").location.city == "los_angeles"

    def test_state_accepts_the_full_iso_code(self) -> None:
        assert parse_username_params("myuser-cc-us-st-US-NY").location.state == "NY"
        assert parse_username_params("myuser-cc-gb-st-eng").location.state == "ENG"

    def test_malformed_state_or_city_is_an_error(self) -> None:
        params = parse_username_params("myuser-cc-us-st-newyork")
        assert params.location == LocationTarget(country="US", state="NEWYORK")
        assert params.location_error == LOCATION_INVALID_STATE
        params = parse_username_params("myuser-cc-us-city-***")
        assert params.location == LocationTarget(country="US") and params.location_error == LOCATION_INVALID_CITY
        assert parse_username_params("myuser-st-newyork").location_error == LOCATION_INVALID_STATE

    def test_state_or_city_without_country_is_an_error(self) -> None:
        params = parse_username_params("myuser-st-ny")
        assert params.location == LocationTarget(state="NY") and params.location_error == LOCATION_NEEDS_COUNTRY
        assert parse_username_params("myuser-city-paris-sessid-x").location_error == LOCATION_NEEDS_COUNTRY
        assert parse_username_params("myuser-cc-fr-city-paris").location_error is None

    def test_empty_state_and_city_values_are_absent(self) -> None:
        assert parse_username_params("myuser-cc-us-st--city-") == UsernameParams("myuser", None, LocationTarget(country="US"))
