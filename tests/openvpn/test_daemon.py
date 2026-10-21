# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The daemon's output in the log: what an operator needs at info, the management chatter at debug."""

from __future__ import annotations

import asyncio

import structlog

from api.openvpn.daemon import OpenVpnDaemon
from api.tunnel.system import CommandRunner

# What OpenVPN 2.6 at verb 3 prints on an idle gateway, and when a device connects or fails.
OUTPUT = [
    "2026-10-18 05:03:04 MANAGEMENT: CMD 'status 3'",
    "2026-10-18 05:03:05 10.0.0.9:51234 [tv] Peer Connection Initiated with [AF_INET]10.0.0.9:51234",
    "2026-10-18 05:03:06 TLS Error: TLS handshake failed",
    "2026-10-18 05:03:14 MANAGEMENT: CMD 'kill 7'",
]


class PrintingRunner(CommandRunner):
    """Runs ``printf`` with :data:`OUTPUT` in place of the daemon."""

    async def spawn(self, *argv: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "printf",
            "%s\\n",
            *OUTPUT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )


async def test_management_commands_are_logged_at_debug_and_the_rest_as_before() -> None:
    daemon = OpenVpnDaemon(PrintingRunner())
    with structlog.testing.capture_logs() as logs:
        await daemon.start("/dev/null")
        await daemon.wait()
        await daemon.stop()

    lines = [(log["log_level"], log["line"]) for log in logs if log["event"] == "openvpn"]
    assert lines == [
        ("debug", OUTPUT[0]),
        ("info", OUTPUT[1]),
        ("warning", OUTPUT[2]),
        ("debug", OUTPUT[3]),
    ]
    assert daemon.recent_output() == "\n".join(OUTPUT)
