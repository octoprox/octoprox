# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM handler for TLS interception.

Intercepts HTTPS traffic by terminating TLS on the client side,
reading plaintext HTTP requests, logging headers, and relaying
requests upstream via a pluggable relay strategy.

Uses h11 for HTTP/1.1 and h2 (hyper-h2) for HTTP/2 on the client-facing side.
Protocol is negotiated via ALPN.
"""

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING

import h11
import structlog

import h2.config
import h2.connection
import h2.events
import h2.exceptions

from api.core.tls_cert_manager import TLSCertManager, pop_client_hello

if TYPE_CHECKING:
    from api.core.mitm.base import MitmRelay
    from api.db.redis import RedisClient
    from api.models.project import Project
    from api.models.proxy import Proxy

logger = structlog.get_logger()

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
    """Handles MITM interception for a single CONNECT tunnel.

    Upgrades the client connection to TLS (presenting a generated cert),
    reads HTTP/1.1 or HTTP/2 requests, logs headers, and relays them to
    the target via a pluggable relay strategy (plain, curl_cffi, or rnet).
    """

    def __init__(
        self,
        cert_manager: TLSCertManager,
        redis_client: "RedisClient | None" = None,
    ) -> None:
        self._cert_manager = cert_manager
        self._redis_client = redis_client

    def _record_request(
        self,
        *,
        project: "Project",
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
        """Fire-and-forget: schedule MITM request recording as a background task."""
        if self._redis_client is None:
            return

        from api.core import utc_now

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
            "mitm_mode": project.tls_mitm_mode.value
            if hasattr(project.tls_mitm_mode, "value")
            else str(project.tls_mitm_mode),
            "mitm_engine": project.tls_mitm_engine.value
            if project.tls_mitm_engine and hasattr(project.tls_mitm_engine, "value")
            else str(project.tls_mitm_engine or ""),
            "mitm_browser": project.tls_mitm_browser.value
            if project.tls_mitm_browser and hasattr(project.tls_mitm_browser, "value")
            else str(project.tls_mitm_browser or ""),
            "latency_ms": str(round(latency_ms, 2)),
        }

        if tls_info:
            fields.update(tls_info)

        coro = self._redis_client.record_mitm_request(project.id, fields)
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target_host: str,
        target_port: int,
        proxy: "Proxy",
        project: "Project",
        upstream_reader: asyncio.StreamReader | None = None,
        upstream_writer: asyncio.StreamWriter | None = None,
    ) -> tuple[int, int]:
        """Run MITM interception on the client connection.

        Upgrades the client connection to TLS, then uses h11 to parse
        HTTP/1.1 requests and serialize responses, relaying them via
        the appropriate strategy based on project settings.

        Returns:
            Tuple of (bytes_sent, bytes_received).
        """
        from api.core.mitm import create_relay

        bytes_sent = 0
        bytes_received = 0

        # Upgrade client connection to TLS (Octoprox as server)
        server_ssl_ctx = self._cert_manager.get_server_ssl_context(target_host)
        loop = asyncio.get_running_loop()
        transport = client_writer.transport
        protocol = transport.get_protocol()

        try:
            new_transport = await loop.start_tls(
                transport,
                protocol,
                server_ssl_ctx,
                server_side=True,
            )
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

        # Update the writer's transport to the TLS-wrapped one
        client_writer._transport = new_transport  # type: ignore[attr-defined]

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

        # pick code path based on negotiated ALPN protocol
        negotiated = ssl_object.selected_alpn_protocol() if ssl_object else None

        if negotiated == "h2":
            bytes_sent, bytes_received = await self._handle_h2(
                client_reader,
                client_writer,
                relay,
                base_url,
                proxy_url,
                project,
                target_host,
                tls_info
            )
        else:
            bytes_sent, bytes_received = await self._handle_h1(
                client_reader,
                client_writer,
                relay,
                base_url,
                proxy_url,
                project,
                target_host,
                tls_info
            )

        return bytes_sent, bytes_received

    async def _handle_h1(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        relay: "MitmRelay",
        base_url: str,
        proxy_url: str,
        project: "Project",
        target_host: str,
        tls_info: dict[str, str]
    ) -> tuple[int, int]:
        """Handle HTTP/1.1 requests via h11."""
        bytes_sent = 0
        bytes_received = 0

        conn = h11.Connection(our_role=h11.SERVER)

        try:
            while True:
                # Read a complete request using h11 (Request + optional Data + EndOfMessage)
                request_event, body, read_bytes = await _read_request(conn, client_reader)
                bytes_sent += read_bytes

                if request_event is None:
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
                    written = _send_via_h11(conn, client_writer, 502, [], b"")
                    with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                        await client_writer.drain()
                    bytes_received += written
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

    async def _handle_h2(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        relay: "MitmRelay",
        base_url: str,
        proxy_url: str,
        project: "Project",
        target_host: str,
        tls_info: dict[str, str]
    ) -> tuple[int, int]:
        """Handle HTTP/2 requests via hyper-h2 with concurrent stream processing."""
        from api.core.mitm.client_hello import (
            Http2Fingerprint,
            H2_SETTINGS_NAMES,
            h2_fingerprint_to_dict,
        )

        bytes_sent = 0
        bytes_received = 0

        config = h2.config.H2Configuration(
            client_side=False,
            header_encoding="utf-8"
        )
        h2_conn = h2.connection.H2Connection(config=config)
        h2_conn.initiate_connection()

        write_lock = asyncio.Lock()

        async def _flush() -> int:
            """Send pending h2 data to client, returns bytes written."""
            out = h2_conn.data_to_send()
            if out:
                client_writer.write(out)
                await client_writer.drain()
            return len(out)

        bytes_received += await _flush()

        # fingerprint state
        h2_fp = Http2Fingerprint()
        is_fingerprint_done = False

        # track streams: stream_id -> {"headers": [...], "data": [bytes, ...]}
        streams: dict[int, dict] = {}
        stream_tasks: set[asyncio.Task] = set()

        async def _handle_stream(
            stream_id: int,
            method: str,
            url: str,
            headers: list[tuple[str, str]],
            body: bytes | None
        ) -> None:
            """Process a single HTTP/2 stream (runs as async task)."""
            nonlocal bytes_sent, bytes_received

            forward_headers = [
                (k, v) for k, v in headers
                if k.lower() not in _HOP_BY_HOP
            ]

            req_start = time.monotonic()
            try:
                (
                    status_code,
                    _reason,
                    response_headers,
                    response_body
                ) = await relay.send_request(
                    method,
                    url,
                    forward_headers,
                    body,
                    proxy_url
                )
            except Exception as e:
                logger.debug(
                    "MITM h2 upstream request failed",
                    url=url,
                    error=str(e)
                )
                async with write_lock:
                    h2_conn.send_headers(
                        stream_id,
                        [(":status", "502")],
                        end_stream=True
                    )
                    bytes_received += await _flush()
                return

            req_latency_ms = (time.monotonic() - req_start) * 1000

            # build h2 response headers
            resp_headers = [(":status", str(status_code))]
            for k, v in response_headers:
                if k.lower() not in ("transfer-encoding", "content-length", ":status"):
                    resp_headers.append((k.lower(), v))

            try:
                if response_body:
                    async with write_lock:
                        h2_conn.send_headers(stream_id, resp_headers)
                        bytes_received += await _flush()

                    # send body in flow-control-aware chunks
                    offset = 0
                    while offset < len(response_body):
                        # wait for flow control window to open
                        for _ in range(100):
                            async with write_lock:
                                window = h2_conn.local_flow_control_window(stream_id)
                            if window > 0:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            # flow control window never opened, give up
                            break

                        async with write_lock:
                            window = h2_conn.local_flow_control_window(stream_id)
                            max_size = min(
                                h2_conn.local_settings.max_frame_size,
                                h2_conn.max_outbound_frame_size,
                                window,
                            )
                            if max_size <= 0:
                                continue

                            chunk = response_body[offset:offset + max_size]
                            is_end_stream = (offset + len(chunk) >= len(response_body))
                            h2_conn.send_data(
                                stream_id,
                                chunk,
                                end_stream=is_end_stream,
                            )
                            offset += len(chunk)
                            bytes_received += await _flush()
                else:
                    async with write_lock:
                        h2_conn.send_headers(
                            stream_id,
                            resp_headers,
                            end_stream=True,
                        )
                        bytes_received += await _flush()
            except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError):
                # client reset the stream mid-response
                return

            bytes_sent += len(body) if body else 0

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
                tls_info=tls_info
            )

        try:
            while True:
                data = await client_reader.read(_READ_SIZE)
                if not data:
                    break

                try:
                    events = h2_conn.receive_data(data)
                except h2.exceptions.ProtocolError:
                    break

                for event in events:
                    if isinstance(event, h2.events.RemoteSettingsChanged):
                        if not is_fingerprint_done:
                            h2_fp.settings = [
                                (s.setting, s.new_value)
                                for s in event.changed_settings.values()
                            ]
                            h2_fp.frames.append(
                                {
                                    "type": "SETTINGS",
                                    "stream_id": 0,
                                    "settings": {
                                        H2_SETTINGS_NAMES.get(
                                            s.setting,
                                            f"UNKNOWN_{s.setting}"
                                        ): s.new_value
                                        for s in event.changed_settings.values()
                                    }
                                }
                            )

                    elif isinstance(event, h2.events.WindowUpdated):
                        if event.stream_id == 0 and not is_fingerprint_done:
                            h2_fp.window_update = event.delta
                            h2_fp.frames.append(
                                {
                                    "type": "WINDOW_UPDATE",
                                    "stream_id": 0,
                                    "delta": event.delta
                                }
                            )

                    elif isinstance(event, h2.events.PriorityUpdated):
                        if not is_fingerprint_done:
                            h2_fp.has_priority = True
                            h2_fp.frames.append(
                                {
                                    "type": "PRIORITY",
                                    "stream_id": event.stream_id,
                                    "weight": event.weight,
                                    "depends_on": event.depends_on,
                                    "exclusive": event.exclusive
                                }
                            )

                    elif isinstance(event, h2.events.RequestReceived):
                        headers_list = [(k, v) for k, v in event.headers]

                        # capture pseudo-header order from first request
                        if not is_fingerprint_done:
                            h2_fp.pseudo_header_order = [
                                k for k, _v in headers_list
                                if k.startswith(":")
                            ]
                            h2_fp.frames.append(
                                {
                                    "type": "HEADERS",
                                    "stream_id": event.stream_id,
                                    "pseudo_header_order": h2_fp.pseudo_header_order
                                }
                            )
                            # finalize fingerprint
                            is_fingerprint_done = True
                            fp_dict = h2_fingerprint_to_dict(h2_fp)
                            tls_info["h2_fingerprint"] = fp_dict["akamai"]
                            tls_info["h2_fingerprint_hash"] = fp_dict["akamai_hash"]
                            tls_info["h2_frames"] = json.dumps(fp_dict["frames"])

                        # extract request details
                        method = ""
                        path = ""
                        regular_headers: list[tuple[str, str]] = []

                        for k, v in headers_list:
                            if k == ":method":
                                method = v
                            elif k == ":path":
                                path = v
                            elif k.startswith(":"):
                                pass  # skip other pseudo-headers
                            else:
                                regular_headers.append((k, v))

                        url = f"{base_url}{path}"

                        # initialize stream state
                        streams[event.stream_id] = {
                            "method": method,
                            "url": url,
                            "headers": regular_headers,
                            "data": []
                        }

                        if event.stream_ended is not None:
                            # no body, dispatch immediately
                            task = asyncio.create_task(
                                _handle_stream(
                                    event.stream_id,
                                    method,
                                    url,
                                    regular_headers,
                                    None
                                )
                            )
                            stream_tasks.add(task)
                            task.add_done_callback(stream_tasks.discard)
                            del streams[event.stream_id]

                    elif isinstance(event, h2.events.DataReceived):
                        stream_id = event.stream_id
                        if stream_id in streams:
                            streams[stream_id]["data"].append(event.data)
                            # acknowledge received data for flow control
                            h2_conn.acknowledge_received_data(
                                event.flow_controlled_length,
                                stream_id
                            )

                    elif isinstance(event, h2.events.StreamEnded):
                        stream_id = event.stream_id
                        if stream_id in streams:
                            stream = streams.pop(stream_id)
                            body = b"".join(stream["data"]) if stream["data"] else None
                            task = asyncio.create_task(
                                _handle_stream(
                                    stream_id,
                                    stream["method"],
                                    stream["url"],
                                    stream["headers"],
                                    body
                                )
                            )
                            stream_tasks.add(task)
                            task.add_done_callback(stream_tasks.discard)

                    elif isinstance(event, h2.events.StreamReset):
                        streams.pop(event.stream_id, None)

                # flush any pending h2 frames (ACKs, WINDOW_UPDATEs, etc.)
                async with write_lock:
                    bytes_received += await _flush()

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            # wait for all in-flight stream tasks
            if stream_tasks:
                await asyncio.gather(*stream_tasks, return_exceptions=True)
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

        # ConnectionClosed, PAUSED, or other sentinel — stop
        return None, None, total_read


def _send_via_h11(
    conn: h11.Connection,
    writer: asyncio.StreamWriter,
    status_code: int,
    headers: list[tuple[str, str]],
    body: bytes,
) -> int:
    """Serialize and write a full HTTP/1.1 response via h11.

    Returns the number of bytes written.
    """
    h11_headers: list[tuple[str, str]] = list(headers)
    h11_headers.append(("content-length", str(len(body))))

    out = conn.send(h11.Response(status_code=status_code, headers=h11_headers)) or b""
    if body:
        out += conn.send(h11.Data(data=body)) or b""
    out += conn.send(h11.EndOfMessage()) or b""

    writer.write(out)
    return len(out)
