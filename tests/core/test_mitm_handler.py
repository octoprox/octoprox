# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MitmHandler."""

import asyncio
import base64
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.mitm.handler import MitmHandler, start_tls_with_head
from api.core.proxy_server import ProxyServer
from api.core.tls_cert_manager import TLSCertManager
from api.models.project import MitmEngine, MitmMode, Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.router import Router
from api.tunnel.sniff import take_buffered
from tests.gateway.doubles import QuietMetering
from tests.waits import until


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


class _SlowCertificates(TLSCertManager):
    """The CA's host contexts, the first of them made slowly on the event loop, as key generation once was."""

    def __init__(self, ca_dir: Path) -> None:
        super().__init__(ca_dir / "ca.crt", ca_dir / "ca.key")
        self._generate_ca()
        self.made: list[str] = []

    def get_server_ssl_context(self, hostname: str) -> ssl.SSLContext:
        if hostname not in self.made:
            self.made.append(hostname)
            time.sleep(0.2)
        return super().get_server_ssl_context(hostname)


class _AnsweringRelay:
    """The relay to the target: answers every request with ``hello``."""

    last_upstream_headers: list[tuple[str, str]] = []

    async def send_request(
        self, method: str, url: str, headers: list[tuple[str, str]], body: bytes | None, proxy_url: str
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        return 200, "OK", [("Content-Type", "text/plain")], b"hello"

    async def close(self) -> None:
        pass


def _intercepted_get(port: int, ca_cert: Path, host: str) -> bytes:
    """A client in a thread of its own: CONNECT, TLS trusting the CA as soon as the 200 is in, one GET."""
    token = base64.b64encode(b"proj:secret").decode()
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        raw.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += raw.recv(1024)
        assert head.startswith(b"HTTP/1.1 200")
        with ssl.create_default_context(cafile=str(ca_cert)).wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            response = b""
            while chunk := tls.recv(4096):
                response += chunk
            return response


class TestInterceptedConnectOnUvloop:
    """A CONNECT the MITM handler takes over, end to end on uvloop, the event loop gateways run on."""

    def test_a_client_hello_sent_while_the_hosts_certificate_is_made_reaches_the_handshake(
        self, tmp_path: Path
    ) -> None:
        uvloop = pytest.importorskip("uvloop")
        certificates = _SlowCertificates(tmp_path)
        project = Project(name="P", username="proj", password="secret", tls_mitm_mode=MitmMode.PLAIN)
        outcome: list[bytes | BaseException] = []

        async def vendor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()

        def client(port: int) -> None:
            try:
                outcome.append(_intercepted_get(port, certificates.ca_cert_path, "slow.example.com"))
            except BaseException as exc:
                outcome.append(exc)

        async def main() -> None:
            upstream = await asyncio.start_server(vendor, "127.0.0.1", 0)
            router = MagicMock(spec=Router)
            router.get_project_by_username.return_value = project
            router.select_proxy_for_project.return_value = Proxy(
                host="127.0.0.1", port=upstream.sockets[0].getsockname()[1], protocol=ProxyProtocol.HTTP,
                connector_id="connector-1",
            )
            metering = QuietMetering()
            server = ProxyServer(
                router, metering, host="127.0.0.1", port=0, timeout=5.0, mitm_handler=MitmHandler(certificates)
            )
            await server.start()
            thread = threading.Thread(target=client, args=(server.port,))
            thread.start()
            await until(lambda: not thread.is_alive())
            await server.stop()
            upstream.close()

        with patch("api.core.mitm.create_relay", return_value=_AnsweringRelay()):
            uvloop.run(main())

        assert certificates.made == ["slow.example.com"]
        (response,) = outcome
        assert isinstance(response, bytes), response
        assert response.startswith(b"HTTP/1.1 200") and response.endswith(b"hello")


class TestInterceptedConnect:
    async def test_a_client_that_fails_the_handshake_leaves_no_upstream_open(self, tmp_path: Path) -> None:
        """A client that does not trust the CA, a common setup mistake: its vendor tunnel is closed at once."""
        manager = TLSCertManager(tmp_path / "ca.crt", tmp_path / "ca.key")
        manager._generate_ca()
        project = Project(name="P", username="proj", password="secret", tls_mitm_mode=MitmMode.PLAIN)
        upstream_closed = asyncio.Event()

        async def vendor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await reader.read()
            upstream_closed.set()
            writer.close()

        upstream = await asyncio.start_server(vendor, "127.0.0.1", 0)
        router = MagicMock(spec=Router)
        router.get_project_by_username.return_value = project
        router.select_proxy_for_project.return_value = Proxy(
            host="127.0.0.1", port=upstream.sockets[0].getsockname()[1], protocol=ProxyProtocol.HTTP,
            connector_id="connector-1",
        )
        metering = QuietMetering()
        server = ProxyServer(router, metering, host="127.0.0.1", port=0, timeout=5.0, mitm_handler=MitmHandler(manager))
        # Kept here too, so the leg is closed by the gateway and not by the garbage collector.
        legs: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        connect = server._connect_via_proxy

        async def keep_leg(*args: Any) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            legs.append(await connect(*args))
            return legs[-1]

        server._connect_via_proxy = keep_leg  # type: ignore[method-assign]
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            token = base64.b64encode(b"proj:secret").decode()
            writer.write(f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode())
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            # Whatever the client sends instead of a handshake the gateway accepts, the handshake fails.
            writer.write(b"\x16\x03\x01\x00\x05hello")
            await writer.drain()

            await asyncio.wait_for(upstream_closed.wait(), 2.0)
            writer.close()
        finally:
            await server.stop()
            upstream.close()
