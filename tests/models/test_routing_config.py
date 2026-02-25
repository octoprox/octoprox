# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for RoutingConfig model and validate_routing_config."""

import pytest
from pydantic import ValidationError

from api.models.connector import RoutingConfig, validate_routing_config


class TestRoutingConfig:
    """Tests for RoutingConfig Pydantic model."""

    def test_empty_config(self) -> None:
        """Test that empty config is valid (no restrictions)."""
        config = RoutingConfig()
        assert config.domain_whitelist == []
        assert config.domain_blacklist == []

    def test_whitelist_only(self) -> None:
        """Test config with only whitelist."""
        config = RoutingConfig(domain_whitelist=["example.com", "test.org"])
        assert config.domain_whitelist == ["example.com", "test.org"]
        assert config.domain_blacklist == []

    def test_blacklist_only(self) -> None:
        """Test config with only blacklist."""
        config = RoutingConfig(domain_blacklist=["blocked.com"])
        assert config.domain_blacklist == ["blocked.com"]
        assert config.domain_whitelist == []

    def test_mutual_exclusivity_raises(self) -> None:
        """Test that setting both whitelist and blacklist raises ValueError."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            RoutingConfig(
                domain_whitelist=["example.com"],
                domain_blacklist=["blocked.com"],
            )

    def test_normalizes_to_lowercase(self) -> None:
        """Test that domains are normalized to lowercase."""
        config = RoutingConfig(domain_whitelist=["EXAMPLE.COM", "Test.Org"])
        assert config.domain_whitelist == ["example.com", "test.org"]

    def test_strips_whitespace(self) -> None:
        """Test that whitespace is stripped from domains."""
        config = RoutingConfig(domain_whitelist=["  example.com  ", " test.org "])
        assert config.domain_whitelist == ["example.com", "test.org"]

    def test_removes_empty_strings(self) -> None:
        """Test that empty strings are removed from lists."""
        config = RoutingConfig(domain_whitelist=["example.com", "", "  ", "test.org"])
        assert config.domain_whitelist == ["example.com", "test.org"]

    def test_both_empty_lists_is_valid(self) -> None:
        """Test that both lists being empty is valid (no restriction)."""
        config = RoutingConfig(domain_whitelist=[], domain_blacklist=[])
        assert config.domain_whitelist == []
        assert config.domain_blacklist == []

    def test_one_empty_one_populated_is_valid(self) -> None:
        """Test that one empty list with one populated is valid."""
        config = RoutingConfig(domain_whitelist=["x.com"], domain_blacklist=[])
        assert config.domain_whitelist == ["x.com"]
        assert config.domain_blacklist == []


class TestValidateRoutingConfig:
    """Tests for validate_routing_config function."""

    def test_empty_config(self) -> None:
        """Test that empty config returns empty dict."""
        result = validate_routing_config({})
        assert result == {}

    def test_whitelist_config(self) -> None:
        """Test whitelist config returns only non-empty fields."""
        result = validate_routing_config({"domain_whitelist": ["example.com", "test.org"]})
        assert result == {"domain_whitelist": ["example.com", "test.org"]}
        assert "domain_blacklist" not in result

    def test_blacklist_config(self) -> None:
        """Test blacklist config returns only non-empty fields."""
        result = validate_routing_config({"domain_blacklist": ["blocked.com"]})
        assert result == {"domain_blacklist": ["blocked.com"]}
        assert "domain_whitelist" not in result

    def test_both_lists_raises(self) -> None:
        """Test that both whitelist and blacklist raises ValidationError."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            validate_routing_config({
                "domain_whitelist": ["a.com"],
                "domain_blacklist": ["b.com"],
            })

    def test_normalizes_domains(self) -> None:
        """Test that domains are normalized in output."""
        result = validate_routing_config({"domain_whitelist": ["  EXAMPLE.COM  "]})
        assert result == {"domain_whitelist": ["example.com"]}

    def test_empty_lists_are_excluded(self) -> None:
        """Test that empty lists are excluded from output."""
        result = validate_routing_config({"domain_whitelist": [], "domain_blacklist": []})
        assert result == {}

    def test_only_whitespace_entries_produce_empty_result(self) -> None:
        """Test that lists with only whitespace entries are treated as empty."""
        result = validate_routing_config({"domain_whitelist": ["", "  "]})
        assert result == {}
