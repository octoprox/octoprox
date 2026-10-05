# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What devices and wg are told."""

import pytest

from api.models.wireguard import (
    WireGuardPeer,
    WireGuardServerSettings,
    validate_endpoint_host,
    validate_subnet,
)
from api.wireguard.config import (
    PRIVATE_KEY_PLACEHOLDER,
    client_conf_filename,
    render_client_conf,
    render_server_conf,
)
from api.wireguard.system import parse_dump

SERVER = WireGuardServerSettings(
    private_key="server-private", public_key="server-public", endpoint_host="vpn.example.net", endpoint_port=51820,
)


def _peer(**overrides: object) -> WireGuardPeer:
    base: dict[str, object] = {
        "project_id": "p1", "name": "Living room TV", "public_key": "peer-public",
        "private_key": "peer-private", "preshared_key": "psk", "address": "10.66.0.2",
    }
    base.update(overrides)
    return WireGuardPeer(**base)  # type: ignore[arg-type]


class TestServerConf:
    def test_lists_enabled_peers_only(self) -> None:
        conf = render_server_conf("sk", 51820, [_peer(), _peer(name="off", public_key="other", address="10.66.0.3", enabled=False)])
        assert "PrivateKey = sk" in conf and "ListenPort = 51820" in conf
        assert conf.count("[Peer]") == 1
        assert "PublicKey = peer-public" in conf
        assert "PresharedKey = psk" in conf
        assert "AllowedIPs = 10.66.0.2/32" in conf
        assert "other" not in conf

    def test_peer_without_psk(self) -> None:
        conf = render_server_conf("sk", 1, [_peer(preshared_key=None)])
        assert "PresharedKey" not in conf


class TestClientConf:
    def test_full_tunnel_with_tunnel_dns(self) -> None:
        conf = render_client_conf(_peer(), SERVER)
        assert "PrivateKey = peer-private" in conf
        assert "Address = 10.66.0.2/32" in conf
        assert "DNS = 10.66.0.1" in conf
        assert "PublicKey = server-public" in conf
        assert "PresharedKey = psk" in conf
        assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf
        assert "Endpoint = vpn.example.net:51820" in conf
        assert "PersistentKeepalive = 25" in conf
        assert "MTU" not in conf

    def test_byo_key_gets_placeholder_and_mtu_when_set(self) -> None:
        server = SERVER.model_copy(update={"client_mtu": 1380, "persistent_keepalive": 0})
        conf = render_client_conf(_peer(private_key=None), server)
        assert PRIVATE_KEY_PLACEHOLDER in conf
        assert "MTU = 1380" in conf
        assert "PersistentKeepalive" not in conf

    def test_ipv6_endpoint_is_bracketed(self) -> None:
        conf = render_client_conf(_peer(), SERVER.model_copy(update={"endpoint_host": "2001:db8::1"}))
        assert "Endpoint = [2001:db8::1]:51820" in conf

    def test_filename_is_a_valid_interface_name(self) -> None:
        assert client_conf_filename("Living room TV") == "Living-room-TV.conf"
        assert client_conf_filename("a very long device name indeed") == "a-very-long-dev.conf"
        assert client_conf_filename("///") == "octoprox.conf"


class TestSubnet:
    def test_canonical_and_bounds(self) -> None:
        assert validate_subnet(" 10.66.0.0/16 ") == "10.66.0.0/16"
        with pytest.raises(ValueError):
            validate_subnet("10.66.0.1/16")  # host bits set
        with pytest.raises(ValueError):
            validate_subnet("10.0.0.0/31")
        with pytest.raises(ValueError):
            validate_subnet("127.0.0.0/8")

    def test_endpoint_host_forms(self) -> None:
        assert validate_endpoint_host(" vpn.example.net ") == "vpn.example.net"
        assert validate_endpoint_host("203.0.113.9") == "203.0.113.9"
        assert validate_endpoint_host("[2001:db8::1]") == "2001:db8::1"
        assert validate_endpoint_host("") == ""
        for bad in ("vpn.example.net:51820", "https://vpn.example.net", "a b"):
            with pytest.raises(ValueError):
                validate_endpoint_host(bad)

    def test_gateway_is_first_host(self) -> None:
        assert SERVER.gateway == "10.66.0.1"
        assert SERVER.model_copy(update={"subnet": "192.168.200.0/24"}).gateway == "192.168.200.1"


def test_parse_dump() -> None:
    text = (
        "srvpriv\tsrvpub\t51820\toff\n"
        "peerA\tpsk\t203.0.113.9:40000\t10.66.0.2/32\t1700000000\t1234\t5678\t25\n"
        "peerB\t(none)\t(none)\t10.66.0.3/32\t0\t0\t0\toff\n"
        "garbage line\n"
    )
    dumps = parse_dump(text)
    assert [d.public_key for d in dumps] == ["peerA", "peerB"]
    assert dumps[0].endpoint == "203.0.113.9:40000"
    assert dumps[0].latest_handshake == 1700000000 and dumps[0].rx_bytes == 1234 and dumps[0].tx_bytes == 5678
    assert dumps[1].endpoint is None and dumps[1].latest_handshake == 0
