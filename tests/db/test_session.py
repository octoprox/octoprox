# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the engine every Postgres role builds, against the real Postgres test container.

Its connections must give up on a server that went silent within seconds
rather than when the kernel stops retransmitting: what the socket options
of each new connection are for.
"""

import socket

from sqlalchemy import text

from api.core.config import Settings
from api.db.session import get_async_engine

# The idle-time option: TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS.
TCP_KEEPIDLE: int = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", 0))


async def test_every_connection_gives_up_on_a_silent_server(test_settings: Settings) -> None:
    engine = get_async_engine(test_settings.database_url, "octoprox-test")
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT 1")) == 1
            raw = await conn.get_raw_connection()
            sock = raw.driver_connection._transport.get_extra_info("socket")  # type: ignore[union-attr]
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            # Idle seconds, probe interval and probes, on every platform.
            assert (
                sock.getsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE),
                sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
                sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
            ) == (10, 5, 3)
            # Linux, where the images run: what bounds a pre-ping or a query on a dead connection.
            if hasattr(socket, "TCP_USER_TIMEOUT"):
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 20_000
    finally:
        await engine.dispose()
