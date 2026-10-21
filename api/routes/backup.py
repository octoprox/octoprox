# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Admin backup routes: export and import a full Octoprox setup as a passphrase-encrypted file.

An import replaces every row in one transaction, appends one ``all`` change entry and deletes the keys only the removed
projects used; the key derivation, compression and encryption run off the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile

from api.core import utc_now
from api.core.auth import DbDep, RequireAdminDep
from api.core.backup import BackupError, BackupFile, BackupRepository, ImportResult
from api.core.logging import error_text
from api.db.streams import ChangeKind
from api.models.backup import ExportRequest, ImportSummary
from api.routes.common import change_log_of, geo_service_of, provider_store_of, redis_client_of

logger = structlog.get_logger()

router = APIRouter(prefix="/backup")

# The shortest passphrase an import accepts, as the export model requires.
MIN_PASSPHRASE_LENGTH = 8


@router.post("/export")
async def export_backup(session: DbDep, data: ExportRequest, admin: RequireAdminDep) -> Response:
    """Export the full setup as a downloadable, passphrase-encrypted file."""
    repository = BackupRepository(session)
    schema_version = await repository.schema_version()
    payload = await repository.export(data.include_metrics, data.include_database_files)
    backup = await asyncio.to_thread(
        BackupFile.build,
        payload,
        data.passphrase,
        schema_version=schema_version,
        include_metrics=data.include_metrics,
        include_database_files=data.include_database_files,
    )

    filename = f"octoprox-backup-{utc_now().date().isoformat()}.opbak"
    logger.info(
        "Backup exported",
        admin=admin.username,
        include_metrics=data.include_metrics,
        include_database_files=data.include_database_files,
        bytes=len(backup.content),
    )
    return Response(
        content=backup.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=ImportSummary)
async def import_backup(
    request: Request,
    session: DbDep,
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
    if mode != "replace":
        raise HTTPException(status_code=400, detail=f"Unsupported import mode: {mode}")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters.",
        )

    raw = await file.read()
    repository = BackupRepository(session)

    # 1. Compatibility first, before decryption, so an incompatible file is
    #    refused without needing the passphrase. 2. Decrypt and validate.
    try:
        backup = await asyncio.to_thread(BackupFile.read, raw)
        backup.check_compatibility(await repository.schema_version())
        payload = await asyncio.to_thread(backup.decrypt, passphrase)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 3. Wipe and restore in one transaction: a failure rolls everything back.
    try:
        result = await repository.replace_all(payload, keep_user_id=admin.id if keep_current_user else None)
    except BackupError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Committed here rather than after the response, so this process reads
    # the imported definitions back below.
    await session.commit()
    # Anything may have changed: every process reads every definition again.
    await change_log_of(request).record(ChangeKind.ALL)

    await _follow_import(request, result)
    logger.info(
        "Backup imported",
        admin=admin.username,
        **result.summary.model_dump(exclude={"user_conflicts"}),
        user_conflicts=len(result.summary.user_conflicts),
    )
    return result.summary


async def _follow_import(request: Request, result: ImportResult) -> None:
    """What follows a committed import here: forget the removed projects' keys, and read the imported definitions.

    Both are best effort: the import stands, the keys left behind are never
    read again, and the definitions refresher reads what this misses when
    the import's change entry reaches it.
    """
    if result.removed_project_ids:
        redis = redis_client_of(request)
        await redis.after_commit(
            lambda: result.forget_removed_projects(redis),
            "forget the removed projects' keys",
            projects=len(result.removed_project_ids),
        )
    try:
        await provider_store_of(request).sync_all()
        await geo_service_of(request).resync()
    except Exception as exc:
        logger.warning("Imported definitions not read yet, left to the refresher", error=error_text(exc))
