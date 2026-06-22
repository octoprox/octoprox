# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""TLS certificate manager for MITM interception.

Handles CA certificate generation/loading and per-domain certificate
generation for TLS MITM proxying.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import tempfile
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from api.core.fileio import atomic_write

if TYPE_CHECKING:
    from api.db.redis import RedisClient

logger = structlog.get_logger()

# CA certificate validity period
_CA_VALIDITY_DAYS = 3650  # ~10 years

# Domain certificate validity period
_DOMAIN_VALIDITY_DAYS = 365  # 1 year

# RSA key size for all generated keys
_RSA_KEY_SIZE = 2048

# Maximum number of cached domain SSL contexts
_MAX_CACHE_SIZE = 1000

# CA bootstrap coordination (clustered startup).
#
# All instances share one CA volume, so on a fresh volume they would
# otherwise race to each generate their own CA. Generation is gated behind a
# short Redis lock (reusing the `lease:` keyspace) so exactly one instance
# generates while its peers wait for the result and load it.
_CA_BOOTSTRAP_LEASE_NAME = "ca-bootstrap"
_CA_BOOTSTRAP_LOCK_TTL_MS = 30_000  # generation takes <1s; ample headroom
_CA_BOOTSTRAP_TIMEOUT_S = 60.0  # how long a peer waits for the CA to appear
_CA_BOOTSTRAP_POLL_S = 0.25

# Lua: DEL the lock iff we still own it, so a slow generator whose lock has
# already expired and been re-taken by a peer cannot delete the peer's lock.
_CA_LOCK_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


# ---------------------------------------------------------------------------
# ClientHello capture via _msg_callback
# ---------------------------------------------------------------------------

# Stores raw ClientHello bytes + record-layer version per TLS connection,
# keyed by id(SSLObject).  Populated by _tls_msg_callback during start_tls()
# and consumed by pop_client_hello() in the MITM handler immediately after.
_pending_client_hellos: dict[int, tuple[bytes, int]] = {}
_hello_lock = threading.Lock()


def _tls_msg_callback(
    conn: object,
    direction: str,
    version: object,
    content_type: int,
    msg_type: int,
    data: bytes,
) -> None:
    """OpenSSL message callback that captures ClientHello during TLS handshake."""
    # content_type 22 = Handshake, msg_type 1 = ClientHello
    if direction == "read" and content_type == 22 and msg_type == 1:
        with _hello_lock:
            _pending_client_hellos[id(conn)] = (bytes(data), int(version))  # type: ignore[call-overload]


def pop_client_hello(ssl_object: object) -> tuple[bytes, int] | None:
    """Retrieve and remove captured ClientHello bytes for an SSL connection.

    Returns ``(handshake_bytes, record_layer_version)`` or ``None``.

    Must be called after ``start_tls()`` completes, passing the ``ssl_object``
    obtained from ``transport.get_extra_info("ssl_object")``.
    """
    with _hello_lock:
        return _pending_client_hellos.pop(id(ssl_object), None)


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

    async def bootstrap(
        self,
        redis_client: RedisClient,
        instance_id: str,
        *,
        timeout_s: float = _CA_BOOTSTRAP_TIMEOUT_S,
    ) -> None:
        """Load the CA, generating it once if the shared volume is empty.

        Every instance shares one CA volume so that the leaf certs minted by
        any instance validate against the same CA, regardless of which
        backend HAProxy routes a given client to. On a fresh volume this
        ensures exactly one instance generates the CA:

        * Fast path — if the CA already exists, just load it.
        * Otherwise acquire a short Redis lock; the winner generates and
          writes the CA atomically while peers poll until it appears, then
          load it.

        Coordination is mandatory: a Redis error is treated as "didn't get
        the lock, retry" (never as licence to generate locally, which would
        let every instance mint its own CA). A transient blip self-heals on
        the next poll; a lasting outage surfaces as a ``TimeoutError`` so
        startup fails loudly rather than producing divergent CAs.
        """
        if self._load_if_present():
            return

        from api.db.redis import LEASE_KEY

        lock_key = LEASE_KEY.format(name=_CA_BOOTSTRAP_LEASE_NAME)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        while True:
            if self._load_if_present():
                return
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for the MITM CA after {timeout_s:.0f}s"
                )

            # Become the sole generator, or stand by while a peer generates.
            try:
                acquired = await redis_client.client.set(
                    lock_key, instance_id, nx=True, px=_CA_BOOTSTRAP_LOCK_TTL_MS
                )
            except Exception:
                logger.warning(
                    "CA bootstrap: Redis unavailable, retrying", exc_info=True
                )
                acquired = None

            if acquired:
                try:
                    # Re-check under the lock: a peer may have generated the
                    # CA between our existence check and acquiring the lock.
                    if not self._load_if_present():
                        self._generate_ca()
                        self._log_ca("Generated new")
                finally:
                    with contextlib.suppress(Exception):
                        await redis_client.client.eval(  # type: ignore[misc]
                            _CA_LOCK_RELEASE, 1, lock_key, instance_id
                        )
                return

            # A peer holds the lock (or Redis blipped). Wait, then retry.
            await asyncio.sleep(_CA_BOOTSTRAP_POLL_S)

    def _load_if_present(self) -> bool:
        """Load the CA if both files exist. Returns True if loaded."""
        if self._ca_cert_path.exists() and self._ca_key_path.exists():
            self._load_ca()
            self._log_ca("Loaded existing")
            return True
        return False

    def _log_ca(self, action: str) -> None:
        fingerprint = self._ca_cert.fingerprint(hashes.SHA256()).hex()  # type: ignore[union-attr]
        logger.info(
            f"{action} MITM CA certificate",
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

        # advertise HTTP/2 and HTTP/1.1 -- handler picks code path based on negotiated protocol
        ctx.set_alpn_protocols(["h2", "http/1.1"])

        # Capture raw ClientHello bytes during the TLS handshake.
        # _msg_callback is an internal CPython API (stable since 3.8) that hooks
        # into OpenSSL's SSL_CTX_set_msg_callback.
        ctx._msg_callback = _tls_msg_callback  # type: ignore[attr-defined]

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

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

        # Ensure parent directory exists, then write both files atomically.
        # Write the key (0600) first and the cert last: a peer watching the
        # shared volume gates on "both files present", so publishing the cert
        # last guarantees the key is already fully in place. os.replace makes
        # each file appear in a single step, so a reader never observes a
        # partial or cert/key-mismatched CA.
        self._ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._ca_key_path, key_bytes, mode=0o600)
        atomic_write(self._ca_cert_path, cert_pem, mode=0o644)

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
