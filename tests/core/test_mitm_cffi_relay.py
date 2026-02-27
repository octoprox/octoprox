# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for CffiRelay."""

from unittest.mock import AsyncMock, MagicMock, patch

from api.core.mitm.cffi_relay import CffiRelay
from api.models.project import MitmBrowser, MitmMode


class TestCffiRelay:
    """Tests for CffiRelay."""

    async def test_match_ua_forwards_user_agent(self) -> None:
        """In match_ua mode, the client's User-Agent should be forwarded."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.content = b"<html></html>"

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0", "Accept": "text/html"}
            status, reason, resp_headers, body = await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        assert status == 200
        # User-Agent should be in the forwarded headers
        call_kwargs = mock_session.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "User-Agent" in forwarded

    async def test_override_ua_removes_user_agent(self) -> None:
        """In override_ua mode, the client's User-Agent should be removed."""
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.FIREFOX)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.content = b"ok"

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0", "Accept": "text/html"}
            await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        # User-Agent should NOT be in forwarded headers (engine sets its own)
        call_kwargs = mock_session.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "User-Agent" not in forwarded
        assert "Accept" in forwarded

    async def test_match_ua_detects_chrome(self) -> None:
        """match_ua should detect Chrome from UA and use chrome impersonation."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.content = b""

        with patch("api.core.mitm.cffi_relay.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_cls.return_value = mock_session

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0 Safari/537.36"}
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        mock_session_cls.assert_called_with(impersonate="chrome")

    async def test_override_ua_uses_configured_browser(self) -> None:
        """override_ua should use the configured browser regardless of UA."""
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.SAFARI)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.content = b""

        with patch("api.core.mitm.cffi_relay.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_cls.return_value = mock_session

            headers = {"User-Agent": "Mozilla/5.0 Chrome/131.0"}
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        # Should use safari, not chrome (despite Chrome UA)
        mock_session_cls.assert_called_with(impersonate="safari")

    async def test_close_closes_session(self) -> None:
        """close() should close the underlying session."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        mock_session = AsyncMock()
        relay._session = mock_session

        await relay.close()

        mock_session.close.assert_called_once()
        assert relay._session is None

    async def test_close_when_no_session(self) -> None:
        """close() should be safe when no session exists."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        await relay.close()  # Should not raise

    async def test_request_body_forwarded(self) -> None:
        """Request body should be forwarded correctly."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.reason = "Created"
        mock_response.headers = {}
        mock_response.content = b""

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            body = b'{"key": "value"}'
            await relay.send_request(
                "POST", "https://example.com/api", {"Content-Type": "application/json"}, body, "http://proxy:8080"
            )

        call_kwargs = mock_session.request.call_args
        assert call_kwargs.kwargs.get("data") == body or call_kwargs[1].get("data") == body
