# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The management protocol against a scripted daemon on a unix socket."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

from api.openvpn.management import ClientEvent, ManagementClient, ManagementError, parse_status

STATUS_3 = [
    "TITLE\tOpenVPN 2.6.3",
    "TIME\t2026-10-05 10:00:00\t1759658400",
    "HEADER\tCLIENT_LIST\tCommon Name\tReal Address\tVirtual Address\tVirtual IPv6 Address\tBytes Received\tBytes Sent\tConnected Since\tConnected Since (time_t)\tUsername\tClient ID\tPeer ID\tData Channel Cipher",
    "CLIENT_LIST\tdev-1\t203.0.113.9:40123\t10.67.0.2\t\t1500\t98000\t2026-10-05 09:59:00\t1759658340\tUNDEF\t7\t0\tAES-256-GCM",
    "CLIENT_LIST\tdev-2\t198.51.100.4:5000\t10.67.0.3\t\tnot-a-number\t1\t2026-10-05 09:59:00\t1759658340\tUNDEF\t8\t1\tAES-256-GCM",
    "HEADER\tROUTING_TABLE\tVirtual Address\tCommon Name\tReal Address\tLast Ref\tLast Ref (time_t)",
    "ROUTING_TABLE\t10.67.0.2\tdev-1\t203.0.113.9:40123\t2026-10-05 10:00:00\t1759658400",
    "GLOBAL_STATS\tMax bcast/mcast queue length\t0",
]


def test_parse_status_reads_the_header_and_skips_bad_rows() -> None:
    sessions = parse_status(STATUS_3)
    assert len(sessions) == 1
    s = sessions[0]
    assert (s.common_name, s.real_address, s.virtual_address) == ("dev-1", "203.0.113.9:40123", "10.67.0.2")
    assert (s.rx_bytes, s.tx_bytes, s.connected_since, s.cid) == (1500, 98000, 1759658340, 7)
    # Without a header the default column order applies.
    assert parse_status([STATUS_3[3]])[0].cid == 7


def test_client_event_accessors() -> None:
    event = ClientEvent(kind="CONNECT", cid=1, kid=0, env={
        "common_name": "dev-1", "tls_serial_0": "42", "untrusted_ip": "203.0.113.9", "untrusted_port": "40123",
    })
    assert event.common_name == "dev-1" and event.serial == "42" and event.remote == "203.0.113.9:40123"
    assert ClientEvent(kind="CONNECT", cid=1, kid=0, env={"untrusted_ip": "2001:db8::1", "untrusted_port": "5"}).remote == "[2001:db8::1]:5"
    assert ClientEvent(kind="CONNECT", cid=1, kid=0).remote is None


