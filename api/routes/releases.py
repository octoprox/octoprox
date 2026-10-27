# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What's New: the release notes this version ships, what the user has not seen yet, and for admins a newer release."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api import __version__
from api.core.auth import CurrentUserDep, DbDep
from api.core.releases import LatestRelease, ReleaseNote, ReleaseNotes
from api.db.repository import UserRepository
from api.models.user import UserRole
from api.routes.common import redis_client_of, settings_of

router = APIRouter(prefix="/releases")


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


async def _releases(request: Request, session: DbDep, user: CurrentUserDep) -> ReleasesResponse:
    db_user = await UserRepository(session).get_by_id(user.id)
    last_seen = db_user.last_seen_release if db_user else None
    notes = ReleaseNotes.bundled()
    update = None
    if user.role == UserRole.ADMIN and settings_of(request).update_check:
        latest = await LatestRelease.load(redis_client_of(request))
        newer = ReleaseNotes(latest.notes).after(__version__) if latest else []
        if latest and newer:
            update = ReleaseUpdate(version=newer[0].version, checked_at=latest.checked_at, notes=newer)
    return ReleasesResponse(
        current_version=__version__,
        last_seen_version=last_seen,
        unseen=notes.unseen(__version__, last_seen),
        notes=notes.through(__version__),
        update=update,
    )


@router.get("", response_model=ReleasesResponse)
async def get_releases(request: Request, session: DbDep, user: CurrentUserDep) -> ReleasesResponse:
    """The release notes up to the running version, how many the user has not seen, and for admins a newer release."""
    return await _releases(request, session, user)


@router.post("/seen", response_model=ReleasesResponse)
async def mark_releases_seen(request: Request, session: DbDep, user: CurrentUserDep) -> ReleasesResponse:
    """Record that the user has seen the notes up to the running version."""
    await UserRepository(session).mark_release_seen(user.id, __version__)
    return await _releases(request, session, user)
