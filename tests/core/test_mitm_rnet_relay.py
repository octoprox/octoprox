# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for RnetRelay."""

from unittest.mock import AsyncMock, MagicMock, patch

from api.core.mitm.rnet_relay import _IMPERSONATE_MAP, RnetRelay
from api.models.project import MitmBrowser, MitmMode


class TestRnetRelay:
    """Tests for RnetRelay."""

    async def test_match_ua_forwards_user_agent(self) -> None:
        """In match_ua mode, the client's User-Agent should be forwarded."""
        relay = RnetRelay(mode=MitmMode.MATCH_UA)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.bytes = AsyncMock(return_value=b"<html></html>")

        with patch.object(relay, "_get_or_create_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_client_factory.return_value = mock_client

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0", "Accept": "text/html"}
            status, reason, resp_headers, body = await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        assert status == 200
        # User-Agent should be in forwarded headers
        call_kwargs = mock_client.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "User-Agent" in forwarded

    async def test_override_ua_removes_user_agent(self) -> None:
        """In override_ua mode, the client's User-Agent should be removed."""
        relay = RnetRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.FIREFOX)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.bytes = AsyncMock(return_value=b"ok")

        with patch.object(relay, "_get_or_create_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_client_factory.return_value = mock_client

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0", "Accept": "text/html"}
            await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        call_kwargs = mock_client.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "User-Agent" not in forwarded
        assert "Accept" in forwarded

    def test_impersonate_map_has_all_browsers(self) -> None:
        """All browser options should be in the impersonation map."""
        assert MitmBrowser.CHROME in _IMPERSONATE_MAP
        assert MitmBrowser.FIREFOX in _IMPERSONATE_MAP
        assert MitmBrowser.SAFARI in _IMPERSONATE_MAP
        assert MitmBrowser.EDGE in _IMPERSONATE_MAP

    async def test_match_ua_detects_firefox(self) -> None:
        """match_ua should detect Firefox and use firefox impersonation."""
        relay = RnetRelay(mode=MitmMode.MATCH_UA)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.bytes = AsyncMock(return_value=b"")

        with patch("api.core.mitm.rnet_relay.Client") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_client_cls.return_value = mock_client

            headers = {"User-Agent": "Mozilla/5.0 Firefox/133.0"}
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        mock_client_cls.assert_called_with(impersonate=_IMPERSONATE_MAP[MitmBrowser.FIREFOX])

    async def test_override_ua_uses_configured_browser(self) -> None:
        """override_ua should use the configured browser."""
        relay = RnetRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.EDGE)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.bytes = AsyncMock(return_value=b"")

        with patch("api.core.mitm.rnet_relay.Client") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request.return_value = mock_response
            mock_client_cls.return_value = mock_client

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0"}
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        mock_client_cls.assert_called_with(impersonate=_IMPERSONATE_MAP[MitmBrowser.EDGE])

    async def test_close_clears_client(self) -> None:
        """close() should clear the client reference."""
        relay = RnetRelay(mode=MitmMode.MATCH_UA)
        relay._client = MagicMock()

        await relay.close()
        assert relay._client is None

    async def test_close_when_no_client(self) -> None:
        """close() should be safe when no client exists."""
        relay = RnetRelay(mode=MitmMode.MATCH_UA)
        await relay.close()  # Should not raise
