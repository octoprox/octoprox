# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What's New as the API answers it: the notes up to the running version, what the user has not seen, a newer release."""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_VERSION = re.compile(r"\d+\.\d+\.\d+")

type VersionKey = tuple[int, int, int]


def version_key(version: str | None) -> VersionKey | None:
    """``6.1.4`` as ``(6, 1, 4)`` for ordering; None for anything else, such as ``unknown`` from a source tree."""
    if version is None or not _VERSION.fullmatch(version):
        return None
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


class ReleaseChange(BaseModel):
    """One change, in the words of the people who use Octoprox, with the docs page that explains it."""

    text: str
    # A page on the docs site, such as ``wireguard`` or ``connecting#exit-headers``.
    docs: str | None = None


class ReleaseNote(BaseModel):
    """One release's notes, or one summary covering ``from`` to ``version`` for the releases before the notes began."""

    model_config = ConfigDict(populate_by_name=True)

    version: str
    from_version: str | None = Field(default=None, alias="from")
    date: date
    title: str
    changes: list[ReleaseChange]

    @field_validator("version", "from_version")
    @classmethod
    def _check_version(cls, v: str | None) -> str | None:
        if v is not None and version_key(v) is None:
            raise ValueError(f"Version {v!r} is not MAJOR.MINOR.PATCH")
        return v

    @property
    def key(self) -> VersionKey:
        """The version, for ordering."""
        key = version_key(self.version)
        assert key is not None
        return key


class ReleaseUpdate(BaseModel):
    """A newer release than the running one, as the last update check found it."""

    version: str
    checked_at: datetime
    # The releases newer than the running one, newest first.
    notes: list[ReleaseNote]


class ReleasesResponse(BaseModel):
    """The notes of the running version and those before it, and what the user has not seen yet."""

    current_version: str
    # The release whose What's New the user last opened; null until they do.
    last_seen_version: str | None
    # How many of ``notes`` are newer than ``last_seen_version``.
    unseen: int
    notes: list[ReleaseNote]
    # Admins only, while the update check is on and found a newer release.
    update: ReleaseUpdate | None = None
