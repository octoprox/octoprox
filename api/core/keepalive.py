# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""TCP keepalive on a socket, so a peer that vanished without closing it is found out: one helper for every connection that sets it."""

from __future__ import annotations

import socket

# The option that sets the idle time: TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS.
_TCP_KEEPIDLE: int | None = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))


def set_keepalive(
    sock: socket.socket, *, idle: int, interval: int, probes: int, user_timeout_ms: int | None = None
) -> None:
    """Have the kernel probe ``sock``'s peer after ``idle`` seconds without traffic, every ``interval``, giving up after ``probes``; each option the platform lacks is left out.

    Args:
        user_timeout_ms: how long anything sent (data, a close, a keepalive or zero-window probe) may stay
            unacknowledged before the connection fails; set, Linux gives up after it instead of after ``probes``.
            Linux only; None leaves it.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    options = (
        (_TCP_KEEPIDLE, idle),
        (getattr(socket, "TCP_KEEPINTVL", None), interval),
        (getattr(socket, "TCP_KEEPCNT", None), probes),
        (getattr(socket, "TCP_USER_TIMEOUT", None), user_timeout_ms),
    )
    for option, value in options:
        if option is not None and value is not None:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
