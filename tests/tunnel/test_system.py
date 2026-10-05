# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The privileged runner, the nftables driver and the ruleset every tunnel shares."""

import pytest

from api.tunnel.system import (
    CommandError,
    CommandRunner,
    Netfilter,
    explain,
    privileged_argv,
    render_nft_ruleset,
)


class RecordingRunner(CommandRunner):
    """Records every command and what was fed to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[bytes] = []

    async def run(self, *argv: str, stdin: bytes | None = None, check: bool = True) -> str:
        self.calls.append(argv)
        if stdin is not None:
            self.stdin.append(stdin)
        return ""


@pytest.mark.asyncio
async def test_netfilter_feeds_ruleset_on_stdin_and_removes_table() -> None:
    runner = RecordingRunner()
    nft = Netfilter("octoprox_tunnel", runner)
    await nft.apply("table inet octoprox_tunnel {}\n")
    assert runner.calls[-1] == ("nft", "-f", "-")
    assert runner.stdin == [b"table inet octoprox_tunnel {}\n"]
    await nft.remove()
    assert runner.calls[-1] == ("nft", "delete", "table", "inet", "octoprox_tunnel")


def test_privileged_argv_uses_setpriv_only_for_non_root() -> None:
    argv = ("ip", "link", "add", "dev", "wg0", "type", "wireguard")
    assert privileged_argv(argv, euid=0, setpriv="/usr/bin/setpriv") == list(argv)
    assert privileged_argv(argv, euid=1000, setpriv=None) == list(argv)
    launched = privileged_argv(argv, euid=1000, setpriv="/usr/bin/setpriv")
    assert launched[:1] == ["/usr/bin/setpriv"] and launched[-7:] == list(argv)
    assert "--ambient-caps" in launched and "--inh-caps" in launched


def test_explain_names_the_capability_or_the_missing_tool() -> None:
    assert "CAP_NET_ADMIN" in explain(CommandError(("nft", "-f", "-"), 1, "Operation not permitted"))
    assert "install nftables in the image" in explain(CommandError(("nft",), 127, "nft is not installed"))
    assert explain(CommandError(("ip",), 2, "something else")) == "something else"


class TestNftRuleset:
    def test_redirects_tunnel_tcp_and_dns_and_rejects_other_udp(self) -> None:
        rules = render_nft_ruleset("octoprox_tunnel", ["wg0"], 8081, 5353)
        assert rules.startswith("table inet octoprox_tunnel {}\ndelete table inet octoprox_tunnel\n")
        assert 'iifname { "wg0" } udp dport 53 redirect to :5353' in rules
        assert 'iifname { "wg0" } tcp dport 53 redirect to :5353' in rules
        assert 'iifname { "wg0" } meta l4proto tcp redirect to :8081' in rules
        assert 'iifname { "wg0" } meta l4proto udp reject' in rules
        assert 'iifname { "wg0" } drop' in rules
        # DNS redirect must come before the catch-all TCP redirect.
        assert rules.index("tcp dport 53 redirect") < rules.index("meta l4proto tcp redirect")

    def test_several_interfaces_share_one_table(self) -> None:
        rules = render_nft_ruleset("octoprox_tunnel", {"tun0": "10.67.0.1", "wg0": "10.66.0.1"}, 8081, 5353)
        assert 'iifname { "tun0", "wg0" } meta l4proto tcp redirect to :8081' in rules
        assert 'iifname "wg0"' not in rules

    def test_needs_an_interface(self) -> None:
        with pytest.raises(ValueError):
            render_nft_ruleset("octoprox_tunnel", [], 8081, 5353)
