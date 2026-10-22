# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for what the proxy server tells the failure log: the kind of failure, and what the client asked and was answered.

A request counted as failed is logged exactly once and one that went through not at all, and every transfer's meter is
finished, so nothing stays counted in flight.
"""

import asyncio
import base64
import contextlib
import errno
import socket
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.failure_log import RequestFailure
from api.core.mitm.base import UpstreamRefusedError
from api.core.mitm.handler import MitmHandler
from api.core.proxy_server import ProxyServer
from api.core.signals import request_completed, request_failed
from api.core.stats import FailureKind
from api.core.traffic_limiter import TrafficMeter
from api.gateway.metering import RequestMetering
from api.models.project import MitmMode, Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.inflight import InFlight
from api.routing.router import Router
from tests.gateway.doubles import QuietMetering

PROJECT = Project(name="Scraping", username="proj", password="secret")


@dataclass
class _Published:
    """What the listener published while a test ran: completions and failures, in order."""

    completed: list[dict[str, Any]] = field(default_factory=list)
    failures: list[RequestFailure] = field(default_factory=list)

    async def on_completed(self, _sender: object, **kwargs: Any) -> None:
        self.completed.append(kwargs)

    async def on_failed(self, _sender: object, failure: RequestFailure, **_: Any) -> None:
        self.failures.append(failure)


def _router(proxy: Proxy | None, *, quarantined: bool = False) -> MagicMock:
    """The gateway router's surface, answering every request with ``proxy``."""
    router = MagicMock(spec=Router)
    router.get_project_by_username.side_effect = lambda u: PROJECT if u == PROJECT.username else None
    router.get_project.return_value = PROJECT
    router.select_proxy_for_project.return_value = proxy
    router.are_all_proxies_quarantined.return_value = quarantined
    router.traffic_limit_status.return_value = None
    return router


def _auth(username: str = "proj-sessid-s1-cc-us") -> str:
    token = base64.b64encode(f"{username}:secret".encode()).decode()
    return f"Proxy-Authorization: Basic {token}\r\n"


def _upstream(port: int) -> Proxy:
    return Proxy(
        host="127.0.0.1", port=port, protocol=ProxyProtocol.HTTP,
        username="vendor-user", password="vendor-secret", connector_id="connector-1",
    )


@pytest.fixture
def publish() -> Iterator[_Published]:
    """The listener's request signals, received for the test's duration."""
    published = _Published()
    request_completed.connect(published.on_completed)
    request_failed.connect(published.on_failed)
    yield published
    request_completed.disconnect(published.on_completed)
    request_failed.disconnect(published.on_failed)


def _failures(publish: _Published) -> list[RequestFailure]:
    return publish.failures


def _completions(publish: _Published) -> list[dict[str, Any]]:
    return publish.completed


@pytest.fixture
def inflight() -> InFlight:
    return InFlight()


@pytest.fixture
async def listen(inflight: InFlight) -> AsyncIterator[Callable[..., Awaitable[ProxyServer]]]:
    servers: list[ProxyServer] = []

    async def start(proxy: Proxy | None, *, quarantined: bool = False, timeout: float = 30.0) -> ProxyServer:
        server = ProxyServer(
            _router(proxy, quarantined=quarantined), QuietMetering(inflight), host="127.0.0.1", port=0, timeout=timeout
        )
        await server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.stop()
    # Whatever happened to the request, its meter was finished.
    assert inflight.total == 0


