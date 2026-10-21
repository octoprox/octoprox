# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for CffiRelay."""

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import CertificateVerifyError, ProxyError

from api.core.mitm.base import UpstreamRefusedError
from api.core.mitm.cffi_relay import CffiRelay
from api.core.tls_cert_manager import TLSCertManager
from api.models.project import MitmBrowser, MitmMode


class TestCffiRelay:
    """Tests for CffiRelay."""

    async def test_match_ua_forwards_user_agent(self) -> None:
        """In match_ua mode, the client's User-Agent should be forwarded."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = [("Content-Type", "text/html")]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = mock_headers
        mock_response.content = b"<html></html>"

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            headers = [("User-Agent", "Mozilla/5.0 Chrome/131.0"), ("Accept", "text/html")]
            status, reason, resp_headers, body = await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        assert status == 200
        # User-Agent should be in the forwarded headers
        call_kwargs = mock_session.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", [])
        forwarded_keys = [k for k, _v in forwarded]
        assert "User-Agent" in forwarded_keys

    async def test_override_ua_removes_user_agent(self) -> None:
        """In override_ua mode, the client's User-Agent should be removed."""
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.FIREFOX)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = [("Content-Type", "text/html")]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = mock_headers
        mock_response.content = b"ok"

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            headers = [("User-Agent", "Mozilla/5.0 Chrome/131.0"), ("Accept", "text/html")]
            await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        # User-Agent should NOT be in forwarded headers (engine sets its own)
        call_kwargs = mock_session.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", [])
        forwarded_keys = [k for k, _v in forwarded]
        assert "User-Agent" not in forwarded_keys
        assert "Accept" in forwarded_keys

    async def test_match_ua_detects_chrome(self) -> None:
        """match_ua should detect Chrome from UA and use chrome impersonation."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = []
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = mock_headers
        mock_response.content = b""

        with patch("api.core.mitm.cffi_relay.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_cls.return_value = mock_session

            headers = [("User-Agent", "Mozilla/5.0 Chrome/131.0 Safari/537.36")]
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        assert mock_session_cls.call_args.kwargs["impersonate"] == "chrome"

    async def test_override_ua_uses_configured_browser(self) -> None:
        """override_ua should use the configured browser regardless of UA."""
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.SAFARI)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = []
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = mock_headers
        mock_response.content = b""

        with patch("api.core.mitm.cffi_relay.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_cls.return_value = mock_session

            headers = [("User-Agent", "Mozilla/5.0 Chrome/131.0")]
            await relay.send_request("GET", "https://example.com/", headers, None, "http://proxy:8080")

        # Should use safari, not chrome (despite Chrome UA)
        assert mock_session_cls.call_args.kwargs["impersonate"] == "safari"

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

    async def test_a_browser_switch_closes_the_replaced_session(self) -> None:
        """curl_cffi keeps a timer task running per session until it is closed."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        with patch("api.core.mitm.cffi_relay.AsyncSession", side_effect=lambda **_: AsyncMock()) as session_cls:
            chrome = await relay._get_or_create_session(MitmBrowser.CHROME)
            assert await relay._get_or_create_session(MitmBrowser.CHROME) is chrome
            safari = await relay._get_or_create_session(MitmBrowser.SAFARI)

        chrome.close.assert_awaited_once()
        safari.close.assert_not_awaited()
        assert session_cls.call_count == 2

    async def test_a_replaced_session_that_fails_to_close_does_not_fail_the_request(self) -> None:
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        with patch("api.core.mitm.cffi_relay.AsyncSession", side_effect=lambda **_: AsyncMock()):
            chrome = await relay._get_or_create_session(MitmBrowser.CHROME)
            chrome.close.side_effect = RuntimeError("curl_multi_cleanup")
            safari = await relay._get_or_create_session(MitmBrowser.SAFARI)

        assert relay._session is safari

    async def test_a_cancel_during_the_swap_still_cancels(self) -> None:
        """curl_cffi's close() suppresses CancelledError while it stops its timer task."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        timer_stopped = asyncio.Event()

        async def close_like_curl_cffi() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await timer_stopped.wait()

        with patch("api.core.mitm.cffi_relay.AsyncSession", side_effect=lambda **_: AsyncMock()):
            chrome = await relay._get_or_create_session(MitmBrowser.CHROME)
            chrome.close.side_effect = close_like_curl_cffi
            swap = asyncio.create_task(relay._get_or_create_session(MitmBrowser.SAFARI))
            await asyncio.sleep(0)  # the swap now waits on the old session's close
            swap.cancel()
            timer_stopped.set()
            with pytest.raises(asyncio.CancelledError):
                await swap

        chrome.close.assert_awaited_once()

    async def test_request_body_forwarded(self) -> None:
        """Request body should be forwarded correctly."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = []
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.reason = "Created"
        mock_response.headers = mock_headers
        mock_response.content = b""

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            body = b'{"key": "value"}'
            await relay.send_request(
                "POST", "https://example.com/api", [("Content-Type", "application/json")], body, "http://proxy:8080"
            )

        call_kwargs = mock_session.request.call_args
        assert call_kwargs.kwargs.get("data") == body or call_kwargs[1].get("data") == body

    async def test_client_hint_headers_stripped(self) -> None:
        """sec-ch-ua* headers should be stripped to avoid version mismatch with impersonation."""
        relay = CffiRelay(mode=MitmMode.MATCH_UA)

        mock_headers = MagicMock()
        mock_headers.multi_items.return_value = []
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = mock_headers
        mock_response.content = b""

        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session.request.return_value = mock_response
            mock_session_factory.return_value = mock_session

            headers = [
                ("User-Agent", "Mozilla/5.0 Chrome/131.0"),
                ("sec-ch-ua", '"Chromium";v="131"'),
                ("sec-ch-ua-mobile", "?0"),
                ("sec-ch-ua-platform", '"macOS"'),
                ("Accept", "text/html"),
                ("sec-fetch-dest", "document"),
            ]
            await relay.send_request(
                "GET", "https://example.com/", headers, None, "http://proxy:8080"
            )

        call_kwargs = mock_session.request.call_args
        forwarded = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", [])
        forwarded_keys = {k.lower() for k, _v in forwarded}
        # sec-ch-ua* headers must be stripped
        assert "sec-ch-ua" not in forwarded_keys
        assert "sec-ch-ua-mobile" not in forwarded_keys
        assert "sec-ch-ua-platform" not in forwarded_keys
        # sec-fetch-* and other headers must be preserved
        assert "sec-fetch-dest" in forwarded_keys
        assert "accept" in forwarded_keys


