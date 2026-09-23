# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Session identifier generation for session-mode proxy types."""

from __future__ import annotations

import hashlib
import math
import secrets
import string

from api.providers.sdk.descriptor import SessionIdSpec

_ALPHABETS = {
    "lower_digits": string.ascii_lowercase + string.digits,
    "digits": string.digits,
    "alnum": string.ascii_letters + string.digits,
    "lower": string.ascii_lowercase,
}


class SessionIdGenerator:
    """Produces session ids matching a :class:`SessionIdSpec`.

    ``generate`` mints an unpredictable id for a provisioned slot or a
    rotating request. ``derive`` maps a caller-supplied seed onto the same
    alphabet and length deterministically, so a client's ``-sessid-`` value
    reaches the vendor as the same session id on every request and every
    instance without any shared state.
    """

    def __init__(self, spec: SessionIdSpec) -> None:
        self._spec = spec
        self._alphabet = _ALPHABETS[spec.alphabet]

    def generate(self) -> str:
        body = "".join(secrets.choice(self._alphabet) for _ in range(self._spec.length))
        return self._finish(body)

    def derive(self, seed: str) -> str:
        """Deterministic id for ``seed``: same seed, same id; the seed itself is not recoverable."""
        # Draw enough bytes to fill every position, so long ids never end in fixed padding.
        base = len(self._alphabet)
        needed = math.ceil(self._spec.length * math.log2(base) / 8) + 8
        number = int.from_bytes(hashlib.shake_256(seed.encode("utf-8")).digest(needed), "big")
        chars: list[str] = []
        for _ in range(self._spec.length):
            number, remainder = divmod(number, base)
            chars.append(self._alphabet[remainder])
        return self._finish("".join(chars))

    def _finish(self, body: str) -> str:
        if self._spec.alphabet == "digits" and body[0] == "0" and self._spec.length > 1:
            # Vendors that treat the id as an integer dislike leading zeros.
            # Deterministic for derive: the replacement digit comes from the body.
            body = string.digits[1:][sum(map(ord, body)) % 9] + body[1:]
        return f"{self._spec.prefix}{body}"
