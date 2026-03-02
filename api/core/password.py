# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Password hashing utilities using bcrypt."""

import bcrypt


def hash_password(password: str) -> str:
    """Hash a plain text password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against a bcrypt hash."""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
