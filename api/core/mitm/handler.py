# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM handler for TLS interception on gateways: the client's TLS ends here, and its HTTP/1.1 requests go upstream through a relay.

Every exchange is captured to the project's Redis stream by a held background write, and a failed request is published as
``request_failed`` for the failure log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time
from typing import TYPE_CHECKING

import h11
import structlog

from api.core import utc_now
from api.core.error_response import (
    BANDWIDTH_LIMIT_PROXY_STATUS,
    reason_phrase,
    refused_proxy_status,
)
from api.core.failure_log import build_failure
from api.core.logging import error_text
from api.core.mitm.base import UpstreamRefusedError
from api.core.signals import request_failed
from api.core.stats import FailureKind
from api.core.tasks import HeldTasks
from api.core.tls_cert_manager import TLSCertManager, pop_client_hello

if TYPE_CHECKING:
    from api.core.mitm.base import MitmRelay
    from api.core.traffic_limiter import TrafficMeter
    from api.db.redis import RedisClient
    from api.models.project import Project
    from api.models.proxy import Proxy

logger = structlog.get_logger()


async def start_tls_with_head(
    loop: asyncio.AbstractEventLoop,
    head: bytes,
    transport: asyncio.WriteTransport,
    protocol: asyncio.BaseProtocol,
    ssl_context: ssl.SSLContext,
) -> asyncio.Transport | None:
    """Server-side ``loop.start_tls`` that first feeds the TLS layer ``head``, the bytes the caller already read off the socket.

    The caller pauses reading on ``transport`` before the client can send what the handshake needs.
    """
    handshake = asyncio.ensure_future(
        loop.start_tls(transport, protocol, ssl_context, server_side=True)
    )
    if head:
        # One turn: start_tls has now installed the SSL protocol and queued
        # its connection_made and resume_reading. The replay goes behind them.
        await asyncio.sleep(0)

        def replay() -> None:
            if handshake.done():
                return
            ssl_protocol = transport.get_protocol()
            view = memoryview(head)
            while view:
                buffer = ssl_protocol.get_buffer(len(view))  # type: ignore[attr-defined]
                n = min(len(buffer), len(view))
                buffer[:n] = view[:n]
                ssl_protocol.buffer_updated(n)  # type: ignore[attr-defined]
                view = view[n:]

        loop.call_soon(replay)
    return await handshake

# Read buffer size for client-facing connection
_READ_SIZE = 65536

# Hop-by-hop headers that must not be forwarded through a proxy
_HOP_BY_HOP = frozenset((
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
))