@contextlib.asynccontextmanager
async def refusing_proxy(status_line: bytes) -> AsyncIterator[str]:
    """A local upstream proxy that answers every CONNECT with ``status_line``."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        writer.write(status_line + b"\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://vendor-user:vendor-secret@127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


class TestUpstreamRefusal:
    """libcurl reports the vendor's status in its error text only; the relay reads it from there."""

    @pytest.mark.parametrize(("status_line", "status"), [
        (b"HTTP/1.1 429 Too Many Requests", 429),
        (b"HTTP/1.1 407 Proxy Authentication Required", 407),
        (b"HTTP/1.1 502 Bad Gateway", 502),
    ])
    async def test_a_refused_tunnel_carries_the_vendors_status(self, status_line: bytes, status: int) -> None:
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.CHROME)
        try:
            async with refusing_proxy(status_line) as proxy_url:
                with pytest.raises(UpstreamRefusedError) as refused:
                    await relay.send_request("GET", "https://example.com/", [], None, proxy_url)
        finally:
            await relay.close()

        assert refused.value.status == status
        assert str(refused.value).startswith(f"Upstream proxy refused the tunnel: {status} ")

    async def test_another_proxy_error_is_left_alone(self) -> None:
        relay = CffiRelay(mode=MitmMode.MATCH_UA)
        error = ProxyError("Failed to perform, curl: (5) Could not resolve proxy: vendor.invalid", 5)
        with patch.object(relay, "_get_or_create_session") as mock_session_factory:
            mock_session_factory.return_value.request = AsyncMock(side_effect=error)
            with pytest.raises(ProxyError) as raised:
                await relay.send_request("GET", "https://example.com/", [], None, "http://vendor.invalid:8080")

        assert raised.value is error


