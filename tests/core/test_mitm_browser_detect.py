# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for browser detection from User-Agent strings."""

from api.core.mitm.browser_detect import detect_browser
from api.models.project import MitmBrowser


class TestDetectBrowser:
    """Tests for detect_browser()."""

    def test_chrome_user_agent(self) -> None:
        """Chrome UA should return MitmBrowser.CHROME."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        assert detect_browser(ua) == MitmBrowser.CHROME

    def test_firefox_user_agent(self) -> None:
        """Firefox UA should return MitmBrowser.FIREFOX."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
        assert detect_browser(ua) == MitmBrowser.FIREFOX

    def test_safari_user_agent(self) -> None:
        """Safari UA (without Chrome) should return MitmBrowser.SAFARI."""
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        assert detect_browser(ua) == MitmBrowser.SAFARI

    def test_edge_user_agent(self) -> None:
        """Edge UA should return MitmBrowser.EDGE (not CHROME despite containing Chrome/)."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        assert detect_browser(ua) == MitmBrowser.EDGE

    def test_edge_detected_before_chrome(self) -> None:
        """Edge UA contains 'Chrome/' but should be detected as edge first."""
        ua = "Mozilla/5.0 Chrome/131.0 Edg/131.0"
        assert detect_browser(ua) == MitmBrowser.EDGE

    def test_empty_string_returns_default(self) -> None:
        """Empty string should return the default browser."""
        assert detect_browser("") == MitmBrowser.CHROME

    def test_unknown_user_agent_returns_default(self) -> None:
        """Unknown UA should return the default browser."""
        assert detect_browser("MyCustomBot/1.0") == MitmBrowser.CHROME

    def test_none_like_empty_returns_default(self) -> None:
        """Empty-ish UA should return default."""
        assert detect_browser("   ") == MitmBrowser.CHROME

    def test_chrome_without_safari(self) -> None:
        """Chrome UA without Safari suffix still detected as chrome."""
        ua = "Mozilla/5.0 Chrome/131.0.0.0"
        assert detect_browser(ua) == MitmBrowser.CHROME
