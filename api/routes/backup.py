# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Admin backup routes: export and import a full Octoprox setup as a passphrase-encrypted file.

An import replaces every row but the install's settings and audit trail in one transaction, which also records the
import, appends one ``all`` change entry and deletes the keys only the removed projects used; the key derivation,
compression and encryption run off the event loop.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, UploadFile

from api.core.auth import RequireAdminDep
from api.models.backup import ExportRequest, ImportSummary
from api.routes.common import actor_of, backup_service_of

router = APIRouter(prefix="/backup")


@router.post("/export")
async def export_backup(request: Request, data: ExportRequest, admin: RequireAdminDep) -> Response:
    """Export the full setup as a downloadable, passphrase-encrypted file."""
    backup = await backup_service_of(request).export(data, actor_of(request, admin))
    return Response(
        content=backup.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{backup.filename}"'},
    )


@router.post("/import", response_model=ImportSummary)
async def import_backup(
    request: Request,
    admin: RequireAdminDep,
    file: UploadFile,
    passphrase: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "replace",
    keep_current_user: Annotated[bool, Form()] = False,
) -> ImportSummary:
    """Replace all existing data with the contents of an encrypted backup file.

    With ``keep_current_user`` the calling admin's account survives the wipe;
    imported users that collide with it are renamed / re-identified.
    """
    content = await file.read()
    return await backup_service_of(request).restore(
        content, passphrase, mode, keep_current_user, actor_of(request, admin)
    )