@contextlib.asynccontextmanager
async def tls_site(context: ssl.SSLContext) -> AsyncIterator[int]:
    """A local HTTPS site that answers every request with an empty 200; its port."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def tunnelling_proxy(site_port: int, context: ssl.SSLContext | None = None) -> AsyncIterator[str]:
    """A local upstream proxy that opens every CONNECT to the local site; given ``context``, an https:// one for localhost."""

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        site_reader, site_writer = await asyncio.open_connection("127.0.0.1", site_port)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, site_writer), pipe(site_reader, writer))

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"https://localhost:{port}" if context else f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


class TestCaBundle:
    """curl_cffi 0.15 and later prefer the OS CA bundle; the engine names certifi's, as 5.x's curl_cffi did.

    libcurl's built-in certificate directory (/etc/ssl/certs on Linux) still
    applies on top; these tests cover the bundle, not the directory.
    """

    @pytest.fixture
    def site_ca(self, tmp_path: Path) -> TLSCertManager:
        """A throwaway CA that signs the local site's certificate, and the https:// proxy's."""
        ca = TLSCertManager(tmp_path / "ca.crt", tmp_path / "ca.key")
        ca._generate_ca()
        return ca

    async def _get(self, site_ca: TLSCertManager, *, https_proxy: bool = False) -> int:
        """GET the local site through the relay, real libcurl and a local CONNECT proxy; the status."""
        context = site_ca.get_server_ssl_context("localhost")
        relay = CffiRelay(mode=MitmMode.OVERRIDE_UA, browser=MitmBrowser.CHROME)
        async with tls_site(context) as port, tunnelling_proxy(port, context if https_proxy else None) as proxy_url:
            try:
                status, _reason, _headers, _body = await relay.send_request(
                    "GET", f"https://localhost:{port}/", [], None, proxy_url
                )
            finally:
                await relay.close()
        return status

    async def test_a_site_is_trusted_when_certifi_s_bundle_holds_its_ca(self, site_ca: TLSCertManager) -> None:
        with patch("certifi.where", return_value=str(site_ca.ca_cert_path)):
            assert await self._get(site_ca) == 200

    async def test_a_site_whose_ca_certifi_does_not_hold_is_refused(self, site_ca: TLSCertManager) -> None:
        with pytest.raises(CertificateVerifyError):
            await self._get(site_ca)

    # curl_cffi warns on every https:// site reached through an https://
    # proxy, in case the proxy speaks plain HTTP; this one speaks TLS.
    @pytest.mark.filterwarnings("ignore:Make sure you are using https over https proxy")
    async def test_an_https_proxy_is_trusted_when_certifi_s_bundle_holds_its_ca(self, site_ca: TLSCertManager) -> None:
        """libcurl verifies the proxy against a CA option of its own, which curl_cffi leaves unset once the site's is set."""
        with patch("certifi.where", return_value=str(site_ca.ca_cert_path)):
            assert await self._get(site_ca, https_proxy=True) == 200

    @pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
    async def test_a_ca_bundle_variable_does_not_replace_certifi_s(
        self, site_ca: TLSCertManager, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        """curl_cffi makes either variable the session's bundle; the relay's curl options are applied after it."""
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        monkeypatch.setenv(variable, str(site_ca.ca_cert_path))
        with pytest.raises(CertificateVerifyError):
            await self._get(site_ca)
