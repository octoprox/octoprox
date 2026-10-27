# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Release notes: the ones the image ships (``docs/_data/releases.yml``), and the newest release octoprox.com lists.

The notes file is the one source for the changelog page, What's New in the app, the GitHub Release and the update
check; ``scripts/release.sh`` adds each release's entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from api.db.redis import LATEST_RELEASE_KEY

if TYPE_CHECKING:
    from api.db.redis import RedisClient

# The notes file: in the docs site's data, so the changelog page renders it, and copied into the image beside api/.
RELEASES_FILE = Path(__file__).resolve().parents[2] / "docs" / "_data" / "releases.yml"

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


_NOTES = TypeAdapter(list[ReleaseNote])


class ReleaseNotes:
    """Release notes, newest first."""

    def __init__(self, notes: Iterable[ReleaseNote]) -> None:
        self.notes = sorted(notes, key=lambda note: note.key, reverse=True)

    @classmethod
    def parse(cls, text: str) -> ReleaseNotes:
        """The notes of a notes file's YAML; ValueError (a ValidationError) when an entry is malformed."""
        return cls(_NOTES.validate_python(yaml.safe_load(text) or []))

    @classmethod
    @cache
    def bundled(cls) -> ReleaseNotes:
        """The notes this image ships; none when the file is missing."""
        if not RELEASES_FILE.exists():
            return cls([])
        return cls.parse(RELEASES_FILE.read_text())

    def through(self, version: str) -> list[ReleaseNote]:
        """The notes of ``version`` and the releases before it; all of them when the version is not a release."""
        key = version_key(version)
        return [note for note in self.notes if key is None or note.key <= key]

    def after(self, version: str | None) -> list[ReleaseNote]:
        """The notes of the releases newer than ``version``; none when the version is not a release."""
        key = version_key(version)
        return [] if key is None else [note for note in self.notes if note.key > key]

    def unseen(self, current: str, last_seen: str | None) -> int:
        """How many notes up to ``current`` are newer than the release a user last looked at.

        Someone who never looked has one: the newest, so What's New is pointed out once without listing all history.
        """
        notes = self.through(current)
        if version_key(last_seen) is None:
            return min(1, len(notes))
        return len(ReleaseNotes(notes).after(last_seen))


class LatestRelease(BaseModel):
    """What the last update check found: the notes octoprox.com lists, and when it looked."""

    checked_at: datetime
    notes: list[ReleaseNote]

    @property
    def version(self) -> str | None:
        """The newest release listed."""
        return self.notes[0].version if self.notes else None

    @classmethod
    def of(cls, checked_at: datetime, payload: object) -> LatestRelease:
        """A check's finding from the JSON octoprox.com serves, skipping entries this release cannot read; ValueError when it is not a list.

        Installs never update this code, so a later release's entry they cannot read must not hide the ones they can.
        """
        if not isinstance(payload, list):
            raise ValueError("The release list is not a JSON list")
        notes: list[ReleaseNote] = []
        for entry in payload:
            try:
                notes.append(ReleaseNote.model_validate(entry))
            except ValidationError:
                continue
        return cls(checked_at=checked_at, notes=ReleaseNotes(notes).notes)

    @classmethod
    async def load(cls, redis_client: RedisClient) -> LatestRelease | None:
        """The last check's finding; None before the first check succeeds, or when another release wrote it unreadably."""
        raw = await redis_client.client.get(LATEST_RELEASE_KEY)
        if raw is None:
            return None
        try:
            return cls.model_validate_json(raw)
        except ValidationError:
            return None

    async def save(self, redis_client: RedisClient) -> None:
        """Keep this finding for the api to read, in place of the one before."""
        await redis_client.client.set(LATEST_RELEASE_KEY, self.model_dump_json(by_alias=True))
