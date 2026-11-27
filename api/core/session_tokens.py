# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Session JWTs: signed with the install's secret, carrying who signed in and their role until they expire."""

from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt  # type: ignore[import-untyped]

ALGORITHM = "HS256"


def create_jwt(payload: dict[str, Any], secret: str, expiry_hours: int) -> str:
    """Create a JWT token."""
    to_encode = payload.copy()
    expire = datetime.now(UTC) + timedelta(hours=expiry_hours)
    to_encode.update({"exp": expire})
    encoded: str = jwt.encode(to_encode, secret, algorithm=ALGORITHM)
    return encoded


def verify_jwt(token: str, secret: str) -> dict[str, Any] | None:
    """Verify a JWT token and return the payload."""
    try:
        payload: dict[str, Any] = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None