class MitmHandler:
    """Intercepts the TLS of the gateway's CONNECT tunnels and tunnel connections; one per gateway.

    Upgrades the client connection to TLS (presenting a generated cert),
    reads HTTP/1.1 requests, logs headers, and relays them to the target
    via a pluggable relay strategy (plain, curl_cffi, or rnet).
    """

    def __init__(
        self,
        cert_manager: TLSCertManager,
        redis_client: RedisClient | None = None,
    ) -> None:
        self._cert_manager = cert_manager
        self._redis_client = redis_client
        # Capture writes on their way to Redis, held so none is collected
        # before it runs and the gateway can wait for them when it stops.
        self._captures = HeldTasks()
        # Captures Redis did not take, since start. They are debugging aids,
        # so they are dropped rather than buffered.
        self.dropped_captures = 0

    async def settle(self) -> None:
        """Wait for the capture writes under way; the gateway calls it once its listeners are closed."""
        await self._captures.settle()

    def _record_request(
        self,
        *,
        project: Project,
        method: str,
        url: str,
        request_headers: list[tuple[str, str]],
        upstream_headers: list[tuple[str, str]],
        request_body_size: int,
        status_code: int,
        response_headers: list[tuple[str, str]],
        response_body_size: int,
        target_host: str,
        proxy_url: str,
        latency_ms: float,
        tls_info: dict[str, str] | None = None,
    ) -> None:
        """Capture one intercepted exchange in a held background write; never blocks."""
        if self._redis_client is None:
            return

        req_ct = next(
            (v for k, v in request_headers if k.lower() == "content-type"), ""
        )
        resp_ct = next(
            (v for k, v in response_headers if k.lower() == "content-type"), ""
        )

        fields = {
            "timestamp": utc_now().isoformat(),
            "method": method,
            "url": url,
            "request_headers": json.dumps(request_headers),
            "upstream_headers": json.dumps(upstream_headers),
            "request_body_size": str(request_body_size),
            "request_content_type": req_ct,
            "status_code": str(status_code),
            "response_headers": json.dumps(response_headers),
            "response_body_size": str(response_body_size),
            "response_content_type": resp_ct,
            "target_host": target_host,
            "proxy_url": proxy_url,
            "mitm_mode": str(project.tls_mitm_mode),
            "mitm_engine": str(project.tls_mitm_engine or ""),
            "mitm_browser": str(project.tls_mitm_browser or ""),
            "latency_ms": str(round(latency_ms, 2)),
        }

        if tls_info:
            fields.update(tls_info)

        self._captures.spawn(
            self._write_capture(self._redis_client, project.id, fields), name="mitm_capture", project_id=project.id
        )

    async def _write_capture(self, redis_client: RedisClient, project_id: str, fields: dict[str, str]) -> None:
        """Append one capture to the project's stream; a capture Redis refuses is counted and dropped."""
        try:
            await redis_client.record_mitm_request(project_id, fields)
        except Exception as exc:
            self.dropped_captures += 1
            logger.debug("MITM capture dropped", project_id=project_id, error=error_text(exc))

    async def handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target_host: str,
        target_port: int,
        proxy: Proxy,
        project: Project,
        upstream_reader: asyncio.StreamReader | None = None,
        upstream_writer: asyncio.StreamWriter | None = None,
        meter: TrafficMeter | None = None,
        client_head: bytes = b"",
    ) -> tuple[int, int]:
        """Run MITM interception on the client connection.

        Upgrades the client connection to TLS, then uses h11 to parse
        HTTP/1.1 requests and serialize responses, relaying them via
        the appropriate strategy based on project settings.

        ``client_head`` is the start of the client's TLS stream when the
        caller has already read it off the socket (a transparent connection
        whose ClientHello was sniffed for its name); it is fed to the
        handshake, which would otherwise never see it. A CONNECT client
        sends nothing before the 200, so that path passes nothing.

        With a ``meter``, every request and response is counted as it is
        relayed, and once the connector's traffic limit cuts the connection
        the next request is answered with the limit status instead of being
        relayed, then the connection closes.

        Returns:
            Tuple of (bytes_sent, bytes_received).
        """
        # Local: the package imports this module for MitmHandler. Looked up at
        # call time from the package, where tests replace it.
        from api.core.mitm import create_relay

        bytes_sent = 0
        bytes_received = 0

        # Upgrade client connection to TLS (Octoprox as server)
        server_ssl_ctx = self._cert_manager.get_server_ssl_context(target_host)
        loop = asyncio.get_running_loop()
        transport = client_writer.transport
        protocol = transport.get_protocol()

        try:
            new_transport = await start_tls_with_head(loop, client_head, transport, protocol, server_ssl_ctx)
        except Exception as e:
            logger.debug("MITM TLS handshake failed", target_host=target_host, error=str(e))
            return bytes_sent, bytes_received

        # Extract client-side TLS handshake info (per-connection)
        tls_info: dict[str, str] = {}
        ssl_object = new_transport.get_extra_info("ssl_object") if new_transport else None
        if ssl_object is not None:
            _cipher_info = ssl_object.cipher()  # (name, protocol, key_bits)
            tls_info = {
                "tls_version": ssl_object.version() or "",
                "tls_cipher": _cipher_info[0] if _cipher_info else "",
                "tls_key_bits": str(_cipher_info[2]) if _cipher_info else "",
                "tls_shared_ciphers": ",".join(
                    name for name, _proto, _bits in (ssl_object.shared_ciphers() or [])
                ),
            }

            # Parse raw ClientHello captured during the handshake
            hello_result = pop_client_hello(ssl_object)
            if hello_result is not None:
                raw_hello, record_layer_version = hello_result
                try:
                    # Local: the fingerprint code (and dpkt) loads only once something is intercepted.
                    from api.core.mitm.client_hello import (
                        client_hello_to_dict,
                        compute_ja3,
                        compute_ja4,
                        compute_ja4_r,
                        parse_client_hello,
                    )

                    ch_info = parse_client_hello(raw_hello, record_layer_version=record_layer_version)
                    ja3_hash, ja3_full = compute_ja3(ch_info)
                    ja4 = compute_ja4(ch_info)
                    ja4_r = compute_ja4_r(ch_info)
                    tls_info["tls_client_hello"] = json.dumps(
                        client_hello_to_dict(ch_info, ja3_hash, ja3_full, ja4, ja4_r)
                    )
                except Exception:
                    logger.debug("Failed to parse ClientHello", exc_info=True)

        # The writer and its protocol go on over the TLS transport, as
        # StreamWriter.start_tls leaves them; the protocol then also knows it
        # is over TLS, so the client's close is not warned about.
        client_writer._transport = new_transport  # type: ignore[attr-defined]
        protocol._replace_writer(client_writer)  # type: ignore[attr-defined]

        # Build base URL for the target
        if target_port == 443:
            base_url = f"https://{target_host}"
        else:
            base_url = f"https://{target_host}:{target_port}"

        proxy_url = proxy.url

        # Create relay strategy based on project settings
        relay: MitmRelay = create_relay(
            project,
            upstream_reader=upstream_reader,
            upstream_writer=upstream_writer,
            target_host=target_host,
        )

        conn = h11.Connection(our_role=h11.SERVER)

        try:
            while True:
                # Read a complete request using h11 (Request + optional Data + EndOfMessage)
                request_event, body, read_bytes = await _read_request(conn, client_reader)
                bytes_sent += read_bytes
                if meter is not None:
                    meter.add_sent(read_bytes)

                if request_event is None:
                    break

                if meter is not None and not meter.allowed:
                    status = meter.limit_status
                    limit_message = "The connector serving this connection has reached its traffic limit."
                    await request_failed.send_async(self, failure=build_failure(
                        project.id, FailureKind.TRAFFIC_LIMIT_EXCEEDED, status, limit_message, proxy=proxy,
                        method=request_event.method.decode("ascii"),
                        target=f"{base_url}{request_event.target.decode('ascii')}".split("?", 1)[0],
                    ))
                    written = _send_via_h11(
                        conn, client_writer, status,
                        [("Proxy-Status", BANDWIDTH_LIMIT_PROXY_STATUS),
                         ("Content-Type", "text/plain; charset=utf-8")],
                        limit_message.encode(),
                        head=request_event.method == b"HEAD",
                    )
                    with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                        await client_writer.drain()
                    bytes_received += written
                    meter.add_received(written)
                    break

                # Extract request details from h11 event
                method = request_event.method.decode("ascii")
                path = request_event.target.decode("ascii")
                headers: list[tuple[str, str]] = [
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in request_event.headers
                ]

                logger.debug(
                    "MITM intercepted request",
                    target_host=target_host,
                    method=method,
                    path=path,
                    headers=headers,
                    mode=project.tls_mitm_mode,
                )

                url = f"{base_url}{path}"

                # Remove hop-by-hop headers that shouldn't be forwarded
                forward_headers = [
                    (k, v)
                    for k, v in headers
                    if k.lower() not in _HOP_BY_HOP
                ]

                # Relay request via the chosen strategy
                req_start = time.monotonic()
                try:
                    (
                        status_code,
                        _reason,
                        response_headers,
                        response_body,
                    ) = await relay.send_request(method, url, forward_headers, body, proxy_url)
                except Exception as e:
                    logger.debug("MITM upstream request failed", url=url, error=str(e))
                    refused = e if isinstance(e, UpstreamRefusedError) else None
                    # The query string is left out of the log: it is where tokens travel
                    # (build_failure also cuts it from the error text, which may quote the URL).
                    failure = build_failure(
                        project.id, FailureKind.INTERCEPTED_REQUEST_FAILED, 502, error_text(e),
                        proxy=proxy, exc=e, latency_ms=(time.monotonic() - req_start) * 1000,
                        method=method, target=url.split("?", 1)[0],
                        upstream_status=refused.status if refused else None,
                    )
                    await request_failed.send_async(self, failure=failure)
                    # The client is answered with the logged text, which build_failure
                    # has scrubbed of the upstream's credentials.
                    error_headers = [("Content-Type", "text/plain; charset=utf-8")]
                    if refused is not None:
                        error_headers.append(("Proxy-Status", refused_proxy_status(refused.status)))
                    written = _send_via_h11(
                        conn, client_writer, 502, error_headers, failure.message.encode(),
                        reason=reason_phrase("Bad Gateway", failure.message), head=method == "HEAD",
                    )
                    with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                        await client_writer.drain()
                    bytes_received += written
                    if meter is not None:
                        meter.add_received(written)
                    break

                # Filter response headers: drop transfer-encoding/content-length
                # (h11 manages content-length automatically)
                filtered: list[tuple[str, str]] = [
                    (k, v)
                    for k, v in response_headers
                    if k.lower() not in ("transfer-encoding", "content-length")
                ]

                req_latency_ms = (time.monotonic() - req_start) * 1000

                try:
                    written = _send_via_h11(
                        conn,
                        client_writer,
                        status_code,
                        filtered,
                        response_body,
                    )
                    await client_writer.drain()
                    bytes_received += written
                    if meter is not None:
                        meter.add_received(written)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break

                self._record_request(
                    project=project,
                    method=method,
                    url=url,
                    request_headers=headers,
                    upstream_headers=relay.last_upstream_headers,
                    request_body_size=len(body) if body else 0,
                    status_code=status_code,
                    response_headers=response_headers,
                    response_body_size=len(response_body),
                    target_host=target_host,
                    proxy_url=proxy_url,
                    latency_ms=req_latency_ms,
                    tls_info=tls_info,
                )

                # Prepare for next request (keep-alive)
                try:
                    conn.start_next_cycle()
                except h11.LocalProtocolError:
                    break

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            await relay.close()

        return bytes_sent, bytes_received


