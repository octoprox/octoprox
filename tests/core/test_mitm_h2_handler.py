# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MitmHandler HTTP/2 code path."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import h2.config
import h2.connection
import pytest

from api.core.mitm.handler import MitmHandler
from api.models.project import MitmEngine, MitmMode


def _make_h2_client_data(
    settings=None,
    window_increment=0,
    method="GET",
    path="/",
    authority="example.com",
    scheme="https",
):
    """Build raw bytes a real HTTP/2 client would send."""
    config = h2.config.H2Configuration(
        client_side=True,
        header_encoding="utf-8",
    )
    conn = h2.connection.H2Connection(config=config)
    conn.initiate_connection()

    if settings:
        conn.update_settings(settings)

    data = conn.data_to_send()

    if window_increment > 0:
        conn.increment_flow_control_window(window_increment)
        data += conn.data_to_send()

    headers = [
        (":method", method),
        (":path", path),
        (":authority", authority),
        (":scheme", scheme),
        ("user-agent", "TestBot/1.0"),
        ("accept", "*/*"),
    ]
    conn.send_headers(1, headers, end_stream=True)
    data += conn.data_to_send()

    return data


def _make_h2_concurrent_data():
    """Build raw bytes with 3 HTTP/2 requests on streams 1, 3, 5."""
    config = h2.config.H2Configuration(
        client_side=True,
        header_encoding="utf-8",
    )
    conn = h2.connection.H2Connection(config=config)
    conn.initiate_connection()
    data = conn.data_to_send()

    for stream_id, path in [(1, "/a"), (3, "/b"), (5, "/c")]:
        headers = [
            (":method", "GET"),
            (":path", path),
            (":authority", "example.com"),
            (":scheme", "https"),
        ]
        conn.send_headers(stream_id, headers, end_stream=True)
        data += conn.data_to_send()

    return data


def _make_ssl_object(alpn_protocol="h2"):
    """Create a mock ssl_object with configurable ALPN."""
    ssl_object = MagicMock()
    ssl_object.selected_alpn_protocol.return_value = alpn_protocol
    ssl_object.version.return_value = "TLSv1.3"
    ssl_object.cipher.return_value = ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    ssl_object.shared_ciphers.return_value = [
        ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    ]
    return ssl_object