async def _exchange(server: ProxyServer, request: str, *, then: bytes = b"") -> bytes:
    """Send a request, optionally more bytes and a half-close, and read until the server closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(request.encode() + then)
    await writer.drain()
    if then:
        writer.write_eof()
    response = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return response


def _head(response: bytes) -> tuple[str, dict[str, str]]:
    """The status line and the headers (by lowercase name) of a response."""
    head = response.split(b"\r\n\r\n", 1)[0].decode()
    status_line, *lines = head.split("\r\n")
    return status_line, {name.lower(): value.strip() for name, value in (line.split(":", 1) for line in lines)}


@contextlib.asynccontextmanager
async def _vendor(answer: bytes | None, *, reset_after: int = 0, keep_open: bool = False) -> AsyncIterator[int]:
    """An upstream HTTP proxy that answers each CONNECT with ``answer``, or never answers when None.

    With ``reset_after`` it then takes that many tunnel bytes and resets the
    connection, as a vendor dropping a session does; with ``keep_open`` it
    holds the connection open after answering.
    """
    held: list[asyncio.StreamWriter] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        if answer is None:
            held.append(writer)
            return
        writer.write(answer)
        await writer.drain()
        if keep_open:
            held.append(writer)
            return
        if reset_after:
            await reader.readexactly(reset_after)
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.transport.abort()
            return
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for w in held:
            w.close()
        server.close()
        await server.wait_closed()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


class TestRefused:
    async def test_no_proxy_is_logged_with_what_was_asked(self, listen: Any, publish: _Published) -> None:
        server = await listen(None)
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        status_line, headers = _head(response)
        # The message is in the reason phrase too: CONNECT clients show the status line only.
        assert status_line.startswith("HTTP/1.1 502 Bad Gateway (No upstream proxy available for ")
        assert "proxy-status" not in headers
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.NO_PROXY_AVAILABLE
        assert failure.status == 502
        assert failure.project_id == PROJECT.id
        assert (failure.method, failure.target) == ("CONNECT", "example.com:443")
        assert (failure.session_id, failure.location) == ("s1", "US")
        assert "US" in failure.message
        assert failure.proxy is None

    async def test_quarantine(self, listen: Any, publish: _Published) -> None:
        server = await listen(None, quarantined=True)
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 429")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.ALL_PROXIES_QUARANTINED, 429)

    async def test_a_state_without_a_country(self, listen: Any, publish: _Published) -> None:
        server = await listen(None)
        response = await _exchange(server, f"GET http://example.com/a?b=c HTTP/1.1\r\n{_auth('proj-st-ny')}\r\n")

        assert response.startswith(b"HTTP/1.1 400")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.INVALID_LOCATION, 400)
        assert (failure.method, failure.target) == ("GET", "example.com:80")

    async def test_wrong_credentials_are_not_a_project_failure(self, listen: Any, publish: _Published) -> None:
        server = await listen(None)
        token = base64.b64encode(b"proj:wrong").decode()
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n")

        assert response.startswith(b"HTTP/1.1 407")
        assert _failures(publish) == []


class TestUpstreamFailures:
    async def test_vendor_refusal_carries_the_vendors_status(self, listen: Any, publish: _Published) -> None:
        async with _vendor(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        status_line, headers = _head(response)
        assert status_line == (
            "HTTP/1.1 502 Bad Gateway (Upstream proxy refused the tunnel: 407 Proxy Authentication Required)"
        )
        assert headers["proxy-status"] == "octoprox; error=connection_refused; received-status=407"
        # The vendor's 407 is about the connector's credentials, not the client's.
        assert "proxy-authenticate" not in headers
        assert response.endswith(b"\r\n\r\nUpstream proxy refused the tunnel: 407 Proxy Authentication Required")
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.UPSTREAM_REJECTED
        assert (failure.status, failure.upstream_status) == (502, 407)
        assert "407" in failure.message
        assert failure.error_type == "TunnelRefusedError"
        assert failure.upstream_message is None  # nothing said beyond the status line
        assert failure.proxy == f"127.0.0.1:{port}"
        assert failure.connector_id == "connector-1"
        assert failure.latency_ms is not None
        assert [c["success"] for c in _completions(publish)] == [False]

    async def test_a_vendor_rate_limit_stays_a_502(self, listen: Any, publish: _Published) -> None:
        """One connector throttled is not Octoprox asking the client to slow down: another may serve it."""
        async with _vendor(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 7\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 502 Bad Gateway (Upstream proxy refused the tunnel: 429 Too Many Requests)"
        assert headers["proxy-status"] == "octoprox; error=connection_refused; received-status=429"
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status, failure.upstream_status) == (FailureKind.UPSTREAM_REJECTED, 502, 429)

    async def test_what_the_vendor_says_reaches_the_log_and_the_client(self, listen: Any, publish: _Published) -> None:
        body = b"<html><head><title>403 Forbidden</title></head><body><h1>Zone &quot;resi&quot; is closed</h1></body></html>"
        answer = (
            b"HTTP/1.1 403 Forbidden\r\nx-brd-error: zone closed\r\nServer: vendor\r\nContent-Type: text/html\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        async with _vendor(answer) as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        said = 'x-brd-error: zone closed; 403 Forbidden Zone "resi" is closed'
        (failure,) = _failures(publish)
        assert (failure.kind, failure.upstream_status, failure.upstream_message) == (
            FailureKind.UPSTREAM_REJECTED, 403, said,
        )
        assert failure.message == "403 Forbidden"
        status_line, headers = _head(response)
        assert status_line == f"HTTP/1.1 502 Bad Gateway (Upstream proxy refused the tunnel: 403 Forbidden. The vendor said: {said})"
        assert headers["proxy-status"] == (
            'octoprox; error=connection_refused; received-status=403; details="'
            + said.replace('"', '\\"') + '"'
        )
        assert response.endswith(f"\r\n\r\nUpstream proxy refused the tunnel: 403 Forbidden. The vendor said: {said}".encode())

    async def test_a_refusal_whose_body_never_ends_is_answered_without_waiting_for_it(
        self, listen: Any, publish: _Published
    ) -> None:
        answer = b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: text/plain\r\n\r\nSlow down, zone limit 10/s"
        async with _vendor(answer, keep_open=True) as port:
            server = await listen(_upstream(port))
            started = asyncio.get_running_loop().time()
            await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")
            waited = asyncio.get_running_loop().time() - started

        (failure,) = _failures(publish)
        assert failure.upstream_message == "Slow down, zone limit 10/s"
        assert waited < 3

    async def test_bytes_the_vendor_sends_right_after_its_200_reach_the_client(
        self, listen: Any, publish: _Published
    ) -> None:
        """The target's first bytes can arrive with the vendor's answer: they belong to the tunnel."""
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\nSSH-2.0-banner\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:22 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.endswith(b"\r\n\r\nSSH-2.0-banner\r\n")
        assert _failures(publish) == []

    async def test_unreachable_upstream(self, listen: Any, publish: _Published) -> None:
        server = await listen(_upstream(_closed_port()))
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        status_line, headers = _head(response)
        assert status_line.startswith("HTTP/1.1 502 Bad Gateway (")
        assert headers["proxy-status"] == "octoprox; error=destination_unavailable"
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.UPSTREAM_UNREACHABLE, 502)

    async def test_silent_upstream_times_out(self, listen: Any, publish: _Published) -> None:
        async with _vendor(None) as port:
            server = await listen(_upstream(port), timeout=0.3)
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 504 Gateway Timeout (Connection to upstream proxy timed out)"
        assert headers["proxy-status"] == "octoprox; error=connection_timeout"
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.UPSTREAM_TIMEOUT, 504)
        assert "0.3 seconds" in failure.message

    async def test_a_tunnel_that_went_through_is_not_logged(self, listen: Any, publish: _Published) -> None:
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n", then=b"hello")

        assert response.startswith(b"HTTP/1.1 200")
        assert _failures(publish) == []
        assert [c["success"] for c in _completions(publish)] == [True]

    async def test_a_client_that_leaves_mid_body_is_its_own_kind(self, listen: Any, publish: _Published) -> None:
        async with _vendor(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(
                server, f"POST http://example.com/upload HTTP/1.1\r\n{_auth()}Content-Length: 10\r\n\r\n", then=b"abc",
            )

        assert response.startswith(b"HTTP/1.1 502")
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.CLIENT_CLOSED
        assert (failure.method, failure.target) == ("POST", "example.com:80")
        assert [c["success"] for c in _completions(publish)] == [False]

    async def test_a_client_that_stalls_mid_body_is_its_own_kind(self, listen: Any, publish: _Published) -> None:
        async with _vendor(None) as port:
            server = await listen(_upstream(port), timeout=0.3)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(f"POST http://example.com/upload HTTP/1.1\r\n{_auth()}Content-Length: 10\r\n\r\nabc".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()

        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 504 Gateway Timeout (The client sent no more of the request body for 0.3 seconds)"
        # The client's own doing is not named as an upstream failure.
        assert "proxy-status" not in headers
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.CLIENT_CLOSED, 504)
        assert "request body" in failure.message

    async def test_a_response_cut_short_is_not_followed_by_an_error_answer(
        self, listen: Any, publish: _Published
    ) -> None:
        """Once the vendor's status line is relayed, a second status line would land inside its body."""
        async with _vendor(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"GET http://example.com/ HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"502" not in response
        assert response.endswith(b"\r\n\r\n5\r\n")
        assert _failures(publish) == []
        assert [c["success"] for c in _completions(publish)] == [True]

    async def test_a_vendor_refusal_is_not_logged_as_an_error(self, listen: Any, publish: _Published) -> None:
        """At one per failed request it flooded the log; the Failures page already counts it."""
        async with _vendor(b"HTTP/1.1 400 Bad Request\r\n\r\n") as port:
            server = await listen(_upstream(port))
            with patch("api.core.proxy_server.logger") as logger:
                await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert "CONNECT error" in [call.args[0] for call in logger.debug.call_args_list]
        logger.error.assert_not_called()
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.UPSTREAM_REJECTED

    async def test_a_fault_of_ours_is_still_an_error_with_its_traceback(self, listen: Any, publish: _Published) -> None:
        server = await listen(_upstream(_closed_port()))
        with (
            patch.object(server, "_connect_via_proxy", AsyncMock(side_effect=RuntimeError("a bug"))),
            patch("api.core.proxy_server.logger") as logger,
        ):
            await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        logger.error.assert_called_once()
        assert logger.error.call_args.args == ("CONNECT error",)
        assert logger.error.call_args.kwargs["exc_info"] is True
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.UPSTREAM_ERROR

    @pytest.mark.parametrize(
        ("error", "level"),
        [
            # The client hung up before it was answered.
            (OSError(errno.ENOTCONN, "Transport endpoint is not connected"), "debug"),
            # Out of file descriptors is a fault here.
            (OSError(errno.EMFILE, "Too many open files"), "error"),
        ],
    )
    async def test_a_client_that_is_gone_is_not_an_error(
        self, listen: Any, publish: _Published, error: OSError, level: str
    ) -> None:
        server = await listen(_upstream(_closed_port()))
        with (
            patch.object(server, "_handle_connect", AsyncMock(side_effect=error)),
            patch("api.core.proxy_server.logger") as logger,
        ):
            await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        if level == "debug":
            assert "Client connection gone" in [call.args[0] for call in logger.debug.call_args_list]
            logger.error.assert_not_called()
        else:
            logger.error.assert_called_once()
            assert logger.error.call_args.args == ("Error handling client",)
            assert logger.error.call_args.kwargs["exc_info"] is True

    async def test_a_tunnel_the_upstream_reset_is_still_counted(self, listen: Any, publish: _Published) -> None:
        """Closing a reset upstream leg raises what reset it; the request is counted all the same."""
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n", reset_after=5) as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n", then=b"hello")

        assert response.startswith(b"HTTP/1.1 200")
        assert [c["success"] for c in _completions(publish)] == [True]
        assert _failures(publish) == []

    async def test_a_client_gone_mid_error_has_its_bytes_kept_and_its_meter_finished(
        self, publish: _Published, inflight: InFlight
    ) -> None:
        """No completion is published for it, but nothing it carried is lost and it is not left in flight."""
        metering = QuietMetering(inflight)
        server = ProxyServer(_router(None), metering, host="127.0.0.1", port=0, timeout=30.0)
        upstream_writer = MagicMock(spec=asyncio.StreamWriter)
        upstream_writer.wait_closed = AsyncMock()
        client_writer = MagicMock(spec=asyncio.StreamWriter)
        client_writer.transport.is_closing.return_value = True

        async def tunnel(*_streams: object, meter: TrafficMeter) -> tuple[int, int]:
            meter.add_received(5)
            raise ConnectionResetError("the client hung up")

        with (
            patch.object(
                server,
                "_connect_via_proxy",
                AsyncMock(return_value=(MagicMock(spec=asyncio.StreamReader), upstream_writer)),
            ),
            patch.object(server, "_tunnel", tunnel),
        ):
            await server._relay(
                MagicMock(spec=asyncio.StreamReader), client_writer, _upstream(8080), PROJECT, "example.com", 443,
                established=None, use_mitm=False,
            )

        assert _completions(publish) == [] and _failures(publish) == []
        assert (metering.totals.bytes_received, metering.totals.request_count) == (5, 0)
        assert inflight.total == 0

    async def test_a_device_leg_the_kernel_gave_up_ends_its_relay_counted(
        self, publish: _Published, inflight: InFlight
    ) -> None:
        """The vendor leg is not waited on once the device's leg failed: the transfer is counted and leaves flight."""
        metering = QuietMetering(inflight)
        server = ProxyServer(_router(None), metering, host="127.0.0.1", port=0, timeout=30.0)
        device = asyncio.StreamReader()
        device.set_exception(TimeoutError(errno.ETIMEDOUT, "Connection timed out"))
        client_writer = MagicMock(spec=asyncio.StreamWriter)
        client_writer.is_closing.return_value = False
        client_writer.transport.is_closing.return_value = False
        silent_vendor = asyncio.StreamReader()
        upstream_writer = MagicMock(spec=asyncio.StreamWriter)
        upstream_writer.is_closing.return_value = False
        upstream_writer.wait_closed = AsyncMock()

        with (
            patch.object(server, "_connect_via_proxy", AsyncMock(return_value=(silent_vendor, upstream_writer))),
            patch("api.core.proxy_server.logger") as logger,
        ):
            await asyncio.wait_for(
                server._relay(
                    device, client_writer, _upstream(8080), PROJECT, "example.com", 443,
                    established=None, use_mitm=False,
                ),
                5,
            )

        assert [c["success"] for c in _completions(publish)] == [True]
        assert _failures(publish) == []
        assert inflight.total == 0
        logger.error.assert_not_called()
        upstream_writer.close.assert_called_once()

    def test_an_unparseable_target_keeps_neither_query_nor_userinfo(self) -> None:
        server = ProxyServer(_router(None), MagicMock(spec=RequestMetering), host="127.0.0.1", port=0, timeout=30.0)
        assert server._target_of("GET", "http://[2001:db8::1]/a?token=x") == "[2001:db8::1]"
        assert server._target_of("GET", "http://user:pw@example.com/a?token=x") == "example.com"
        assert server._target_of("GET", "http://user:pw@example.com:8080/a") == "example.com:8080"


