# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing feed's wire format: compressed, encrypted, and digested so a follower can trust it.

Each section is sealed with AES-256-GCM and listed in the manifest under an
HMAC of its JSON, both keyed from ``internal_secret``, which the api, the
gateways and the controllers share: access to Redis alone reveals no section
and forges none, and a follower holding another secret applies nothing and
keeps its snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import zlib
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ValidationError

# HKDF labels, one per key drawn from internal_secret, so the cipher key, the
# digest key and the internal token never coincide. The "v1" is part of the
# label: changing a derivation means a new label, not a new meaning for this one.
_FEED_KEY_INFO = b"octoprox-feed-v1"
_DIGEST_KEY_INFO = b"octoprox-feed-digest-v1"
_INTERNAL_TOKEN_INFO = b"octoprox-internal-token-v1"
# Every derived key is 256 bits: AES-256 and HMAC-SHA256 both take that much.
_KEY_BYTES = 32
# AES-GCM's standard 96-bit nonce, random per encode. A section is encoded only
# when it changed, so one secret seals far fewer than the 2**32 payloads after
# which random nonces start to risk a repeat.
_NONCE_BYTES = 12


class FeedDecodeError(ValueError):
    """A section or manifest that cannot be trusted: not sealed with this secret, altered, or unreadable."""


def _derive(internal_secret: str, info: bytes) -> bytes:
    """A key of its own for ``info``, from ``internal_secret``."""
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=info).derive(internal_secret.encode())


def internal_token(internal_secret: str) -> str:
    """The bearer token gateways present to the api's internal endpoints, as hex.

    Derived rather than configured so the roles share one secret; the api
    compares it with :func:`hmac.compare_digest`.
    """
    return _derive(internal_secret, _INTERNAL_TOKEN_INFO).hex()


class FeedCodec:
    """Seals sections for Redis and opens them again, keyed by ``internal_secret``."""

    def __init__(self, internal_secret: str) -> None:
        self._cipher = AESGCM(_derive(internal_secret, _FEED_KEY_INFO))
        self._digest_key = _derive(internal_secret, _DIGEST_KEY_INFO)

    def digest(self, section_json: bytes) -> str:
        """The section's manifest entry: HMAC-SHA256 of its JSON, as hex.

        Equal content gives an equal digest only when it serialises the same,
        which is why the compiler builds sections in a canonical order.
        """
        return hmac.new(self._digest_key, section_json, hashlib.sha256).hexdigest()

    def manifest_digest(self, manifest: Mapping[str, str]) -> str:
        """The head's digest: HMAC over the manifest's ``(section, digest)`` pairs in name order."""
        return self.digest(json.dumps(sorted(manifest.items()), separators=(",", ":")).encode())

    def encode(self, section: BaseModel) -> tuple[str, str]:
        """``(digest, payload)`` for one section: its manifest entry and what the sections hash stores."""
        return self.seal(section.model_dump_json().encode())

    def seal(self, section_json: bytes) -> tuple[str, str]:
        """``(digest, payload)`` for a section already serialised, as :meth:`encode` gives them."""
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._cipher.encrypt(nonce, zlib.compress(section_json), None)
        return self.digest(section_json), base64.b64encode(nonce + sealed).decode("ascii")

    def decode[M: BaseModel](self, payload: str, model_type: type[M], expected_digest: str) -> M:
        """The section a payload holds, checked against its manifest entry.

        Raises :class:`FeedDecodeError` when the payload was not sealed with
        this secret or was altered, when its JSON is not what the manifest
        lists (a section rewritten since the manifest was read), or when it
        does not parse as ``model_type``.
        """
        try:
            raw = base64.b64decode(payload, validate=True)
            sealed = self._cipher.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)
            section_json = zlib.decompress(sealed)
        except (InvalidTag, ValueError, zlib.error) as exc:
            # Bad base64 and a payload too short for its nonce are ValueErrors;
            # another secret or an altered payload fails the tag.
            raise FeedDecodeError(f"Feed payload cannot be opened ({type(exc).__name__})") from None
        if not hmac.compare_digest(self.digest(section_json), expected_digest):
            raise FeedDecodeError("Feed payload does not match its manifest entry")
        try:
            return model_type.model_validate_json(section_json)
        except ValidationError as exc:
            # The count only: pydantic's message quotes the input, credentials included.
            raise FeedDecodeError(
                f"Feed section does not parse as {model_type.__name__} ({exc.error_count()} errors)"
            ) from None
