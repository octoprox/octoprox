# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The OpenVPN identity: a private CA, the server certificate it signs, one client certificate per device.

OpenVPN authenticates with X.509, so where WireGuard has a key pair the
install has a CA. It is made here with ``cryptography`` rather than easy-rsa
so the management API works on an instance that has no OpenVPN at all, and
kept in Postgres like the WireGuard key pair so every instance presents the
same identity. The CA is private to the install and separate from the MITM
CA: a device trusts it for the tunnel endpoint only. A device's certificate
carries its id as common name; what a connecting device is allowed to do is
decided against the peer directory at connect time, not by the certificate,
so there is no revocation list: a removed or rotated certificate is simply
no longer the one on record.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_COMMON_NAME = "Octoprox OpenVPN CA"
SERVER_COMMON_NAME = "octoprox-openvpn"
# Devices are not re-enrolled on a schedule; a certificate lasts as long as
# the device's registration, which the directory ends, not the clock.
VALIDITY = timedelta(days=10 * 365)
# Back-dated so a clock a little behind ours still accepts a fresh certificate.
_BACKDATE = timedelta(minutes=5)
TLS_CRYPT_KEY_BYTES = 256


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def load_certificate(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode("ascii"))


def load_private_key(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("expected an EC private key")
    return key


def generate_ca() -> tuple[str, str]:
    """A new self-signed CA: ``(cert_pem, key_pem)``."""
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return _cert_pem(cert), _key_pem(key)


def _issue(ca_cert_pem: str, ca_key_pem: str, common_name: str, usage: x509.ObjectIdentifier) -> tuple[str, str]:
    ca_cert = load_certificate(ca_cert_pem)
    ca_key = load_private_key(ca_key_pem)
    key = _new_key()
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=True, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False)  # type: ignore[arg-type]
        .sign(ca_key, hashes.SHA256())
    )
    return _cert_pem(cert), _key_pem(key)


def issue_server_certificate(ca_cert_pem: str, ca_key_pem: str) -> tuple[str, str]:
    """The certificate the daemon presents; devices check it is a server certificate of the CA."""
    return _issue(ca_cert_pem, ca_key_pem, SERVER_COMMON_NAME, ExtendedKeyUsageOID.SERVER_AUTH)


def issue_client_certificate(ca_cert_pem: str, ca_key_pem: str, common_name: str) -> tuple[str, str]:
    """A device's certificate, its id as common name; the daemon reports both at connect time."""
    return _issue(ca_cert_pem, ca_key_pem, common_name, ExtendedKeyUsageOID.CLIENT_AUTH)


def serial_of(cert_pem: str) -> str:
    """The serial number in decimal, which is how the daemon reports a connecting certificate (``tls_serial_0``)."""
    return str(load_certificate(cert_pem).serial_number)


def fingerprint_of(cert_pem: str) -> str:
    """SHA-256 fingerprint, colon-separated upper-case hex, as every TLS tool prints it."""
    digest = load_certificate(cert_pem).fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def not_after_of(cert_pem: str) -> datetime:
    """Expiry as naive UTC, like every other timestamp the API serialises."""
    return load_certificate(cert_pem).not_valid_after_utc.replace(tzinfo=None)


def common_name_of(cert_pem: str) -> str:
    attrs = load_certificate(cert_pem).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else ""


def is_issued_by(cert_pem: str, ca_cert_pem: str) -> bool:
    """Whether ``cert_pem`` was signed by the CA (signature, not just the issuer name)."""
    cert = load_certificate(cert_pem)
    ca = load_certificate(ca_cert_pem)
    try:
        cert.verify_directly_issued_by(ca)
    except Exception:
        return False
    return True


def generate_tls_crypt_key() -> str:
    """A 2048-bit OpenVPN static key, in the file format ``--tls-crypt`` reads (inline or from a file).

    tls-crypt encrypts and authenticates the control channel with it, so a
    scanner finds no TLS handshake to fingerprint and a port scan gets no
    answer; every device config carries it.
    """
    raw = os.urandom(TLS_CRYPT_KEY_BYTES).hex()
    lines = [raw[i : i + 32] for i in range(0, len(raw), 32)]
    return "\n".join(
        [
            "#",
            "# 2048 bit OpenVPN static key",
            "#",
            "-----BEGIN OpenVPN Static key V1-----",
            *lines,
            "-----END OpenVPN Static key V1-----",
        ]
    ) + "\n"


def is_tls_crypt_key(value: str) -> bool:
    return "-----BEGIN OpenVPN Static key V1-----" in value and "-----END OpenVPN Static key V1-----" in value
