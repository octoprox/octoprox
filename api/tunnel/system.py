# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Host-side tooling every tunnel shares: privileged commands and the nftables table.

A tunnel changes the host through ``ip``, ``nft`` and its own tools, all of
which need CAP_NET_ADMIN. When the process is not root they are launched
through ``setpriv``, which the image gives the capability as a file
capability and which passes it on as an *ambient* capability, so the process
itself stays unprivileged. A file capability on the tools themselves would
not do: iproute2 drops its capabilities when run by a non-root user unless
they are inheritable, and ambient makes them so.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterable, Sequence

import structlog

logger = structlog.get_logger()


class CommandError(RuntimeError):
    """A tool exited non-zero or is not installed."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} failed ({returncode}): {stderr}")


# How a privileged tool is launched by a non-root process: setpriv raises
# the capabilities into the inheritable and ambient sets before exec, so the
# tool starts with them and (iproute2) keeps them.
AMBIENT_CAPS = "+net_admin,+net_raw"


def privileged_argv(argv: Sequence[str], *, euid: int, setpriv: str | None) -> list[str]:
    """``argv`` as it must be launched: as is for root or without setpriv, else through setpriv."""
    if euid == 0 or setpriv is None:
        return list(argv)
    return [setpriv, "--inh-caps", AMBIENT_CAPS, "--ambient-caps", AMBIENT_CAPS, "--", *argv]


class CommandRunner:
    """Runs a tool and returns its stdout; the one seam the tests replace."""

    def __init__(self) -> None:
        self._euid = os.geteuid()
        self._setpriv = shutil.which("setpriv")

    async def run(self, *argv: str, stdin: bytes | None = None, check: bool = True) -> str:
        launch = privileged_argv(argv, euid=self._euid, setpriv=self._setpriv)
        try:
            process = await asyncio.create_subprocess_exec(
                *launch,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise CommandError(argv, 127, f"{argv[0]} is not installed") from None
        out, err = await process.communicate(stdin)
        if check and process.returncode != 0:
            raise CommandError(argv, process.returncode or 1, err.decode("utf-8", "replace").strip())
        return out.decode("utf-8", "replace")


# The package a missing tool comes from, which is what the hint has to name.
_PACKAGE_OF = {"ip": "iproute2", "nft": "nftables", "wg": "wireguard-tools", "wireguard-go": "wireguard-go"}


def explain(exc: CommandError) -> str:
    """The error with the hint an operator needs most often."""
    text = exc.stderr or str(exc)
    if "not permitted" in text.lower():
        return (
            f"{text} (the tools need CAP_NET_ADMIN: in Docker add cap_add: [NET_ADMIN]; "
            "a non-root process also needs setpriv with the capability set on it, as the image does)"
        )
    if exc.returncode == 127:
        tool = os.path.basename(exc.argv[0]) if exc.argv else "the tool"
        return f"{text} (install {_PACKAGE_OF.get(tool, tool)} in the image)"
    return text


# One table for every tunnel interface this process attaches; the data plane
# replaces it whole whenever the set of interfaces changes.
DEFAULT_TABLE = "octoprox_tunnel"


class Netfilter:
    """Owns one nftables table and nothing else on the host."""

    def __init__(self, table: str = DEFAULT_TABLE, runner: CommandRunner | None = None) -> None:
        self.table = table
        self._runner = runner or CommandRunner()

    async def apply(self, ruleset: str) -> None:
        await self._runner.run("nft", "-f", "-", stdin=ruleset.encode())
        logger.info("nftables rules applied", table=self.table)

    async def remove(self) -> None:
        await self._runner.run("nft", "delete", "table", "inet", self.table, check=False)


def render_nft_ruleset(table: str, interfaces: Iterable[str], transparent_port: int, dns_port: int) -> str:
    """The nftables table that turns tunnel traffic into connections to our listeners.

    Replaces the table atomically (create-if-missing, delete, define) so a
    restart after a crash never stacks rules. Everything TCP coming off a
    tunnel interface is redirected to the transparent listener and port 53
    in either protocol to the fake-IP resolver, so even a device with a
    hard-coded resolver gets our answers. ``redirect`` sends each connection
    to the primary address of the interface it arrived on, which is where
    the listeners bind for that tunnel. Any other UDP is rejected rather
    than dropped: the ICMP unreachable makes a QUIC client fall back to TCP
    at once instead of after a timeout. Nothing is forwarded; a tunnel only
    reaches the proxy pool.
    """
    names = sorted(set(interfaces))
    if not names:
        raise ValueError("at least one tunnel interface is needed")
    match = "iifname { " + ", ".join(f'"{name}"' for name in names) + " }"
    return f"""table inet {table} {{}}
delete table inet {table}
table inet {table} {{
    chain prerouting {{
        type nat hook prerouting priority dstnat; policy accept;
        {match} udp dport 53 redirect to :{dns_port}
        {match} tcp dport 53 redirect to :{dns_port}
        {match} meta l4proto tcp redirect to :{transparent_port}
    }}
    chain input {{
        type filter hook input priority filter; policy accept;
        {match} udp dport {dns_port} accept
        {match} tcp dport {{ {dns_port}, {transparent_port} }} accept
        {match} meta l4proto udp reject
        {match} meta l4proto tcp reject with tcp reset
    }}
    chain forward {{
        type filter hook forward priority filter; policy accept;
        {match} drop
    }}
}}
"""
