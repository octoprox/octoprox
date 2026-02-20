# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Core modules for Octoprox."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return current UTC time as a naive datetime.

    This avoids the deprecated datetime.utcnow() while remaining compatible
    with database columns that use TIMESTAMP WITHOUT TIME ZONE.
    """
    return datetime.now(UTC).replace(tzinfo=None)
