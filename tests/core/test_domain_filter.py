# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for domain-based filtering logic."""

from api.core.domain_filter import is_domain_allowed, matches_domain
from api.models.connector import RoutingConfig


class TestMatchesDomain:
    """Tests for matches_domain() function."""

    def test_exact_match(self) -> None:
        """Test exact domain match."""
        assert matches_domain("bing.com", "bing.com") is True

    def test_subdomain_match(self) -> None:
        """Test that a subdomain matches the parent domain."""
        assert matches_domain("www.bing.com", "bing.com") is True

    def test_deep_subdomain_match(self) -> None:
        """Test that deeply nested subdomains match."""
        assert matches_domain("x.y.z.bing.com", "bing.com") is True

    def test_no_match_different_domain(self) -> None:
        """Test that different domains don't match."""
        assert matches_domain("google.com", "bing.com") is False

    def test_no_match_partial_suffix(self) -> None:
        """Test that partial suffix doesn't match (notbing.com != bing.com)."""
        assert matches_domain("notbing.com", "bing.com") is False

    def test_subdomain_pattern_exact(self) -> None:
        """Test exact match on subdomain pattern."""
        assert matches_domain("x.bing.com", "x.bing.com") is True

    def test_subdomain_pattern_with_sub(self) -> None:
        """Test subdomain of a subdomain pattern."""
        assert matches_domain("sub.x.bing.com", "x.bing.com") is True

    def test_subdomain_pattern_no_match_sibling(self) -> None:
        """Test that sibling subdomains don't match."""
        assert matches_domain("y.bing.com", "x.bing.com") is False

    def test_case_insensitive(self) -> None:
        """Test that matching is case-insensitive."""
        assert matches_domain("WWW.BING.COM", "bing.com") is True
        assert matches_domain("www.bing.com", "BING.COM") is True

    def test_trailing_dot_handling(self) -> None:
        """Test that trailing dots (FQDN notation) are handled."""
        assert matches_domain("bing.com.", "bing.com") is True
        assert matches_domain("bing.com", "bing.com.") is True
        assert matches_domain("www.bing.com.", "bing.com.") is True

    def test_single_label_domain(self) -> None:
        """Test matching with a single-label domain."""
        assert matches_domain("localhost", "localhost") is True
        assert matches_domain("sub.localhost", "localhost") is True

    def test_empty_strings(self) -> None:
        """Test with empty strings."""
        assert matches_domain("", "") is True
        assert matches_domain("bing.com", "") is False


class TestIsDomainAllowed:
    """Tests for is_domain_allowed() function."""

    def test_empty_config_allows_all(self) -> None:
        """Test that empty routing config allows all domains."""
        config = RoutingConfig()
        assert is_domain_allowed("anything.com", config) is True
        assert is_domain_allowed("example.org", config) is True

    def test_whitelist_allows_matching_domain(self) -> None:
        """Test that whitelisted domains are allowed."""
        config = RoutingConfig(domain_whitelist=["example.com", "test.org"])
        assert is_domain_allowed("example.com", config) is True
        assert is_domain_allowed("www.example.com", config) is True
        assert is_domain_allowed("test.org", config) is True

    def test_whitelist_blocks_non_matching_domain(self) -> None:
        """Test that non-whitelisted domains are blocked."""
        config = RoutingConfig(domain_whitelist=["example.com"])
        assert is_domain_allowed("other.com", config) is False
        assert is_domain_allowed("notexample.com", config) is False

    def test_blacklist_blocks_matching_domain(self) -> None:
        """Test that blacklisted domains are blocked."""
        config = RoutingConfig(domain_blacklist=["blocked.com", "spam.org"])
        assert is_domain_allowed("blocked.com", config) is False
        assert is_domain_allowed("www.blocked.com", config) is False
        assert is_domain_allowed("spam.org", config) is False

    def test_blacklist_allows_non_matching_domain(self) -> None:
        """Test that non-blacklisted domains are allowed."""
        config = RoutingConfig(domain_blacklist=["blocked.com"])
        assert is_domain_allowed("allowed.com", config) is True
        assert is_domain_allowed("example.org", config) is True

    def test_whitelist_with_subdomain_matching(self) -> None:
        """Test whitelist with hierarchical subdomain matching."""
        config = RoutingConfig(domain_whitelist=["bing.com"])
        assert is_domain_allowed("bing.com", config) is True
        assert is_domain_allowed("www.bing.com", config) is True
        assert is_domain_allowed("images.bing.com", config) is True
        assert is_domain_allowed("google.com", config) is False

    def test_blacklist_with_subdomain_matching(self) -> None:
        """Test blacklist with hierarchical subdomain matching."""
        config = RoutingConfig(domain_blacklist=["ads.example.com"])
        assert is_domain_allowed("ads.example.com", config) is False
        assert is_domain_allowed("tracker.ads.example.com", config) is False
        assert is_domain_allowed("example.com", config) is True
        assert is_domain_allowed("www.example.com", config) is True