async def _intercept(request: bytes, error: Exception) -> bytes:
    """Run ``request`` through an intercepted connection whose relay fails with ``error``; the client's bytes."""
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()
    writer = MagicMock()
    writer.transport.get_protocol.return_value = MagicMock()
    writer.drain = AsyncMock()
    project = MagicMock(id="project-1", tls_mitm_mode=MitmMode.PLAIN, tls_mitm_engine=None, tls_mitm_browser=None)
    relay = AsyncMock()
    relay.send_request.side_effect = error

    with (
        patch("asyncio.get_running_loop") as loop,
        patch("api.core.mitm.create_relay", return_value=relay),
    ):
        loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
        await MitmHandler(MagicMock()).handle(reader, writer, "example.com", 443, _upstream(8080), project)
    return b"".join(c.args[0] for c in writer.write.call_args_list)


class TestInterceptedRequests:
    async def test_a_failed_intercepted_request_is_logged_without_its_query(self, publish: _Published) -> None:
        await _intercept(
            b"GET /search?token=abc HTTP/1.1\r\nHost: example.com\r\n\r\n", ConnectionError("upstream failed")
        )

        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.INTERCEPTED_REQUEST_FAILED
        assert (failure.method, failure.target) == ("GET", "https://example.com/search")
        assert failure.message == "upstream failed"
        assert failure.proxy == "127.0.0.1:8080"

    async def test_an_error_text_quoting_the_url_loses_its_query(self, publish: _Published) -> None:
        await _intercept(
            b"GET /search?token=abc HTTP/1.1\r\nHost: example.com\r\n\r\n",
            ConnectionError('error sending request for url (https://example.com/search?token=abc)'),
        )

        (failure,) = _failures(publish)
        assert "token" not in failure.message
        assert "https://example.com/search" in failure.message

    async def test_a_vendor_refusal_inside_the_connection_is_answered_with_the_vendors_status(
        self, publish: _Published
    ) -> None:
        response = await _intercept(
            b"GET /search?token=abc HTTP/1.1\r\nHost: example.com\r\n\r\n", UpstreamRefusedError(429)
        )

        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.INTERCEPTED_REQUEST_FAILED, 502)
        assert failure.upstream_status == 429
        assert failure.message == "Upstream proxy refused the tunnel: 429 Too Many Requests"
        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 502 Bad Gateway (Upstream proxy refused the tunnel: 429 Too Many Requests)"
        assert headers["proxy-status"] == "octoprox; error=connection_refused; received-status=429"
        assert response.endswith(b"\r\n\r\nUpstream proxy refused the tunnel: 429 Too Many Requests")

    async def test_any_failure_inside_the_connection_says_what_went_wrong(self, publish: _Published) -> None:
        response = await _intercept(
            b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
            ConnectionError("reset by http://vendor-user:vendor-secret@127.0.0.1:8080"),
        )

        (failure,) = _failures(publish)
        assert failure.upstream_status is None
        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 502 Bad Gateway (reset by http://127.0.0.1:8080)"
        # Only a refusal is named: the other failures of an engine are not told apart.
        assert "proxy-status" not in headers
        assert b"vendor-secret" not in response

    async def test_a_failed_head_request_is_answered_without_a_body(self, publish: _Published) -> None:
        response = await _intercept(b"HEAD / HTTP/1.1\r\nHost: example.com\r\n\r\n", UpstreamRefusedError(429))

        status_line, headers = _head(response)
        assert status_line == "HTTP/1.1 502 Bad Gateway (Upstream proxy refused the tunnel: 429 Too Many Requests)"
        assert int(headers["content-length"]) > 0
        assert response.endswith(b"\r\n\r\n")
        (failure,) = _failures(publish)
        assert failure.upstream_status == 429
