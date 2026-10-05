# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The interface driver, against a recorded command runner."""

from pathlib import Path

import pytest

from api.models.wireguard import WireGuardPeer
from api.tunnel.system import CommandError, CommandRunner, explain
from api.wireguard.system import WireGuardInterface

PEER = WireGuardPeer(project_id="p", name="tv", public_key="pub", preshared_key="psk", address="10.66.0.2")


class FakeRunner(CommandRunner):
    """Records every command; fails those the test says fail; captures wg conf files."""

    def __init__(self, failures: dict[str, str] | None = None, exists: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[bytes] = []
        self.conf_files: list[str] = []
        self.failures = failures or {}
        self.exists = exists

    async def run(self, *argv: str, stdin: bytes | None = None, check: bool = True) -> str:
        self.calls.append(argv)
        if stdin is not None:
            self.stdin.append(stdin)
        if argv[:2] == ("wg", "setconf") or argv[:2] == ("wg", "syncconf"):
            self.conf_files.append(Path(argv[3]).read_text())
        if argv[:3] == ("ip", "link", "show"):
            if not self.exists:
                raise CommandError(argv, 1, 'Device "wg0" does not exist.')
            return ""
        joined = " ".join(argv)
        for prefix, stderr in self.failures.items():
            if joined.startswith(prefix):
                if check:
                    raise CommandError(argv, 2, stderr)
                return ""
        if argv[:3] == ("wg", "show", "wg0"):
            return "srv\tpub\t51820\toff\npub\tpsk\t1.2.3.4:5\t10.66.0.2/32\t1700000000\t10\t20\toff\n"
        return ""


@pytest.mark.asyncio
async def test_kernel_interface_up_and_down() -> None:
    runner = FakeRunner()
    iface = WireGuardInterface("wg0", runner)
    await iface.up(private_key="sk", listen_port=51820, address="10.66.0.1/16", mtu=1420, peers=[PEER])
    assert iface.backend == "kernel"
    assert ("ip", "link", "add", "dev", "wg0", "type", "wireguard") in runner.calls
    assert ("ip", "address", "add", "10.66.0.1/16", "dev", "wg0") in runner.calls
    assert ("ip", "link", "set", "mtu", "1420", "up", "dev", "wg0") in runner.calls
    assert runner.calls[-3][:3] == ("wg", "setconf", "wg0")
    assert "PrivateKey = sk" in runner.conf_files[0] and "AllowedIPs = 10.66.0.2/32" in runner.conf_files[0]
    # The key file is gone once wg has read it.
    assert not Path(runner.calls[-3][3]).exists()

    await iface.sync(private_key="sk", listen_port=51820, peers=[])
    assert runner.calls[-1][:3] == ("wg", "syncconf", "wg0")
    assert "[Peer]" not in runner.conf_files[1]

    dumps = await iface.dump()
    assert dumps[0].public_key == "pub" and dumps[0].rx_bytes == 10

    await iface.down()
    assert runner.calls[-1] == ("ip", "link", "del", "dev", "wg0")
    assert iface.backend is None


@pytest.mark.asyncio
async def test_falls_back_to_wireguard_go() -> None:
    runner = FakeRunner(failures={"ip link add": "RTNETLINK answers: Operation not supported"})
    iface = WireGuardInterface("wg0", runner, wireguard_go="/usr/bin/wireguard-go")
    await iface.up(private_key="sk", listen_port=1, address="10.66.0.1/16", mtu=1420, peers=[])
    assert iface.backend == "userspace"
    assert ("/usr/bin/wireguard-go", "wg0") in runner.calls


@pytest.mark.asyncio
async def test_other_failures_propagate_with_a_hint() -> None:
    runner = FakeRunner(failures={"ip link add": "RTNETLINK answers: Operation not permitted"})
    iface = WireGuardInterface("wg0", runner)
    with pytest.raises(CommandError) as info:
        await iface.up(private_key="sk", listen_port=1, address="10.66.0.1/16", mtu=1420, peers=[])
    assert "CAP_NET_ADMIN" in explain(info.value)
    assert "install wireguard-tools in the image" in explain(CommandError(("wg",), 127, "wg is not installed"))


@pytest.mark.asyncio
async def test_stale_interface_is_removed_first_and_existing_address_tolerated() -> None:
    runner = FakeRunner(failures={"ip address add": "RTNETLINK answers: File exists"}, exists=True)
    iface = WireGuardInterface("wg0", runner)
    await iface.up(private_key="sk", listen_port=1, address="10.66.0.1/16", mtu=1420, peers=[])
    assert runner.calls[1] == ("ip", "link", "del", "dev", "wg0")
    assert iface.backend == "kernel"
