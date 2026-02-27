# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""TLS certificate manager for MITM interception.

Handles CA certificate generation/loading and per-domain certificate
generation for TLS MITM proxying.
"""

import os
import ssl
import tempfile
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = structlog.get_logger()

# CA certificate validity period
_CA_VALIDITY_DAYS = 3650  # ~10 years

# Domain certificate validity period
_DOMAIN_VALIDITY_DAYS = 365  # 1 year

# RSA key size for all generated keys
_RSA_KEY_SIZE = 2048

# Maximum number of cached domain SSL contexts
_MAX_CACHE_SIZE = 1000


class TLSCertManager:
    """Manages CA and per-domain certificates for TLS MITM interception."""

    def __init__(self, ca_cert_path: Path, ca_key_path: Path) -> None:
        self._ca_cert_path = ca_cert_path
        self._ca_key_path = ca_key_path
        self._ca_cert: x509.Certificate | None = None
        self._ca_key: rsa.RSAPrivateKey | None = None
        # Cache: hostname -> ssl.SSLContext (server-side), LRU via OrderedDict
        self._context_cache: OrderedDict[str, ssl.SSLContext] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def ca_cert_path(self) -> Path:
        """Path to the CA certificate file."""
        return self._ca_cert_path

    def initialize(self) -> None:
        """Load or generate the CA certificate.

        If CA cert/key files exist at the configured paths, loads them.
        Otherwise, generates a new self-signed CA and writes to disk.
        """
        if self._ca_cert_path.exists() and self._ca_key_path.exists():
            self._load_ca()
            fingerprint = self._ca_cert.fingerprint(hashes.SHA256()).hex()  # type: ignore[union-attr]
            logger.info(
                "Loaded existing MITM CA certificate",
                cert_path=str(self._ca_cert_path),
                fingerprint=fingerprint,
            )
        else:
            self._generate_ca()
            fingerprint = self._ca_cert.fingerprint(hashes.SHA256()).hex()  # type: ignore[union-attr]
            logger.info(
                "Generated new MITM CA certificate",
                cert_path=str(self._ca_cert_path),
                fingerprint=fingerprint,
            )

    def get_server_ssl_context(self, hostname: str) -> ssl.SSLContext:
        """Get an SSL context for serving TLS to the client for the given hostname.

        Generates a certificate for the hostname signed by the CA (if not cached),
        and returns an ssl.SSLContext configured for server-side TLS.
        """
        with self._cache_lock:
            cached = self._context_cache.get(hostname)
            if cached is not None:
                self._context_cache.move_to_end(hostname)
                return cached

        # Generate outside the lock (RSA keygen is slow)
        ctx = self._create_server_context(hostname)

        with self._cache_lock:
            # LRU eviction: remove least-recently-used entry
            if len(self._context_cache) >= _MAX_CACHE_SIZE:
                self._context_cache.popitem(last=False)
            self._context_cache[hostname] = ctx

        return ctx

    def _create_server_context(self, hostname: str) -> ssl.SSLContext:
        """Create a server-side SSL context with a generated cert for the hostname."""
        cert_pem, key_pem = self._generate_domain_cert(hostname)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

        # Load cert chain from in-memory PEM via temp files
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cert_f, \
             tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_f:
            cert_f.write(cert_pem)
            cert_f.flush()
            key_f.write(key_pem)
            key_f.flush()
            try:
                ctx.load_cert_chain(cert_f.name, key_f.name)
            finally:
                os.unlink(cert_f.name)
                os.unlink(key_f.name)

        # Force HTTP/1.1 — we parse requests as HTTP/1.1 on the client-facing side
        ctx.set_alpn_protocols(["http/1.1"])

        return ctx

    def _generate_ca(self) -> None:
        """Generate a self-signed CA certificate and write to disk."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Octoprox"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Octoprox MITM CA"),
        ])

        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=_CA_VALIDITY_DAYS))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        # Ensure parent directory exists
        self._ca_cert_path.parent.mkdir(parents=True, exist_ok=True)

        # Write certificate
        self._ca_cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
        )

        # Write private key with restricted permissions
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        self._ca_key_path.write_bytes(key_bytes)
        os.chmod(self._ca_key_path, 0o600)

        self._ca_cert = cert
        self._ca_key = key

    def _load_ca(self) -> None:
        """Load CA certificate and private key from disk."""
        cert_pem = self._ca_cert_path.read_bytes()
        key_pem = self._ca_key_path.read_bytes()

        self._ca_cert = x509.load_pem_x509_certificate(cert_pem)
        self._ca_key = serialization.load_pem_private_key(key_pem, password=None)  # type: ignore[assignment]

    def _generate_domain_cert(self, hostname: str) -> tuple[bytes, bytes]:
        """Generate a certificate for a specific domain, signed by the CA.

        Returns (cert_pem_bytes, key_pem_bytes).
        """
        assert self._ca_cert is not None
        assert self._ca_key is not None

        key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)

        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ]))
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=_DOMAIN_VALIDITY_DAYS))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(hostname)]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(self._ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

        return cert_pem, key_pem
