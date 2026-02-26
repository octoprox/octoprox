# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for username parameter parsing."""

from api.core.username_params import parse_proxy_username


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
