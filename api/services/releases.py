# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What's New: the release notes this version ships, what the user has not seen yet, and for admins a newer release."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api import __version__
from api.core.releases import LatestRelease, ReleaseNotes
from api.db.repository import UserRepository
from api.models.release import ReleasesResponse, ReleaseUpdate
from api.models.user import UserRole

if TYPE_CHECKING:
    from api.core.auth import CurrentUser
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory


class ReleaseService:
    """Reads the bundled notes against what the user last saw, and records that they saw the running version's.

    Args:
        update_check: whether the workers check octoprox.com for a newer release, which admins are then told of.
    """

    def __init__(
        self, session_factory: SessionFactory, redis_client: RedisClient, *, update_check: bool
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._update_check = update_check

    async def notes(self, caller: CurrentUser) -> ReleasesResponse:
        """The release notes up to the running version, how many the caller has not seen, and for admins a newer release."""
        async with self._session_factory() as session:
            db_user = await UserRepository(session).get_by_id(caller.id)
        return await self._releases(caller, db_user.last_seen_release if db_user else None)

    async def mark_seen(self, caller: CurrentUser) -> ReleasesResponse:
        """Record that the caller has seen the notes up to the running version; the notes as :meth:`notes` reads them."""
        async with self._session_factory() as session:
            users = UserRepository(session)
            await users.mark_release_seen(caller.id, __version__)
            db_user = await users.get_by_id(caller.id)
            await session.commit()
        return await self._releases(caller, db_user.last_seen_release if db_user else None)

    async def _releases(self, caller: CurrentUser, last_seen: str | None) -> ReleasesResponse:
        """The bundled notes against ``last_seen``, and for an admin the newer release the update check found."""
        notes = ReleaseNotes.bundled()
        update = None
        if caller.role == UserRole.ADMIN and self._update_check:
            latest = await LatestRelease.load(self._redis_client)
            newer = ReleaseNotes(latest.notes).after(__version__) if latest else []
            if latest and newer:
                update = ReleaseUpdate(
                    version=newer[0].version, checked_at=latest.checked_at, notes=newer
                )
        return ReleasesResponse(
            current_version=__version__,
            last_seen_version=last_seen,
            unseen=notes.unseen(__version__, last_seen),
            notes=notes.through(__version__),
            update=update,
        )
