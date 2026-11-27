# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What's New: the release notes this version ships, what the user has not seen yet, and for admins a newer release."""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.core.auth import CurrentUserDep
from api.models.release import ReleasesResponse
from api.routes.common import release_service_of

router = APIRouter(prefix="/releases")


@router.get("", response_model=ReleasesResponse)
async def get_releases(request: Request, user: CurrentUserDep) -> ReleasesResponse:
    """The release notes up to the running version, how many the user has not seen, and for admins a newer release."""
    return await release_service_of(request).notes(user)


@router.post("/seen", response_model=ReleasesResponse)
async def mark_releases_seen(request: Request, user: CurrentUserDep) -> ReleasesResponse:
    """Record that the user has seen the notes up to the running version."""
    return await release_service_of(request).mark_seen(user)