async def _read_request(
    conn: h11.Connection,
    reader: asyncio.StreamReader,
) -> tuple[h11.Request | None, bytes | None, int]:
    """Read a full HTTP/1.1 request via h11.

    Returns (request_event, body_bytes, bytes_read_from_socket).
    Returns (None, None, bytes_read) if the connection is closed or an
    unrecoverable event is encountered.
    """
    request_event: h11.Request | None = None
    body_parts: list[bytes] = []
    total_read = 0

    while True:
        event = conn.next_event()

        if event is h11.NEED_DATA:
            try:
                data = await reader.read(_READ_SIZE)
            except (ConnectionResetError, BrokenPipeError, OSError):
                return None, None, total_read
            if not data:
                return None, None, total_read
            total_read += len(data)
            conn.receive_data(data)
            continue

        if isinstance(event, h11.Request):
            request_event = event
            continue

        if isinstance(event, h11.Data):
            body_parts.append(bytes(event.data))
            continue

        if isinstance(event, h11.EndOfMessage):
            body = b"".join(body_parts) if body_parts else None
            return request_event, body, total_read

        # ConnectionClosed, PAUSED, or other sentinel - stop
        return None, None, total_read


def _send_via_h11(
    conn: h11.Connection,
    writer: asyncio.StreamWriter,
    status_code: int,
    headers: list[tuple[str, str]],
    body: bytes,
    reason: str = "",
    *,
    head: bool = False,
) -> int:
    """Serialize and write a full HTTP/1.1 response via h11.

    ``head`` says the request was a HEAD: the response announces the
    body's length but carries none, which h11 would refuse to send.

    Returns the number of bytes written.
    """
    h11_headers: list[tuple[str, str]] = list(headers)
    h11_headers.append(("content-length", str(len(body))))

    response = h11.Response(status_code=status_code, headers=h11_headers, reason=reason.encode("ascii"))
    out = conn.send(response) or b""
    if body and not head:
        out += conn.send(h11.Data(data=body)) or b""
    out += conn.send(h11.EndOfMessage()) or b""

    writer.write(out)
    return len(out)
