# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Interfaces attach to and detach from one data plane; the listeners and the table follow."""

from unittest.mock import MagicMock

import pytest

from api.core.config import Settings
from api.tunnel.dataplane import TunnelDataPlane
from api.tunnel.peers import AddressDirectory, PeerIndex
from tests.tunnel.test_system import RecordingRunner
from tests.tunnel.test_transparent import _Peer


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, tunnel_transparent_port=0, tunnel_dns_port=0, **overrides)  # type: ignore[call-arg]


def _rulesets(runner: RecordingRunner) -> list[str]:
    return [data.decode() for call, data in zip([c for c in runner.calls if c[0] == "nft" and c[1] == "-f"], runner.stdin, strict=True)]


@pytest.mark.asyncio
async def test_attach_binds_listeners_and_rules_follow_the_interfaces() -> None:
    runner = RecordingRunner()
    plane = TunnelDataPlane(_settings(), None, runner=runner)
    with pytest.raises(RuntimeError):
        await plane.attach("wg0", "127.0.0.1")
    await plane.start(MagicMock())
    assert plane.started and not plane.interfaces

    await plane.attach("wg0", "127.0.0.1")
    assert plane.dns_server is not None and plane.transparent_server is not None
    assert plane.dns_server.hosts == ["127.0.0.1"] and plane.transparent_server.hosts == ["127.0.0.1"]
    assert plane.dns_server.port > 0 and plane.transparent_server.port > 0
    assert 'iifname { "wg0" }' in _rulesets(runner)[-1]
    assert f"redirect to :{plane.transparent_server.port}" in _rulesets(runner)[-1]

    # A second interface on the same gateway shares the sockets; the table lists both.
    await plane.attach("tun0", "127.0.0.1")
    assert plane.interfaces == {"wg0": "127.0.0.1", "tun0": "127.0.0.1"}
    assert plane.dns_server.hosts == ["127.0.0.1"]
    assert 'iifname { "tun0", "wg0" }' in _rulesets(runner)[-1]

    # Attaching again is a no-op; detaching one keeps the shared sockets.
    await plane.attach("wg0", "127.0.0.1")
    await plane.detach("wg0")
    assert plane.interfaces == {"tun0": "127.0.0.1"}
    assert plane.transparent_server.is_listening and plane.dns_server.is_listening
    assert 'iifname { "tun0" }' in _rulesets(runner)[-1]

    # The last one takes the table and the sockets with it.
    await plane.detach("tun0")
    await plane.detach("tun0")
    assert plane.interfaces == {}
    assert runner.calls[-1] == ("nft", "delete", "table", "inet", "octoprox_tunnel")
    assert not plane.transparent_server.is_listening and not plane.dns_server.is_listening

    await plane.stop()
    assert not plane.started


@pytest.mark.asyncio
async def test_failed_attach_leaves_nothing_behind() -> None:
    class FailingRunner(RecordingRunner):
        async def run(self, *argv: str, stdin: bytes | None = None, check: bool = True) -> str:
            await super().run(*argv, stdin=stdin, check=check)
            if argv[:2] == ("nft", "-f"):
                raise RuntimeError("nft: Operation not permitted")
            return ""

    plane = TunnelDataPlane(_settings(), None, runner=FailingRunner())
    await plane.start(MagicMock())
    with pytest.raises(RuntimeError):
        await plane.attach("wg0", "127.0.0.1")
    assert plane.interfaces == {}
    assert plane.dns_server is not None and not plane.dns_server.is_listening
    assert plane.transparent_server is not None and not plane.transparent_server.is_listening
    await plane.stop()


@pytest.mark.asyncio
async def test_reattach_on_another_gateway_releases_the_old_one() -> None:
    runner = RecordingRunner()
    plane = TunnelDataPlane(_settings(), None, runner=runner)
    await plane.start(MagicMock())
    await plane.attach("wg0", "127.0.0.1")
    await plane.attach("wg0", "0.0.0.0")
    assert plane.interfaces == {"wg0": "0.0.0.0"}
    assert plane.dns_server is not None and plane.dns_server.hosts == ["0.0.0.0"]
    assert plane.transparent_server is not None and plane.transparent_server.hosts == ["0.0.0.0"]
    await plane.stop()


@pytest.mark.asyncio
async def test_stop_without_an_attached_interface_leaves_nftables_alone() -> None:
    runner = RecordingRunner()
    plane = TunnelDataPlane(_settings(), None, runner=runner)
    await plane.start(MagicMock())
    await plane.stop()
    assert runner.calls == []


def test_index_asks_every_directory() -> None:
    first: AddressDirectory[_Peer] = AddressDirectory()
    second: AddressDirectory[_Peer] = AddressDirectory()
    first.put(_Peer(project_id="p", name="tv", address="10.66.0.2", id="a"))
    second.put(_Peer(project_id="p", name="console", address="10.67.0.2", id="b"))
    index = PeerIndex()
    index.add(first)
    index.add(second)
    index.add(first)
    assert index.by_address("10.66.0.2") is not None and index.by_address("10.66.0.2").id == "a"  # type: ignore[union-attr]
    assert index.by_address("10.67.0.2") is not None and index.by_address("10.67.0.2").id == "b"  # type: ignore[union-attr]
    assert index.by_address("10.68.0.2") is None
