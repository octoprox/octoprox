# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Admin backup routes: export and import a full Octoprox setup.

Both endpoints are admin-only. Export streams back a passphrase-encrypted file;
import replaces all existing data with the file's contents (in a transaction)
and then rebuilds the live in-memory cache. The importing admin may opt to keep
their own account so they are not locked out by a backup taken elsewhere.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.auth import RequireAdminDep
from api.core.backup import (
    BackupDecryptError,
    BackupError,
    BackupIncompatibleError,
    build_backup_file,
    check_compatibility,
    decrypt_payload,
    export_payload,
    get_schema_version,
    read_envelope,
    replace_all,
)
from api.db.session import get_db
from api.models.backup import ExportRequest, ImportSummary

logger = structlog.get_logger()

router = APIRouter(prefix="/backup")

DbDep = Annotated[AsyncSession, Depends(get_db)]

MIN_PASSPHRASE_LENGTH = 8


@router.post("/export")
async def export_backup(
    session: DbDep, data: ExportRequest, _admin: RequireAdminDep
) -> Response:
    """Export the full setup as a downloadable, passphrase-encrypted file."""
    schema_version = await get_schema_version(session)
    payload = await export_payload(session, data.include_metrics)
    file_bytes = build_backup_file(
        payload,
        data.passphrase,
        schema_version=schema_version,
        include_metrics=data.include_metrics,
    )

    filename = f"octoprox-backup-{utc_now().date().isoformat()}.opbak"
    logger.info(
        "Backup exported",
        admin=_admin.username,
        include_metrics=data.include_metrics,
        bytes=len(file_bytes),
    )
    return Response(
        content=file_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=ImportSummary)
async def import_backup(
    request: Request,
    session: DbDep,
    _admin: RequireAdminDep,
    file: UploadFile,
    passphrase: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "replace",
    keep_current_user: Annotated[bool, Form()] = False,
) -> ImportSummary:
    """Replace all existing data with the contents of an encrypted backup file.

    With ``keep_current_user`` the calling admin's account survives the wipe;
    imported users that collide with it are renamed / re-identified.
    """
    if mode != "replace":
        raise HTTPException(status_code=400, detail=f"Unsupported import mode: {mode}")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters.",
        )

    raw = await file.read()

    # 1. Compatibility check first — before decryption, so an incompatible
    #    file is rejected without needing the passphrase.
    try:
        envelope = read_envelope(raw)
        current_schema = await get_schema_version(session)
        check_compatibility(envelope, current_schema)
        # 2. Decrypt + validate.
        payload = decrypt_payload(envelope, passphrase)
    except (BackupIncompatibleError, BackupDecryptError, BackupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 3. Transactional wipe + restore. Everything happens in one transaction,
    #    so a failure rolls back and the instance is never left half-wiped.
    try:
        result = await replace_all(
            session, payload, keep_user_id=_admin.id if keep_current_user else None
        )
    except BackupError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Commit explicitly here (rather than relying on get_db's post-request
    # commit) so the next step's full_reload — which uses a separate pooled
    # connection — observes the restored rows instead of stale data.
    await session.commit()

    # 4. Purge stale Redis state for the replaced entities and rebuild the
    #    live cache so the imported setup is immediately effective.
    proxy_manager = request.app.state.proxy_manager
    await proxy_manager.apply_imported_state(result.old_project_ids, result.old_proxy_ids)

    logger.info(
        "Backup imported",
        admin=_admin.username,
        **result.summary.model_dump(exclude={"user_conflicts"}),
        user_conflicts=len(result.summary.user_conflicts),
    )
    return result.summary