class TestMitmH2Handler:
    """Tests for MitmHandler HTTP/2 code path."""

    @pytest.fixture
    def cert_manager(self) -> MagicMock:
        """Mock TLSCertManager."""
        cm = MagicMock()
        cm.get_server_ssl_context.return_value = MagicMock()
        return cm

    @pytest.fixture
    def handler(self, cert_manager: MagicMock) -> MitmHandler:
        """Create a MitmHandler with mocked cert manager."""
        return MitmHandler(cert_manager)

    async def test_h2_relay_called_with_correct_request(
        self,
        handler: MitmHandler,
    ) -> None:
        """Send HTTP/2 data and verify relay.send_request is called with correct method/url."""
        h2_data = _make_h2_client_data(
            method="GET",
            path="/test-path",
            authority="example.com",
        )

        reader = asyncio.StreamReader()
        reader.feed_data(h2_data)
        reader.feed_eof()

        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        transport.is_closing.return_value = False
        writer.transport = transport
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer._transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.MATCH_UA
        project.tls_mitm_engine = MitmEngine.CURL_CFFI
        project.tls_mitm_browser = None

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (
            200,
            "OK",
            [("content-type", "text/plain")],
            b"response body",
        )
        mock_relay.close = AsyncMock()
        mock_relay.last_upstream_headers = []

        ssl_object = _make_ssl_object(alpn_protocol="h2")

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
            patch("api.core.mitm.handler.pop_client_hello", return_value=None),
        ):
            new_transport = MagicMock()
            new_transport.get_extra_info.return_value = ssl_object
            mock_loop.return_value.start_tls = AsyncMock(return_value=new_transport)

            bs, br = await handler.handle(
                reader,
                writer,
                "example.com",
                443,
                proxy,
                project,
            )

        mock_relay.send_request.assert_called_once()
        call_args = mock_relay.send_request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "https://example.com/test-path"
        mock_relay.close.assert_called_once()

    async def test_h1_fallback_when_no_h2(
        self,
        handler: MitmHandler,
    ) -> None:
        """Mock selected_alpn_protocol returning http/1.1 and verify the h11 path works."""
        request_data = b"GET /fallback HTTP/1.1\r\nHost: example.com\r\nUser-Agent: TestBot/1.0\r\n\r\n"

        reader = asyncio.StreamReader()
        reader.feed_data(request_data)
        reader.feed_eof()

        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        transport.is_closing.return_value = False
        writer.transport = transport
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer._transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.MATCH_UA
        project.tls_mitm_engine = MitmEngine.CURL_CFFI
        project.tls_mitm_browser = None

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (
            200,
            "OK",
            [("content-type", "text/plain")],
            b"ok",
        )
        mock_relay.close = AsyncMock()
        mock_relay.last_upstream_headers = []

        ssl_object = _make_ssl_object(alpn_protocol="http/1.1")

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
            patch("api.core.mitm.handler.pop_client_hello", return_value=None),
        ):
            new_transport = MagicMock()
            new_transport.get_extra_info.return_value = ssl_object
            mock_loop.return_value.start_tls = AsyncMock(return_value=new_transport)

            bs, br = await handler.handle(
                reader,
                writer,
                "example.com",
                443,
                proxy,
                project,
            )

        # verify relay was called via the h11 path
        mock_relay.send_request.assert_called_once()
        call_args = mock_relay.send_request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "https://example.com/fallback"

        # verify response was written back (h11 writes raw bytes)
        assert br > 0
        mock_relay.close.assert_called_once()

    async def test_h2_fingerprint_captured(
        self,
        handler: MitmHandler,
    ) -> None:
        """Send HTTP/2 data with known SETTINGS/WINDOW_UPDATE, verify tls_info has fingerprint."""
        # setting 4 = INITIAL_WINDOW_SIZE
        h2_data = _make_h2_client_data(
            settings={
                4: 16777216,
            },
            window_increment=15663105,
            method="GET",
            path="/fp",
            authority="example.com",
        )

        reader = asyncio.StreamReader()
        reader.feed_data(h2_data)
        reader.feed_eof()

        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        transport.is_closing.return_value = False
        writer.transport = transport
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer._transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.MATCH_UA
        project.tls_mitm_engine = MitmEngine.CURL_CFFI
        project.tls_mitm_browser = None

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (
            200,
            "OK",
            [("content-type", "text/plain")],
            b"fingerprint response",
        )
        mock_relay.close = AsyncMock()
        mock_relay.last_upstream_headers = []

        ssl_object = _make_ssl_object(alpn_protocol="h2")

        # capture tls_info as it gets mutated inside _handle_h2
        captured_tls_info = {}

        original_record = handler._record_request

        def capture_record(**kwargs):
            """Intercept _record_request to capture tls_info."""
            if kwargs.get("tls_info"):
                captured_tls_info.update(kwargs["tls_info"])

        handler._record_request = capture_record

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
            patch("api.core.mitm.handler.pop_client_hello", return_value=None),
        ):
            new_transport = MagicMock()
            new_transport.get_extra_info.return_value = ssl_object
            mock_loop.return_value.start_tls = AsyncMock(return_value=new_transport)

            await handler.handle(
                reader,
                writer,
                "example.com",
                443,
                proxy,
                project,
            )

        # verify fingerprint fields are present
        assert "h2_fingerprint" in captured_tls_info
        assert "h2_fingerprint_hash" in captured_tls_info
        assert "h2_frames" in captured_tls_info

        # verify the fingerprint string contains the INITIAL_WINDOW_SIZE setting
        assert "4:16777216" in captured_tls_info["h2_fingerprint"]

        # verify the fingerprint string contains the window update delta
        assert "15663105" in captured_tls_info["h2_fingerprint"]

        # verify h2_frames is valid json with expected frame types
        frames = json.loads(captured_tls_info["h2_frames"])
        frame_types = [f["type"] for f in frames]
        assert "SETTINGS" in frame_types
        assert "WINDOW_UPDATE" in frame_types
        assert "HEADERS" in frame_types

    async def test_h2_concurrent_streams(
        self,
        handler: MitmHandler,
    ) -> None:
        """Send 3 HTTP/2 requests on different streams (1, 3, 5), verify 3 relay calls."""
        h2_data = _make_h2_concurrent_data()

        reader = asyncio.StreamReader()
        reader.feed_data(h2_data)
        reader.feed_eof()

        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        transport.is_closing.return_value = False
        writer.transport = transport
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer._transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.MATCH_UA
        project.tls_mitm_engine = MitmEngine.CURL_CFFI
        project.tls_mitm_browser = None

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (
            200,
            "OK",
            [("content-type", "text/plain")],
            b"stream response",
        )
        mock_relay.close = AsyncMock()
        mock_relay.last_upstream_headers = []

        ssl_object = _make_ssl_object(alpn_protocol="h2")

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
            patch("api.core.mitm.handler.pop_client_hello", return_value=None),
        ):
            new_transport = MagicMock()
            new_transport.get_extra_info.return_value = ssl_object
            mock_loop.return_value.start_tls = AsyncMock(return_value=new_transport)

            await handler.handle(
                reader,
                writer,
                "example.com",
                443,
                proxy,
                project,
            )

        # verify relay.send_request was called 3 times (one per stream)
        assert mock_relay.send_request.call_count == 3

        # verify each request had the correct url
        called_urls = [
            call.args[1]
            for call in mock_relay.send_request.call_args_list
        ]
        assert "https://example.com/a" in called_urls
        assert "https://example.com/b" in called_urls
        assert "https://example.com/c" in called_urls

        mock_relay.close.assert_called_once()
