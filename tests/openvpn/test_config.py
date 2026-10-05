# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the daemon and the devices are told."""

from datetime import datetime

import pytest

from api.models.openvpn import (
    OpenVpnPeer,
    OpenVpnServerSettings,
    OpenVpnServerSettingsDoc,
    validate_subnet,
)
from api.openvpn.config import client_profile_filename, render_client_profile, render_server_conf

IDENTITY = {
    "ca_cert": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
    "ca_key": "-----BEGIN PRIVATE KEY-----\nCAKEY\n-----END PRIVATE KEY-----\n",
    "server_cert": "-----BEGIN CERTIFICATE-----\nSERVER\n-----END CERTIFICATE-----\n",
    "server_key": "-----BEGIN PRIVATE KEY-----\nSERVERKEY\n-----END PRIVATE KEY-----\n",
    "tls_crypt_key": "-----BEGIN OpenVPN Static key V1-----\nabc\n-----END OpenVPN Static key V1-----\n",
}
SERVER = OpenVpnServerSettings(**IDENTITY, endpoint_host="vpn.example.net")
PEER = OpenVpnPeer(
    project_id="p1", name="Living room TV", certificate="-----BEGIN CERTIFICATE-----\nPEER\n-----END CERTIFICATE-----",
    private_key="-----BEGIN PRIVATE KEY-----\nPEERKEY\n-----END PRIVATE KEY-----", serial="1234", address="10.67.0.2",
    certificate_expires_at=datetime(2036, 1, 1),
)


class TestServerConf:
    def test_udp_server_with_management_auth(self) -> None:
        conf = render_server_conf(SERVER, interface="ovpn0", port=1194, mtu=1500, management_socket="/run/m.sock")
        assert "server 10.67.0.0 255.255.0.0\n" in conf
        assert "topology subnet\n" in conf
        assert "proto udp\n" in conf and "port 1194\n" in conf
        assert "dev ovpn0\n" in conf and "dev-type tun\n" in conf and "tun-mtu 1500\n" in conf
        assert 'push "redirect-gateway def1"\n' in conf
        assert 'push "dhcp-option DNS 10.67.0.1"\n' in conf
        assert "keepalive 10 60\n" in conf
        assert "management /run/m.sock unix\nmanagement-client-auth\n" in conf
        # Without this the daemon refuses every certificate-only client ("Auth Username/Password was not provided").
        assert "auth-user-pass-optional\n" in conf
        assert "remote-cert-tls client\n" in conf and "dh none\n" in conf
        assert "data-ciphers-fallback AES-256-GCM\n" in conf
        assert "<ca>\n-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n</ca>" in conf
        assert "<key>\n-----BEGIN PRIVATE KEY-----\nSERVERKEY" in conf
        assert "<tls-crypt>\n-----BEGIN OpenVPN Static key V1-----" in conf
        # The server never hands out pool addresses by itself; nothing about client-config-dir either.
        assert "client-config-dir" not in conf and "ifconfig-pool-persist" not in conf

    def test_tcp_and_overrides(self) -> None:
        tcp = SERVER.model_copy(update={"protocol": "tcp", "subnet": "10.77.0.0/24", "keepalive_interval": 5, "keepalive_timeout": 30})
        conf = render_server_conf(tcp, interface="tun9", port=443, mtu=1400, management_socket="/x")
        assert "proto tcp-server\n" in conf and "port 443\n" in conf
        assert "server 10.77.0.0 255.255.255.0\n" in conf and 'push "dhcp-option DNS 10.77.0.1"' in conf
        assert "keepalive 5 30\n" in conf and "tun-mtu 1400\n" in conf


class TestClientProfile:
    def test_profile_carries_everything_inline(self) -> None:
        profile = render_client_profile(PEER, SERVER)
        assert profile.startswith("# Octoprox device: Living room TV\nclient\n")
        assert "proto udp\nremote vpn.example.net 1194\n" in profile
        assert "remote-cert-tls server\n" in profile
        assert "<ca>\n-----BEGIN CERTIFICATE-----\nCA\n" in profile
        assert "<cert>\n-----BEGIN CERTIFICATE-----\nPEER\n" in profile
        assert "<key>\n-----BEGIN PRIVATE KEY-----\nPEERKEY\n" in profile
        assert "<tls-crypt>\n" in profile
        assert "tun-mtu" not in profile
        # Router firmware often runs OpenVPN 2.4, which refuses a profile with 2.5 directives.
        assert "data-ciphers" not in profile
        # Nothing of the server's private material leaks into a device profile.
        assert "SERVERKEY" not in profile and "CAKEY" not in profile

    def test_unset_endpoint_and_mtu(self) -> None:
        server = SERVER.model_copy(update={"endpoint_host": "", "client_mtu": 1380, "protocol": "tcp"})
        profile = render_client_profile(PEER, server)
        assert "remote <set the public endpoint in Settings, OpenVPN> 1194" in profile
        assert "proto tcp\n" in profile and "tun-mtu 1380\n" in profile

    def test_filename(self) -> None:
        assert client_profile_filename("Living room TV") == "Living-room-TV.ovpn"
        assert client_profile_filename("///") == "octoprox.ovpn"
        assert client_profile_filename("a" * 100).startswith("a" * 64 + ".ovpn")


class TestSettings:
    def test_subnet_and_keepalive(self) -> None:
        assert validate_subnet(" 10.67.0.0/16 ") == "10.67.0.0/16"
        with pytest.raises(ValueError):
            validate_subnet("10.67.0.1/16")
        with pytest.raises(ValueError):
            OpenVpnServerSettingsDoc(keepalive_interval=60, keepalive_timeout=60)
        with pytest.raises(ValueError):
            OpenVpnServerSettingsDoc(protocol="sctp")  # type: ignore[arg-type]
        assert SERVER.daemon_signature() != SERVER.model_copy(update={"protocol": "tcp"}).daemon_signature()
        assert SERVER.daemon_signature() == SERVER.model_copy(update={"endpoint_host": "other"}).daemon_signature()
