# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MitmHandler MITM request recording."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from api.core.mitm.handler import MitmHandler
from api.models.project import MitmEngine, MitmMode


class TestMitmHandlerRecording:
    """Tests for fire-and-forget request recording in MitmHandler."""

    def _make_project(
        self,
        mode: MitmMode = MitmMode.MATCH_UA,
        engine: MitmEngine | None = MitmEngine.CURL_CFFI,
        browser: str | None = None,
    ) -> MagicMock:
        project = MagicMock()
        project.id = "test-project-id"
        project.tls_mitm_mode = mode
        project.tls_mitm_engine = engine
        project.tls_mitm_browser = browser
        return project

    async def test_record_request_called_on_success(self) -> None:
        """Verify _record_request is called after a successful relay cycle."""
        cert_manager = MagicMock()
        cert_manager.get_server_ssl_context.return_value = MagicMock()
        redis_client = MagicMock()
        redis_client.record_mitm_request = AsyncMock()

        handler = MitmHandler(cert_manager, redis_client=redis_client)

        request_data = b"GET /test HTTP/1.1\r\nHost: example.com\r\nUser-Agent: TestBot/1.0\r\n\r\n"
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
        project = self._make_project()

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (
            200,
            "OK",
            [("content-type", "text/html")],
            b"<html>ok</html>",
        )
        mock_relay.last_upstream_headers = [("User-Agent", "TestBot/1.0")]
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
        ):
            mock_loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            await handler.handle(reader, writer, "example.com", 443, proxy, project)

        # Allow the fire-and-forget task to complete
        await asyncio.sleep(0.05)

        redis_client.record_mitm_request.assert_called_once()
        call_args = redis_client.record_mitm_request.call_args
        assert call_args[0][0] == "test-project-id"
        fields = call_args[0][1]
        assert fields["method"] == "GET"
        assert fields["url"] == "https://example.com/test"
        assert fields["status_code"] == "200"
        assert fields["target_host"] == "example.com"
        assert fields["mitm_mode"] == "match_ua"
        assert fields["response_body_size"] == str(len(b"<html>ok</html>"))
        assert "timestamp" in fields
        assert float(fields["latency_ms"]) >= 0
        assert "User-Agent" in fields["upstream_headers"]

    async def test_record_request_not_called_without_redis(self) -> None:
        """Verify recording is skipped when no redis_client is provided."""
        cert_manager = MagicMock()
        cert_manager.get_server_ssl_context.return_value = MagicMock()

        handler = MitmHandler(cert_manager)  # No redis_client
        assert handler._redis_client is None

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
        project = self._make_project()

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (200, "OK", [], b"ok")
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
        ):
            mock_loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            bs, br = await handler.handle(reader, writer, "example.com", 443, proxy, project)

        # Should still complete successfully
        assert br > 0

    async def test_record_request_not_called_on_upstream_error(self) -> None:
        """Verify recording is NOT called when the upstream relay fails."""
        cert_manager = MagicMock()
        cert_manager.get_server_ssl_context.return_value = MagicMock()
        redis_client = MagicMock()
        redis_client.record_mitm_request = AsyncMock()

        handler = MitmHandler(cert_manager, redis_client=redis_client)

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
        project = self._make_project(mode=MitmMode.PLAIN, engine=None)

        mock_relay = AsyncMock()
        mock_relay.send_request.side_effect = ConnectionError("upstream failed")
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
        ):
            mock_loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            await handler.handle(reader, writer, "example.com", 443, proxy, project)

        await asyncio.sleep(0.05)

        # Recording should NOT be called on upstream failure
        redis_client.record_mitm_request.assert_not_called()

    async def test_redis_error_does_not_break_proxy(self) -> None:
        """Verify Redis errors in recording don't affect proxy traffic."""
        cert_manager = MagicMock()
        cert_manager.get_server_ssl_context.return_value = MagicMock()
        redis_client = MagicMock()
        redis_client.record_mitm_request = AsyncMock(
            side_effect=ConnectionError("Redis down"),
        )

        handler = MitmHandler(cert_manager, redis_client=redis_client)

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
        project = self._make_project()

        mock_relay = AsyncMock()
        mock_relay.send_request.return_value = (200, "OK", [], b"ok")
        mock_relay.last_upstream_headers = []
        mock_relay.close = AsyncMock()

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("api.core.mitm.create_relay", return_value=mock_relay),
        ):
            mock_loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            bs, br = await handler.handle(reader, writer, "example.com", 443, proxy, project)

        await asyncio.sleep(0.05)

        # Proxy should still have completed successfully despite Redis error
        assert br > 0
        redis_client.record_mitm_request.assert_called_once()
