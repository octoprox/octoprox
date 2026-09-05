# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Session identifier generation for session-mode proxy types."""

from __future__ import annotations

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
    """Generates unpredictable session ids matching a :class:`SessionIdSpec`."""

    def __init__(self, spec: SessionIdSpec) -> None:
        self._spec = spec
        self._alphabet = _ALPHABETS[spec.alphabet]

    def generate(self) -> str:
        body = "".join(secrets.choice(self._alphabet) for _ in range(self._spec.length))
        if self._spec.alphabet == "digits" and body[0] == "0" and self._spec.length > 1:
            # Vendors that treat the id as an integer dislike leading zeros.
            body = secrets.choice(string.digits[1:]) + body[1:]
        return f"{self._spec.prefix}{body}"
