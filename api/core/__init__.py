# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Core modules for Octoprox."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return current UTC time as a naive datetime.

    This avoids the deprecated datetime.utcnow() while remaining compatible
    with database columns that use TIMESTAMP WITHOUT TIME ZONE.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def as_naive_utc(value: datetime | None) -> datetime | None:
    """A time a client sent, as the tables store times: UTC without an offset. One without an offset already is."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
