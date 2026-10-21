# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's internal endpoints under ``/internal/v1``: the IP database files gateways fetch, which the feed only lists.

Every route requires the internal token derived from ``internal_secret``, compared in constant time; gateways hold it
instead of ``jwt_secret``, and no user signs in here.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from api.feed.codec import internal_token
from api.routes.common import geo_database_store_of, settings_of

# The authorization scheme gateways send the token with.
_BEARER = "bearer"
# What a database file's checksum looks like: sha256, lowercase hex, as the
# rows store it and the feed carries it.
_SHA256_PATTERN = "^[0-9a-f]{64}$"


async def require_internal_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """401 unless the request carries ``Authorization: Bearer <internal token>``."""
    scheme, _, presented = (authorization or "").partition(" ")
    expected = internal_token(settings_of(request).internal_secret)
    if scheme.lower() != _BEARER or not hmac.compare_digest(presented.strip().encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail="Internal token required", headers={"WWW-Authenticate": "Bearer"}
        )


router = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_internal_token)])


@router.get("/geo/databases/{database_id}/blob", response_class=FileResponse)
async def geo_database_blob(
    request: Request,
    database_id: str,
    sha256: Annotated[str, Query(pattern=_SHA256_PATTERN)],
) -> FileResponse:
    """The file of a stored IP database at ``sha256``, streamed; 404 when neither the copy here nor Postgres has it.

    A database just written through another api process may not be listed here yet: one sync of the list settles that first.
    """
    store = geo_database_store_of(request)
    path = await store.blob_file(database_id, sha256)
    if path is None:
        await store.sync_all()
        path = await store.blob_file(database_id, sha256)
    if path is None:
        raise HTTPException(status_code=404, detail="No IP database file at that checksum")
    return FileResponse(path, media_type="application/octet-stream")
