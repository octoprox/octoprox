# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Export and import a full Octoprox setup as a passphrase-encrypted file.

An import replaces every row but the install's settings and audit trail in one transaction, which also records the
import, then appends one ``all`` change entry and deletes the keys only the removed projects used; the key derivation,
compression and encryption run off the event loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.backup import BackupError, BackupFile, BackupRepository
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.streams import ChangeKind
from api.models.audit import INSTALL_ENTITY_ID, AuditAction, AuditEntityType
from api.models.backup import BackupExport
from api.services.errors import WriteRejectedError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.core.backup import ImportResult
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.geo.service import GeoService
    from api.jobs.queue import JobQueue
    from api.models.backup import ExportRequest, ImportSummary
    from api.providers.registry import ProviderRegistry
    from api.providers.store import ProviderStore

logger = structlog.get_logger()

# The shortest passphrase an import accepts, as the export model requires.
MIN_PASSPHRASE_LENGTH = 8


class BackupService:
    """Exports the setup as an encrypted file, and restores one in place of every row, then has this process read it."""

    def __init__(
        self,
        session_factory: SessionFactory,
        jobs: JobQueue,
        redis_client: RedisClient,
        provider_registry: ProviderRegistry,
        *,
        changes: ChangeLog,
        provider_store: ProviderStore,
        geo: GeoService,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs
        self._redis_client = redis_client
        self._registry = provider_registry
        self._changes = changes
        self._provider_store = provider_store
        self._geo = geo

    async def export(self, data: ExportRequest, actor: Actor) -> BackupExport:
        """The full setup as a passphrase-encrypted file, named for today; the actor is named in the log, not recorded."""
        async with self._session_factory() as session:
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
            admin=actor.name,
            include_metrics=data.include_metrics,
            include_database_files=data.include_database_files,
            bytes=len(backup.content),
        )
        return BackupExport(content=backup.content, filename=filename)

    async def restore(
        self, content: bytes, passphrase: str, mode: str, keep_current_user: bool, actor: Actor
    ) -> ImportSummary:
        """Replace all existing data with the contents of an encrypted backup file; what was restored. With
        ``keep_current_user`` the actor's account survives the wipe, and imported users that collide with it are
        renamed."""
        if mode != "replace":
            raise WriteRejectedError(f"Unsupported import mode: {mode}")
        if len(passphrase) < MIN_PASSPHRASE_LENGTH:
            raise WriteRejectedError(
                f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters."
            )

        async with self._session_factory() as session:
            repository = BackupRepository(session)

            # 1. Compatibility first, before decryption, so an incompatible file is
            #    refused without needing the passphrase. 2. Decrypt and validate.
            try:
                backup = await asyncio.to_thread(BackupFile.read, content)
                backup.check_compatibility(await repository.schema_version())
                payload = await asyncio.to_thread(backup.decrypt, passphrase)
            except BackupError as exc:
                raise WriteRejectedError(str(exc)) from exc

            # 3. Wipe and restore in one transaction: a failure rolls everything back.
            try:
                result = await repository.replace_all(
                    payload, self._registry, keep_user_id=actor.id if keep_current_user else None
                )
            except BackupError as exc:
                await session.rollback()
                raise WriteRejectedError(str(exc)) from exc
            # In the import's transaction, so the trail it restored ends with who restored it.
            await actor.acted(
                session,
                AuditAction.IMPORTED,
                AuditEntityType.BACKUP,
                entity_id=INSTALL_ENTITY_ID,
                name="Backup",
                details={
                    "backup_created_at": backup.envelope.created_at,
                    "app_version": backup.envelope.app_version,
                    "includes_metrics": backup.envelope.includes_metrics,
                    "kept_current_user": result.summary.kept_current_user,
                },
            )
            # Committed before the change entry, so this process reads the
            # imported definitions back below.
            await session.commit()
        # Anything may have changed: every process reads every definition again.
        await self._changes.record(ChangeKind.ALL)

        await self._follow(result)
        logger.info(
            "Backup imported",
            admin=actor.name,
            **result.summary.model_dump(exclude={"user_conflicts"}),
            user_conflicts=len(result.summary.user_conflicts),
        )
        return result.summary

    async def _follow(self, result: ImportResult) -> None:
        """What follows a committed import here: forget the removed projects' keys, ask for the removed cloud
        connectors' scale runs, and read the imported definitions. All best effort: keys left behind are never read
        again, and the import's change entry brings the rest."""
        if result.deleting_connector_ids:
            # Never raises: what Redis refuses is logged and left to the producer.
            await self._jobs.request_now(JobKind.SCALE, result.deleting_connector_ids)
        if result.removed_project_ids:
            redis = self._redis_client
            await redis.after_commit(
                lambda: result.forget_removed_projects(redis),
                "forget the removed projects' keys",
                projects=len(result.removed_project_ids),
            )
        try:
            await self._provider_store.sync_all()
            await self._geo.resync()
        except Exception as exc:
            logger.warning(
                "Imported definitions not read yet, left to the refresher", error=error_text(exc)
            )
