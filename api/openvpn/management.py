# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The daemon's management interface: how Octoprox admits devices and reads their state.

OpenVPN exposes a line protocol on a unix socket. With
``management-client-auth`` in the daemon's config, every connecting device
is held until the management client answers ``client-auth`` (with the
per-device directives, the fixed address among them) or ``client-deny``.
That makes the peer directory the one authority on who may connect, with no
revocation list and no per-device files: a device that is unknown, disabled
or rotated is refused here. The same connection answers ``status`` for the
sessions the daemon carries and ``kill`` to end one.

Notifications (lines starting with ``>``) arrive interleaved with command
replies; one reader task sorts them, commands take a lock so their replies
cannot interleave either.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

CONNECT_RETRY_SECONDS = 0.2
COMMAND_TIMEOUT_SECONDS = 10.0


class ManagementError(RuntimeError):
    """The daemon refused a command, or the connection is gone."""


@dataclass(frozen=True)
class ClientEvent:
    """One ``>CLIENT:`` notification with its environment.

    ``CONNECT`` and ``REAUTH`` must be answered (the device waits);
    ``ESTABLISHED`` and ``DISCONNECT`` are informational.
    """

    kind: str
    cid: int
    kid: int | None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def common_name(self) -> str:
        return self.env.get("common_name", "")

    @property
    def serial(self) -> str | None:
        """Decimal serial of the device's certificate, as the daemon reports it."""
        return self.env.get("tls_serial_0")

    @property
    def remote(self) -> str | None:
        ip = self.env.get("untrusted_ip") or self.env.get("trusted_ip")
        port = self.env.get("untrusted_port") or self.env.get("trusted_port")
        if not ip:
            return None
        return f"[{ip}]:{port}" if ":" in ip and port else f"{ip}:{port}" if port else ip


@dataclass(frozen=True)
class SessionStatus:
    """One row of ``status 3``: a device the daemon currently carries."""

    common_name: str
    real_address: str
    virtual_address: str
    rx_bytes: int
    tx_bytes: int
    connected_since: int  # unix seconds
    cid: int | None


ClientHandler = Callable[[ClientEvent], Awaitable[None]]

_STATUS_FIELDS = {
    "Common Name": "common_name",
    "Real Address": "real_address",
    "Virtual Address": "virtual_address",
    "Bytes Received": "rx_bytes",
    "Bytes Sent": "tx_bytes",
    "Connected Since (time_t)": "connected_since",
    "Client ID": "cid",
}
# Column order of CLIENT_LIST when no HEADER line precedes it (status-version 3).
_DEFAULT_STATUS_COLUMNS = [
    "Common Name", "Real Address", "Virtual Address", "Virtual IPv6 Address", "Bytes Received", "Bytes Sent",
    "Connected Since", "Connected Since (time_t)", "Username", "Client ID", "Peer ID", "Data Channel Cipher",
]


def parse_status(lines: list[str]) -> list[SessionStatus]:
    """The sessions in a ``status 3`` reply. Fields are tab-separated; the HEADER line names them."""
    columns = list(_DEFAULT_STATUS_COLUMNS)
    sessions: list[SessionStatus] = []
    for line in lines:
        fields = line.split("\t") if "\t" in line else line.split(",")
        if fields[0] == "HEADER" and len(fields) > 2 and fields[1] == "CLIENT_LIST":
            columns = fields[2:]
        elif fields[0] == "CLIENT_LIST":
            row = dict(zip(columns, fields[1:], strict=False))
            try:
                sessions.append(
                    SessionStatus(
                        common_name=row.get("Common Name", ""),
                        real_address=row.get("Real Address", ""),
                        virtual_address=row.get("Virtual Address", ""),
                        rx_bytes=int(row.get("Bytes Received") or 0),
                        tx_bytes=int(row.get("Bytes Sent") or 0),
                        connected_since=int(row.get("Connected Since (time_t)") or 0),
                        cid=int(row["Client ID"]) if row.get("Client ID", "").isdigit() else None,
                    )
                )
            except ValueError:
                continue
    return sessions


