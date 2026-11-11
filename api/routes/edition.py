# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Which edition the install runs: public, since the login page asks before anyone has signed in.

The web app calls an extension's endpoints only when the edition names it, since the free image answers any other
``/api/v1`` GET with the web app's page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.core.auth import DbDep
from api.core.extension import Edition
from api.routes.common import extension_of

router = APIRouter()


@router.get("/edition", response_model=Edition)
async def get_edition(request: Request, session: DbDep) -> Edition:
    """The edition's name (``free`` on the public image) and the organisation it is licensed to, when there is one."""
    return await extension_of(request).edition(session)
