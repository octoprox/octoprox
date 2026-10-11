# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Personal access token secrets: how one is minted, hashed and recognised.

A token is ``octo_`` followed by 43 URL-safe characters (32 random bytes).
The prefix lets the auth dependency tell a token from a JWT without trying
to decode it, and lets secret scanners match the format. Only the SHA-256
of the whole string is stored; a lookup hashes what the client sent and
compares, so a database dump does not hand out working tokens.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "octo_"
# What a token list shows of each token: enough to tell them apart, not
# enough to use. The prefix plus the first 7 random characters.
DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 7


def generate_token() -> tuple[str, str, str]:
    """Mint a token. Returns ``(secret, hash, display_prefix)``."""
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return secret, hash_token(secret), secret[:DISPLAY_PREFIX_LENGTH]


def hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def looks_like_token(bearer: str) -> bool:
    """Whether a bearer value is a personal access token rather than a JWT."""
    return bearer.startswith(TOKEN_PREFIX)
