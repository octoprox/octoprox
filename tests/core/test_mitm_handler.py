# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MitmHandler."""

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.mitm.handler import MitmHandler, start_tls_with_head
from api.core.tls_cert_manager import TLSCertManager
from api.models.project import MitmEngine, MitmMode
from api.tunnel.sniff import take_buffered


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
        mock_relay.send_request.return_value = (200, "OK", [("Content-Type", "text/plain")], b"response body")
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
        # headers is a list of tuples; host and user-agent should be present
        header_keys = [k.lower() for k, _v in call_args[0][2]]
        assert "host" in header_keys
        assert "user-agent" in header_keys
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
        assert b"HTTP/1.1 502" in written_data


class TestStartTlsWithHead:
    """The TLS upgrade must see a ClientHello the caller already took off the socket."""

    async def test_peeked_client_hello_reaches_the_handshake(self, tmp_path: Path) -> None:
        manager = TLSCertManager(ca_cert_path=tmp_path / "ca.crt", ca_key_path=tmp_path / "ca.key")
        manager._generate_ca()
        server_ctx = manager.get_server_ssl_context("localhost")
        served: list[bytes] = []

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # As the transparent listener does: the ClientHello is peeked into
            # the reader (for its SNI) before the decision to intercept.
            while len(reader._buffer) < 5:  # type: ignore[attr-defined]
                await reader._wait_for_data("peek")  # type: ignore[attr-defined]
            head = take_buffered(reader)
            transport = writer.transport
            new_transport = await start_tls_with_head(
                asyncio.get_running_loop(), head, transport, transport.get_protocol(), server_ctx
            )
            writer._transport = new_transport  # type: ignore[attr-defined]
            served.append(await reader.readline())
            writer.write(b"pong\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client_ctx = ssl.create_default_context()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port, ssl=client_ctx), 5
            )
            writer.write(b"ping\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 5) == b"pong\n"
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        assert served == [b"ping\n"]
