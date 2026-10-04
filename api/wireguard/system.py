# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The host side of the endpoint: the WireGuard interface and the nftables table.

Everything here shells out to ``ip``, ``wg``, ``nft`` and, when the kernel has
no WireGuard, ``wireguard-go``. The tools need CAP_NET_ADMIN. When the process
is not root they are launched through ``setpriv``, which the image gives the
capability as a file capability and which passes it on as an *ambient*
capability, so the process itself stays unprivileged. A file capability on
the tools themselves would not do: iproute2 drops its capabilities when run
by a non-root user unless they are inheritable, and ambient makes them so.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import structlog

from api.models.wireguard import WireGuardPeer
from api.wireguard.config import render_server_conf

logger = structlog.get_logger()

Backend = Literal["kernel", "userspace"]


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


def explain(exc: CommandError) -> str:
    """The error with the hint an operator needs most often."""
    text = exc.stderr or str(exc)
    if "not permitted" in text.lower():
        return (
            f"{text} (the tools need CAP_NET_ADMIN: in Docker add cap_add: [NET_ADMIN]; "
            "a non-root process also needs setpriv with the capability set on it, as the image does)"
        )
    if exc.returncode == 127:
        return f"{text} (install wireguard-tools, nftables and iproute2 in the image)"
    return text


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


class Netfilter:
    """Owns one nftables table and nothing else on the host."""

    def __init__(self, table: str = "octoprox_wg", runner: CommandRunner | None = None) -> None:
        self.table = table
        self._runner = runner or CommandRunner()

    async def apply(self, ruleset: str) -> None:
        await self._runner.run("nft", "-f", "-", stdin=ruleset.encode())
        logger.info("nftables rules applied", table=self.table)

    async def remove(self) -> None:
        await self._runner.run("nft", "delete", "table", "inet", self.table, check=False)