class ScriptedDaemon:
    """Answers management commands the way openvpn does, and can raise client events."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.commands: list[str] = []
        self.server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.kill_found = True

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, self.path)

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def emit(self, lines: list[str]) -> None:
        assert self._writer is not None
        self._writer.write("".join(line + "\r\n" for line in lines).encode())
        await self._writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            await self._converse(reader, writer)
        finally:
            # wait_closed (3.12.1+) waits for every connection: close ours when the client goes.
            writer.close()

    async def _converse(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b">INFO:OpenVPN Management Interface Version 5 -- type 'help' for more info\r\n")
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            self.commands.append(line)
            if line == "state":
                reply = ["1759658400,CONNECTED,SUCCESS,10.67.0.1,,,,", "END"]
            elif line == "version":
                reply = ["OpenVPN Version: OpenVPN 2.6.3 x86_64-pc-linux-gnu", "Management Version: 5", "END"]
            elif line == "status 3":
                reply = [*STATUS_3, "END"]
            elif line.startswith("client-auth "):
                while (await reader.readline()).decode().rstrip("\r\n") != "END":
                    pass
                reply = ["SUCCESS: client-auth command succeeded"]
            elif line.startswith("client-auth-nt ") or line.startswith("client-deny "):
                reply = ["SUCCESS: client-deny command succeeded"]
            elif line.startswith("kill "):
                name = line.split(" ", 1)[1]
                reply = [f"SUCCESS: common name '{name}' found, 1 client(s) killed" if self.kill_found else f"ERROR: common name '{name}' not found"]
            else:
                reply = ["ERROR: unknown command, enter 'help' for more options"]
            writer.write("".join(r + "\r\n" for r in reply).encode())
            await writer.drain()


@pytest.fixture
async def daemon() -> AsyncIterator[ScriptedDaemon]:
    with tempfile.TemporaryDirectory(prefix="octoprox-ovpn-test-") as directory:
        scripted = ScriptedDaemon(os.path.join(directory, "m.sock"))
        await scripted.start()
        try:
            yield scripted
        finally:
            await scripted.stop()


@pytest.mark.asyncio
async def test_commands_and_client_events(daemon: ScriptedDaemon) -> None:
    events: list[ClientEvent] = []
    answered = asyncio.Event()

    async def on_client(event: ClientEvent) -> None:
        events.append(event)
        answered.set()

    client = ManagementClient(daemon.path, on_client=on_client)
    await client.connect(timeout=2)
    try:
        assert client.connected
        await client.wait_until_ready(timeout=2)
        assert await client.version() == "OpenVPN 2.6.3 x86_64-pc-linux-gnu"
        assert [s.common_name for s in await client.status()] == ["dev-1"]

        await client.approve(3, 0, ["ifconfig-push 10.67.0.2 255.255.0.0"])
        assert daemon.commands[-1] == "client-auth 3 0"
        await client.approve_without_push(3, 1)
        await client.deny(4, 0, 'device "disabled"')
        assert daemon.commands[-1] == "client-deny 4 0 \"device 'disabled'\" \"device 'disabled'\""
        assert await client.kill("dev-1") is True
        daemon.kill_found = False
        assert await client.kill("dev-9") is False
        with pytest.raises(ManagementError):
            await client.command("bogus")

        # A notification interleaved with a command reply reaches the handler, not the command.
        await daemon.emit([
            ">CLIENT:CONNECT,5,0", ">CLIENT:ENV,common_name=dev-1", ">CLIENT:ENV,tls_serial_0=42",
            ">CLIENT:ENV,untrusted_ip=203.0.113.9", ">CLIENT:ENV,END", ">CLIENT:ADDRESS,5,10.67.0.2,1",
        ])
        await asyncio.wait_for(answered.wait(), 2)
        assert events[0].kind == "CONNECT" and events[0].cid == 5 and events[0].kid == 0
        assert events[0].common_name == "dev-1" and events[0].serial == "42"
        assert await client.state() == "CONNECTED"
    finally:
        await client.close()
    assert not client.connected


@pytest.mark.asyncio
async def test_connect_waits_for_the_socket_and_gives_up_when_the_daemon_died() -> None:
    with tempfile.TemporaryDirectory(prefix="octoprox-ovpn-test-") as directory:
        path = os.path.join(directory, "late.sock")
        client = ManagementClient(path)
        with pytest.raises(ManagementError, match="exited"):
            await client.connect(timeout=5, still_alive=lambda: False)
        with pytest.raises(ManagementError, match="did not come up"):
            await client.connect(timeout=0.3)

        scripted = ScriptedDaemon(path)

        async def start_late() -> None:
            await asyncio.sleep(0.3)
            await scripted.start()

        task = asyncio.create_task(start_late())
        await client.connect(timeout=3)
        await task
        try:
            assert await client.state() == "CONNECTED"
        finally:
            await client.close()
            await scripted.stop()


@pytest.mark.asyncio
async def test_handler_failure_denies_the_device(daemon: ScriptedDaemon) -> None:
    async def on_client(event: ClientEvent) -> None:
        raise RuntimeError("boom")

    client = ManagementClient(daemon.path, on_client=on_client)
    await client.connect(timeout=2)
    try:
        await client.state()  # the daemon has accepted us once it answers
        await daemon.emit([">CLIENT:CONNECT,9,0", ">CLIENT:ENV,common_name=x", ">CLIENT:ENV,END"])
        for _ in range(20):
            await asyncio.sleep(0.05)
            if any(c.startswith("client-deny 9 0") for c in daemon.commands):
                break
        assert any(c.startswith('client-deny 9 0 "internal error"') for c in daemon.commands)
    finally:
        await client.close()
