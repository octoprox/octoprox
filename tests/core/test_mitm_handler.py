# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MitmHandler."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.mitm.handler import MitmHandler, _read_chunked_body
from api.models.project import MitmEngine, MitmMode


class TestReadChunkedBody:
    """Tests for the chunked body reader."""

    async def test_simple_chunked_body(self) -> None:
        """Read a simple chunked body with one chunk."""
        data = b"5\r\nhello\r\n0\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        result = await _read_chunked_body(reader)
        assert result == b"hello"

    async def test_multiple_chunks(self) -> None:
        """Read a body with multiple chunks."""
        data = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        result = await _read_chunked_body(reader)
        assert result == b"hello world"

    async def test_empty_chunked_body(self) -> None:
        """Read an empty chunked body (just terminator)."""
        data = b"0\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        result = await _read_chunked_body(reader)
        assert result == b""

    async def test_chunk_with_extension(self) -> None:
        """Read a chunk with extension (ignored)."""
        data = b"5;ext=val\r\nhello\r\n0\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        result = await _read_chunked_body(reader)
        assert result == b"hello"

    async def test_eof_mid_chunk(self) -> None:
        """Gracefully handle EOF in the middle of reading."""
        reader = asyncio.StreamReader()
        reader.feed_eof()

        result = await _read_chunked_body(reader)
        assert result == b""


class TestMitmHandler:
    """Tests for MitmHandler."""

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

    async def test_tls_handshake_failure_returns_zero(self, handler: MitmHandler) -> None:
        """TLS handshake failure should return (0, 0)."""
        reader = asyncio.StreamReader()
        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        writer.transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.MATCH_UA
        project.tls_mitm_engine = MitmEngine.CURL_CFFI
        project.tls_mitm_browser = None

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.start_tls = AsyncMock(side_effect=Exception("TLS failed"))
            bs, br = await handler.handle(reader, writer, "example.com", 443, proxy, project)

        assert bs == 0
        assert br == 0

    async def test_relay_send_request_called(self, handler: MitmHandler) -> None:
        """Verify relay.send_request() is called with parsed request data."""
        # Set up a minimal HTTP request
        request_data = b"GET /path HTTP/1.1\r\nHost: example.com\r\nUser-Agent: TestBot/1.0\r\n\r\n"

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
        mock_relay.send_request.return_value = (200, "OK", {"Content-Type": "text/plain"}, b"response body")
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay) as mock_factory,
        ):
            new_transport = MagicMock()
            mock_loop.return_value.start_tls = AsyncMock(return_value=new_transport)

            bs, br = await handler.handle(reader, writer, "example.com", 443, proxy, project)

        # Verify relay was created and called
        mock_factory.assert_called_once()
        mock_relay.send_request.assert_called_once()
        call_args = mock_relay.send_request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "https://example.com/path"
        assert "Host" in call_args[0][2]
        mock_relay.close.assert_called_once()

        # Verify response was written
        assert br > 0

    async def test_upstream_error_sends_502(self, handler: MitmHandler) -> None:
        """Upstream request failure should send 502 to client."""
        request_data = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"

        reader = asyncio.StreamReader()
        reader.feed_data(request_data)
        reader.feed_eof()

        writer = MagicMock()
        transport = MagicMock()
        transport.get_protocol.return_value = MagicMock()
        writer.transport = transport
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer._transport = transport

        proxy = MagicMock()
        proxy.url = "http://proxy:8080"
        project = MagicMock()
        project.tls_mitm_mode = MitmMode.PLAIN
        project.tls_mitm_engine = None
        project.tls_mitm_browser = None

        mock_relay = AsyncMock()
        mock_relay.send_request.side_effect = ConnectionError("upstream failed")
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
        ):
            mock_loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            await handler.handle(reader, writer, "example.com", 443, proxy, project)

        # Check that 502 was written
        written_data = b"".join(call.args[0] for call in writer.write.call_args_list)
        assert b"502 Bad Gateway" in written_data
