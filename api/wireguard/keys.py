# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard keys: Curve25519 pairs and preshared keys in WireGuard's base64 form.

Generated here rather than with ``wg genkey`` so the management API works on
an instance that has no WireGuard tools at all (a control-plane node).
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

KEY_BYTES = 32


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode_key(value: str) -> bytes:
    """The 32 raw bytes of a WireGuard key, or ValueError."""
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not a base64 WireGuard key") from None
    if len(raw) != KEY_BYTES:
        raise ValueError("a WireGuard key is 32 bytes (44 base64 characters)")
    return raw


def is_valid_key(value: str) -> bool:
    try:
        decode_key(value)
    except ValueError:
        return False
    return True


def generate_private_key() -> str:
    private = X25519PrivateKey.generate()
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _encode(raw)


def public_key_of(private_key: str) -> str:
    private = X25519PrivateKey.from_private_bytes(decode_key(private_key))
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return _encode(raw)


def generate_keypair() -> tuple[str, str]:
    """``(private_key, public_key)``."""
    private = generate_private_key()
    return private, public_key_of(private)


def generate_preshared_key() -> str:
    return _encode(os.urandom(KEY_BYTES))
