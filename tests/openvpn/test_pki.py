# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install's CA, the certificates it issues and the tls-crypt key."""

from datetime import datetime

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from api.openvpn import pki


def test_ca_signs_server_and_client_certificates() -> None:
    ca_cert, ca_key = pki.generate_ca()
    ca = pki.load_certificate(ca_cert)
    assert ca.subject == ca.issuer
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True

    server_cert, server_key = pki.issue_server_certificate(ca_cert, ca_key)
    client_cert, client_key = pki.issue_client_certificate(ca_cert, ca_key, "device-1")
    assert pki.is_issued_by(server_cert, ca_cert) and pki.is_issued_by(client_cert, ca_cert)
    assert not pki.is_issued_by(ca_cert, client_cert)
    assert "BEGIN PRIVATE KEY" in server_key and "BEGIN PRIVATE KEY" in client_key

    server = pki.load_certificate(server_cert)
    client = pki.load_certificate(client_cert)
    assert ExtendedKeyUsageOID.SERVER_AUTH in server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert pki.common_name_of(client_cert) == "device-1"
    assert pki.common_name_of(server_cert) == pki.SERVER_COMMON_NAME

    # The serial is what the daemon reports (tls_serial_0, decimal) and is unique per issue.
    assert pki.serial_of(client_cert) == str(client.serial_number)
    other_cert, _ = pki.issue_client_certificate(ca_cert, ca_key, "device-1")
    assert pki.serial_of(other_cert) != pki.serial_of(client_cert)

    assert len(pki.fingerprint_of(ca_cert).split(":")) == 32
    expires = pki.not_after_of(client_cert)
    assert isinstance(expires, datetime) and expires.tzinfo is None
    assert (expires - datetime.utcnow()).days > 3600  # noqa: DTZ003


def test_another_ca_does_not_vouch() -> None:
    ca_cert, ca_key = pki.generate_ca()
    other_ca, _ = pki.generate_ca()
    client_cert, _ = pki.issue_client_certificate(ca_cert, ca_key, "x")
    assert not pki.is_issued_by(client_cert, other_ca)


def test_tls_crypt_key_is_in_openvpn_static_key_format() -> None:
    key = pki.generate_tls_crypt_key()
    assert pki.is_tls_crypt_key(key)
    lines = key.strip().splitlines()
    assert lines[0] == "#" and lines[3] == "-----BEGIN OpenVPN Static key V1-----"
    body = lines[4:-1]
    assert len(body) == 16 and all(len(line) == 32 for line in body)
    assert bytes.fromhex("".join(body)) != bytes(256)
    assert pki.generate_tls_crypt_key() != key
    assert not pki.is_tls_crypt_key("nope")