class ManagementClient:
    """One connection to the daemon's management socket."""

    def __init__(self, path: str, *, on_client: ClientHandler | None = None) -> None:
        self.path = path
        self._on_client = on_client
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Queue[str | None] | None = None
        self._event: ClientEvent | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, *, timeout: float = 15.0, still_alive: Callable[[], bool] | None = None) -> None:
        """Connect, retrying while the daemon is still creating its socket.

        ``still_alive`` stops the wait early when the daemon has already
        exited, so a bad config is reported in a second, not after the timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.path)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if still_alive is not None and not still_alive():
                    raise ManagementError("the daemon exited before its management socket came up") from None
                if loop.time() >= deadline:
                    raise ManagementError(f"management socket {self.path} did not come up in {timeout:.0f}s") from None
                await asyncio.sleep(CONNECT_RETRY_SECONDS)
                continue
            self._read_task = asyncio.create_task(self._read_loop(), name="openvpn_management_reader")
            return

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        for task in list(self._handlers):
            task.cancel()
        self._handlers.clear()

    # --- commands --------------------------------------------------------------------

    async def command(self, text: str, *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> list[str]:
        """Send one command (several lines for ``client-auth``) and return its reply lines.

        A ``SUCCESS:`` line is returned alone; ``ERROR:`` raises; a multi-line
        reply is everything up to ``END``.
        """
        if self._writer is None or self._reader is None:
            raise ManagementError("not connected to the management socket")
        async with self._lock:
            self._pending = asyncio.Queue()
            try:
                self._writer.write((text.rstrip("\n") + "\n").encode())
                await self._writer.drain()
                lines: list[str] = []
                while True:
                    try:
                        line = await asyncio.wait_for(self._pending.get(), timeout)
                    except TimeoutError:
                        raise ManagementError(f"no reply to {text.split()[0]!r} in {timeout:.0f}s") from None
                    if line is None:
                        raise ManagementError("the management connection closed")
                    if line.startswith("SUCCESS:"):
                        return [line]
                    if line.startswith("ERROR:"):
                        raise ManagementError(line[len("ERROR:") :].strip())
                    if line == "END":
                        return lines
                    lines.append(line)
            finally:
                self._pending = None

    async def state(self) -> str:
        """The daemon's state name (``CONNECTED`` once a server has its interface up)."""
        for line in await self.command("state"):
            fields = line.split(",")
            if len(fields) > 1:
                return fields[1]
        return ""

    async def wait_until_ready(self, *, timeout: float = 30.0, still_alive: Callable[[], bool] | None = None) -> None:
        """Wait for the daemon to finish initialising: the interface exists and has its address then."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if await self.state() == "CONNECTED":
                return
            if still_alive is not None and not still_alive():
                raise ManagementError("the daemon exited while initialising")
            if loop.time() >= deadline:
                raise ManagementError(f"the daemon did not finish initialising in {timeout:.0f}s")
            await asyncio.sleep(0.5)

    async def version(self) -> str | None:
        for line in await self.command("version"):
            if line.startswith("OpenVPN Version:"):
                return line.split(":", 1)[1].strip()
        return None

    async def status(self) -> list[SessionStatus]:
        return parse_status(await self.command("status 3"))

    async def approve(self, cid: int, kid: int, directives: list[str]) -> None:
        """Admit a connecting device with its per-connection directives (``ifconfig-push`` and pushes)."""
        await self.command("\n".join([f"client-auth {cid} {kid}", *directives, "END"]))

    async def approve_without_push(self, cid: int, kid: int) -> None:
        """Admit a renegotiation: the device already has its directives."""
        await self.command(f"client-auth-nt {cid} {kid}")

    async def deny(self, cid: int, kid: int, reason: str) -> None:
        safe = reason.replace('"', "'")
        await self.command(f'client-deny {cid} {kid} "{safe}" "{safe}"')

    async def kill(self, common_name: str) -> bool:
        """End the device's session if the daemon carries one; False when it does not."""
        try:
            await self.command(f"kill {common_name}")
        except ManagementError as exc:
            if "not found" in str(exc):
                return False
            raise
        return True

    # --- reading ---------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(">"):
                    try:
                        self._notification(line[1:])
                    except Exception as exc:
                        # One line the daemon mangled must not cost the connection:
                        # with it gone, every device would hang at connect.
                        logger.warning("Unparseable management notification", line=line, error=str(exc))
                elif self._pending is not None:
                    self._pending.put_nowait(line)
                else:
                    logger.debug("Unsolicited management line", line=line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Management connection failed", error=str(exc))
        finally:
            if self._pending is not None:
                self._pending.put_nowait(None)
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    def _notification(self, line: str) -> None:
        kind, _, rest = line.partition(":")
        if kind != "CLIENT":
            if kind == "FATAL":
                logger.error("OpenVPN fatal", message=rest)
            return
        fields = rest.split(",")
        event = fields[0]
        if event in ("CONNECT", "REAUTH") and len(fields) >= 3:
            self._event = ClientEvent(kind=event, cid=int(fields[1]), kid=int(fields[2]))
        elif event in ("ESTABLISHED", "DISCONNECT") and len(fields) >= 2:
            self._event = ClientEvent(kind=event, cid=int(fields[1]), kid=None)
        elif event == "ENV" and self._event is not None:
            payload = rest[len("ENV,") :]
            if payload == "END":
                pending, self._event = self._event, None
                if self._on_client is not None:
                    task = asyncio.create_task(self._dispatch(pending))
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
            else:
                name, _, value = payload.partition("=")
                self._event.env[name] = value
        # CLIENT:ADDRESS and anything else carry nothing the directory needs.

    async def _dispatch(self, event: ClientEvent) -> None:
        assert self._on_client is not None
        try:
            await self._on_client(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Client event handler failed", kind=event.kind, cid=event.cid, error=str(exc))
            if event.kind in ("CONNECT", "REAUTH") and event.kid is not None:
                # Never leave a device hanging: refuse it rather than let it time out.
                with contextlib.suppress(Exception):
                    await self.deny(event.cid, event.kid, "internal error")
