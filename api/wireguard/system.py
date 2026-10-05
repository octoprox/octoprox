# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The WireGuard interface on the host.

Shells out to ``ip``, ``wg`` and, when the kernel has no WireGuard,
``wireguard-go``, through the privileged runner every tunnel shares (see
:mod:`api.tunnel.system` for how the capability reaches the tools).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import structlog

from api.models.wireguard import WireGuardPeer
from api.tunnel.system import CommandError, CommandRunner
from api.wireguard.config import render_server_conf

logger = structlog.get_logger()

Backend = Literal["kernel", "userspace"]


def _kernel_unsupported(exc: CommandError) -> bool:
    text = exc.stderr.lower()
    return "not supported" in text or "unknown device type" in text


@dataclass(frozen=True)
class PeerDump:
    """One line of ``wg show <interface> dump`` for a peer."""

    public_key: str
    endpoint: str | None
    latest_handshake: int  # unix seconds, 0 when never
    rx_bytes: int
    tx_bytes: int


class WireGuardInterface:
    """Creates, configures, inspects and removes the tunnel interface."""

    def __init__(self, name: str, runner: CommandRunner | None = None, *, wireguard_go: str = "wireguard-go") -> None:
        self.name = name
        self._runner = runner or CommandRunner()
        self._wireguard_go = wireguard_go
        self.backend: Backend | None = None

    async def exists(self) -> bool:
        try:
            await self._runner.run("ip", "link", "show", "dev", self.name)
        except CommandError:
            return False
        return True

    async def up(
        self,
        *,
        private_key: str,
        listen_port: int,
        address: str,
        mtu: int,
        peers: Iterable[WireGuardPeer],
    ) -> None:
        """Create the interface, give it our key, address and peers, and bring it up.

        A leftover interface from a crashed process is removed first so the
        configuration below is the whole truth. The kernel implementation is
        tried first; when the kernel has no WireGuard, ``wireguard-go`` creates
        the same interface in userspace and ``wg`` configures it the same way.
        """
        if await self.exists():
            await self.down()
        try:
            await self._runner.run("ip", "link", "add", "dev", self.name, "type", "wireguard")
            self.backend = "kernel"
        except CommandError as exc:
            if not _kernel_unsupported(exc):
                raise
            logger.info("Kernel has no WireGuard, starting wireguard-go", interface=self.name)
            await self._runner.run(self._wireguard_go, self.name)
            self.backend = "userspace"
        await self._configure("setconf", private_key, listen_port, peers)
        try:
            await self._runner.run("ip", "address", "add", address, "dev", self.name)
        except CommandError as exc:
            if "exists" not in exc.stderr.lower():
                raise
        await self._runner.run("ip", "link", "set", "mtu", str(mtu), "up", "dev", self.name)
        logger.info("WireGuard interface up", interface=self.name, backend=self.backend, address=address, port=listen_port)

    async def sync(self, *, private_key: str, listen_port: int, peers: Iterable[WireGuardPeer]) -> None:
        """Apply the peer set (and key or port) without disturbing sessions that did not change."""
        await self._configure("syncconf", private_key, listen_port, peers)

    async def _configure(
        self, command: str, private_key: str, listen_port: int, peers: Iterable[WireGuardPeer]
    ) -> None:
        # wg reads keys from a file, never from the command line, so the server
        # private key touches disk only for the length of the call, in a file
        # only this user can read.
        fd, path = tempfile.mkstemp(prefix="octoprox-wg-", suffix=".conf")
        try:
            try:
                os.write(fd, render_server_conf(private_key, listen_port, peers).encode())
            finally:
                os.close(fd)
            await self._runner.run("wg", command, self.name, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def down(self) -> None:
        """Remove the interface. Deleting it also ends a wireguard-go process serving it."""
        await self._runner.run("ip", "link", "del", "dev", self.name, check=False)
        self.backend = None

    async def dump(self) -> list[PeerDump]:
        """Per-peer handshake and transfer counters."""
        out = await self._runner.run("wg", "show", self.name, "dump")
        return parse_dump(out)


def parse_dump(text: str) -> list[PeerDump]:
    """Parse ``wg show <interface> dump``: the first line is the interface, the rest are peers."""
    peers: list[PeerDump] = []
    for line in text.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        endpoint = fields[2] if fields[2] not in ("", "(none)") else None
        try:
            peers.append(
                PeerDump(
                    public_key=fields[0],
                    endpoint=endpoint,
                    latest_handshake=int(fields[4]),
                    rx_bytes=int(fields[5]),
                    tx_bytes=int(fields[6]),
                )
            )
        except ValueError:
            continue
    return peers
