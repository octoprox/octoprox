# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Backup: the whole setup exported to one passphrase-encrypted file (:mod:`api.models.backup`), and imported from one."""

from __future__ import annotations

import base64
import gzip
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Any

import structlog
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import DateTime, LargeBinary, delete, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

from api.core import utc_now
from api.db.base import Base
from api.db.migrations import current_revision
from api.db.models import (
    ApiTokenModel,
    AuthIdentityModel,
    AuthProviderModel,
    ConnectorExitIpModel,
    ConnectorMetricsModel,
    ConnectorModel,
    CredentialModel,
    FailureMetricsModel,
    GeoDatabaseBlobModel,
    GeoDatabaseModel,
    GeoSettingsModel,
    HostMetricsModel,
    IpObservationModel,
    OpenVpnPeerModel,
    OpenVpnSettingsModel,
    ProjectMetricsModel,
    ProjectModel,
    ProviderAuditModel,
    ProviderDescriptorModel,
    ProxyMetricsModel,
    ProxyModel,
    TunnelPeerMetricsModel,
    UserModel,
    WireGuardPeerModel,
    WireGuardSettingsModel,
)
from api.db.redis import project_keys
from api.db.repository import MetricsRepository
from api.models.backup import (
    BackupEnvelope,
    BackupKdf,
    BackupPayload,
    ImportSummary,
    UserConflict,
)

if TYPE_CHECKING:
    from api.db.redis import RedisClient

logger = structlog.get_logger()

# What the envelope's ``format`` says, so a file of something else is refused
# before it is decrypted.
BACKUP_FORMAT = "octoprox-backup"
# The envelope's layout version; a file from a newer layout is refused.
BACKUP_FORMAT_VERSION = 1
# The payload is gzipped JSON encrypted with Fernet (AES-128-CBC and HMAC),
# under a key derived from the passphrase with a random salt per file.
# PBKDF2-HMAC-SHA256 rounds deriving the file key from the passphrase: the
# OWASP 2023 figure for SHA-256. That is 0.1 s of CPU on a laptop and more on
# a small server, per export or import; the routes spend it off the event loop.
PBKDF2_ITERATIONS = 600_000
# Keys per DEL when an import forgets its removed projects' keys: one round
# trip per this many, each project owning a dozen or so.
_FORGET_BATCH = 1000


class BackupError(Exception):
    """Base class for backup-related errors mapped to HTTP 400 by the route."""


class BackupIncompatibleError(BackupError):
    """The backup file is not compatible with this instance."""


class BackupDecryptError(BackupError):
    """The passphrase is wrong or the file is corrupt."""


@dataclass(frozen=True)
class _EntitySpec:
    """One entity a backup carries: its payload key, its model, and how its rows travel."""

    key: str
    model: type[Base]
    is_metric: bool = False
    # IP database bytes: exported only when the admin asks, since a city
    # database is around a hundred megabytes.
    is_database_file: bool = False
    # Metrics use an auto-increment integer PK that is referenced by nothing;
    # preserving it would clash with the table's sequence on later inserts, so
    # we drop it on import and let the sequence assign fresh ids.
    preserve_id: bool = True

    def travels(self, include_metrics: bool, include_database_files: bool) -> bool:
        """Whether an export made with these options carries the entity's rows."""
        return (include_metrics or not self.is_metric) and (include_database_files or not self.is_database_file)

    def dump(self, instance: Base) -> dict[str, Any]:
        """A row as JSON-safe values by attribute name: datetimes as ISO strings, bytes as base64."""
        row: dict[str, Any] = {}
        for name in self._columns:
            value = getattr(instance, name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, bytes | memoryview):
                value = base64.b64encode(bytes(value)).decode("ascii")
            row[name] = value
        return row

    def build(self, row: dict[str, Any]) -> Base:
        """The model instance a dumped row describes; keys the model does not map are skipped."""
        kwargs: dict[str, Any] = {}
        for name, value in row.items():
            sql_type = self._columns.get(name)
            if sql_type is None or (name == "id" and not self.preserve_id):
                continue
            if isinstance(value, str) and isinstance(sql_type, DateTime):
                value = datetime.fromisoformat(value)
            elif isinstance(value, str) and isinstance(sql_type, LargeBinary):
                value = base64.b64decode(value)
            kwargs[name] = value
        return self.model(**kwargs)

    @cached_property
    def _columns(self) -> dict[str, TypeEngine[Any]]:
        """Each column's SQL type by ORM attribute name (``metadata_``, not ``metadata``), in the mapper's order."""
        return {attr.key: attr.columns[0].type for attr in inspect(self.model).column_attrs}


# FK-safe order: parents before children, metrics last.
_ENTITY_SPECS: tuple[_EntitySpec, ...] = (
    _EntitySpec("users", UserModel),
    # Who signs in how: linked provider accounts and personal access tokens
    # reference users, so they come right after; the providers themselves
    # are global.
    _EntitySpec("auth_identities", AuthIdentityModel),
    _EntitySpec("api_tokens", ApiTokenModel),
    _EntitySpec("auth_providers", AuthProviderModel),
    _EntitySpec("projects", ProjectModel),
    _EntitySpec("credentials", CredentialModel),
    _EntitySpec("connectors", ConnectorModel),
    _EntitySpec("proxies", ProxyModel),
    _EntitySpec("proxy_metrics", ProxyMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("project_metrics", ProjectMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("connector_metrics", ConnectorMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("host_metrics", HostMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("failure_metrics", FailureMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("provider_descriptors", ProviderDescriptorModel),
    _EntitySpec("provider_audit_log", ProviderAuditModel),
    # IP attribution. Settings and database rows are configuration and always
    # travel; the files are opt-in; the history rides the metrics flag. The
    # exit aggregate references connectors, so it comes after them.
    _EntitySpec("geo_settings", GeoSettingsModel),
    _EntitySpec("geo_databases", GeoDatabaseModel),
    _EntitySpec("geo_database_blobs", GeoDatabaseBlobModel, is_database_file=True),
    _EntitySpec("ip_observations", IpObservationModel, is_metric=True, preserve_id=False),
    _EntitySpec("connector_exit_ips", ConnectorExitIpModel, is_metric=True),
    # WireGuard: the install's key pair and the devices allowed in. Peers
    # reference projects, so they come after them (and here is after).
    _EntitySpec("wireguard_settings", WireGuardSettingsModel),
    _EntitySpec("wireguard_peers", WireGuardPeerModel),
    # OpenVPN: the install's CA and server identity, and the devices allowed in.
    _EntitySpec("openvpn_settings", OpenVpnSettingsModel),
    _EntitySpec("openvpn_peers", OpenVpnPeerModel),
    # Device history for every tunnel protocol: after the devices it describes.
    _EntitySpec("tunnel_peer_metrics", TunnelPeerMetricsModel, is_metric=True, preserve_id=False),
)


def _app_version() -> str:
    try:
        return pkg_version("octoprox")
    except PackageNotFoundError:  # pragma: no cover - dev environments
        return "unknown"


class BackupFile:
    """A backup file: its bytes, and the unencrypted envelope in them that describes the encrypted payload."""

    def __init__(self, content: bytes, envelope: BackupEnvelope) -> None:
        self.content = content
        self.envelope = envelope

    @classmethod
    def build(
        cls,
        payload: dict[str, Any],
        passphrase: str,
        *,
        schema_version: str,
        include_metrics: bool,
        include_database_files: bool = False,
    ) -> BackupFile:
        """The file of ``payload``, gzipped and encrypted under a key derived from ``passphrase`` and a fresh salt."""
        salt = os.urandom(16)
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
        token = Fernet(cls._key(passphrase, salt, PBKDF2_ITERATIONS)).encrypt(compressed)
        envelope = BackupEnvelope(
            format=BACKUP_FORMAT,
            format_version=BACKUP_FORMAT_VERSION,
            created_at=utc_now().isoformat(),
            app_version=_app_version(),
            schema_version=schema_version,
            includes_metrics=include_metrics,
            includes_database_files=include_database_files,
            kdf=BackupKdf(
                iterations=PBKDF2_ITERATIONS,
                salt=base64.b64encode(salt).decode("ascii"),
            ),
            ciphertext=base64.b64encode(token).decode("ascii"),
        )
        return cls(envelope.model_dump_json(indent=2).encode("utf-8"), envelope)

    @classmethod
    def read(cls, content: bytes) -> BackupFile:
        """The file ``content`` holds, its envelope parsed, which needs no passphrase."""
        try:
            data = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError("File is not a valid Octoprox backup.") from exc
        try:
            envelope = BackupEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - normalize to a clean 400
            raise BackupError("File is not a valid Octoprox backup.") from exc
        return cls(content, envelope)

    def check_compatibility(self, schema_version: str) -> None:
        """Refuse a file of another format, a newer layout or another schema revision than ``schema_version``, unopened."""
        if self.envelope.format != BACKUP_FORMAT:
            raise BackupIncompatibleError("This file is not an Octoprox backup.")
        if self.envelope.format_version > BACKUP_FORMAT_VERSION:
            raise BackupIncompatibleError(
                "Backup was created by a newer version of Octoprox; upgrade this instance to import it."
            )
        if self.envelope.schema_version != schema_version:
            raise BackupIncompatibleError(
                f"Backup schema ('{self.envelope.schema_version}') does not match this "
                f"instance ('{schema_version}'). Migrate both instances to "
                "the same Octoprox version before importing."
            )

    def decrypt(self, passphrase: str) -> BackupPayload:
        """The payload, decrypted, decompressed and validated; ``BackupDecryptError`` for a wrong passphrase."""
        kdf = self.envelope.kdf
        try:
            key = self._key(passphrase, base64.b64decode(kdf.salt), kdf.iterations)
            compressed = Fernet(key).decrypt(base64.b64decode(self.envelope.ciphertext))
        except (InvalidToken, ValueError) as exc:
            raise BackupDecryptError("Incorrect passphrase or corrupt backup file.") from exc
        return BackupPayload.model_validate(json.loads(gzip.decompress(compressed)))

    @staticmethod
    def _key(passphrase: str, salt: bytes, iterations: int) -> bytes:
        """The Fernet key PBKDF2-HMAC-SHA256 derives from the passphrase and the salt."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


@dataclass(frozen=True)
class _KeptUser:
    """The admin who survives an import's wipe, whom no imported user, sign-in or token may collide with."""

    id: str
    username: str
    email: str

    def resolve_conflicts(self, rows: list[dict[str, Any]], id_map: dict[str, str]) -> list[UserConflict]:
        """Change the imported user rows that collide: a fresh id, a suffixed username, a cleared email; each change.

        Args:
            id_map: filled with each replaced id and its fresh one, for the rows that reference users to follow.
        """
        imported_usernames = {str(r.get("username", "")) for r in rows}
        conflicts: list[UserConflict] = []

        for row in rows:
            original_username = str(row.get("username", ""))
            new_username = original_username
            new_id = False
            email_cleared = False

            if row.get("id") == self.id:
                fresh = str(uuid.uuid4())
                id_map[str(row["id"])] = fresh
                row["id"] = fresh
                new_id = True

            if original_username == self.username:
                # -imported, then -imported-2 and on while another imported user has it.
                base = f"{original_username}-imported"
                candidate = base
                n = 2
                while candidate == self.username or candidate in imported_usernames:
                    candidate = f"{base}-{n}"
                    n += 1
                imported_usernames.add(candidate)
                row["username"] = candidate
                new_username = candidate

            # The column is unique, and there is no other address to make up.
            if self.email and row.get("email") == self.email:
                row["email"] = ""
                email_cleared = True

            if new_id or new_username != original_username or email_cleared:
                conflicts.append(
                    UserConflict(
                        original_username=original_username,
                        new_username=new_username,
                        new_id=new_id,
                        email_cleared=email_cleared,
                    )
                )
        return conflicts

    async def follow(self, session: AsyncSession, data: dict[str, Any], id_map: dict[str, str]) -> None:
        """Point imported sign-ins and tokens at their user's new id, and drop those that collide with this user's own."""
        for row in data.get("auth_identities", []):
            row["user_id"] = id_map.get(str(row.get("user_id")), row.get("user_id"))
        for row in data.get("api_tokens", []):
            row["user_id"] = id_map.get(str(row.get("user_id")), row.get("user_id"))

        kept_identities = (
            await session.execute(select(AuthIdentityModel).where(AuthIdentityModel.user_id == self.id))
        ).scalars().all()
        taken_subjects = {(i.provider, i.subject) for i in kept_identities}
        data["auth_identities"] = [
            row
            for row in data.get("auth_identities", [])
            if (row.get("provider"), row.get("subject")) not in taken_subjects and row.get("user_id") != self.id
        ]
        kept_hashes = set(
            (await session.execute(select(ApiTokenModel.token_hash).where(ApiTokenModel.user_id == self.id)))
            .scalars()
            .all()
        )
        data["api_tokens"] = [
            row
            for row in data.get("api_tokens", [])
            if row.get("token_hash") not in kept_hashes and row.get("user_id") != self.id
        ]


@dataclass(frozen=True)
class ImportResult:
    """What a replace-import restored, and the projects it removed."""

    summary: ImportSummary
    # Projects the install had that the backup does not carry, gone with
    # everything they held; the keys they own in Redis are forgotten once the
    # import has committed (:meth:`forget_removed_projects`).
    removed_project_ids: list[str] = field(default_factory=list)
    # Running metric totals recomputed from the imported history.
    totals_rebuilt: int = 0

    async def forget_removed_projects(self, redis_client: RedisClient) -> int:
        """Delete the removed projects' keys that never expire (``project_keys``); how many went."""
        keys = [key for project_id in self.removed_project_ids for key in project_keys(project_id)]
        deleted = 0
        for start in range(0, len(keys), _FORGET_BATCH):
            deleted += int(await redis_client.client.delete(*keys[start:start + _FORGET_BATCH]))
        return deleted


class BackupRepository:
    """The install's rows as a backup reads and replaces them, in the caller's session; the caller commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def schema_version(self) -> str:
        """The revision the database records: a file's ``schema_version`` must match it to be imported."""
        connection = await self._session.connection()
        revision = await connection.run_sync(current_revision)
        if not revision:
            raise BackupError("Database has no applied migration revision.")
        return revision

    async def export(self, include_metrics: bool, include_database_files: bool = False) -> dict[str, Any]:
        """Every entity's rows, dumped, by payload key; an entity the options leave out has none."""
        payload: dict[str, Any] = {}
        for spec in _ENTITY_SPECS:
            rows: list[dict[str, Any]] = []
            if spec.travels(include_metrics, include_database_files):
                instances = (await self._session.execute(select(spec.model))).scalars().all()
                rows = [spec.dump(instance) for instance in instances]
            payload[spec.key] = rows
        return payload

    async def replace_all(self, payload: BackupPayload, keep_user_id: str | None = None) -> ImportResult:
        """Wipe every entity and restore ``payload``'s, then recompute the running totals; on a raise the caller rolls back.

        Args:
            keep_user_id: the user who survives the wipe, imported users colliding with it changed; None wipes every user.
        """
        kept = await self._kept_user(keep_user_id) if keep_user_id is not None else None
        # The projects before the wipe: those the backup does not carry are removed.
        old_project_ids = set((await self._session.execute(select(ProjectModel.id))).scalars().all())
        await self._wipe(keep_user_id)

        data = payload.model_dump()
        user_conflicts: list[UserConflict] = []
        if kept is not None:
            id_map: dict[str, str] = {}
            user_conflicts = kept.resolve_conflicts(data["users"], id_map)
            await kept.follow(self._session, data, id_map)

        counts: dict[str, int] = {}
        for spec in _ENTITY_SPECS:
            rows = data.get(spec.key, [])
            for row in rows:
                self._session.add(spec.build(row))
            counts[spec.key] = len(rows)
            # Flush per entity so FK targets exist before children are added.
            await self._session.flush()

        # Last: the totals the counters read, from the history just restored.
        totals = await MetricsRepository(self._session).rebuild_totals()
        removed = sorted(old_project_ids - {str(row.get("id")) for row in data.get("projects", [])})

        logger.info(
            "Backup import restored entities",
            kept_current_user=kept is not None,
            user_conflicts=len(user_conflicts),
            removed_projects=len(removed),
            totals_rebuilt=totals,
            **counts,
        )
        return ImportResult(
            summary=ImportSummary(
                **counts,
                kept_current_user=kept is not None,
                user_conflicts=user_conflicts,
            ),
            removed_project_ids=removed,
            totals_rebuilt=totals,
        )

    async def _kept_user(self, keep_user_id: str) -> _KeptUser:
        """The user to keep, as it is before the wipe."""
        user = await self._session.get(UserModel, keep_user_id)
        if user is None:
            raise BackupError(
                "Your user account was not found in the database, so it cannot be kept. "
                "Import without keeping the current user instead."
            )
        return _KeptUser(id=user.id, username=user.username, email=user.email or "")

    async def _wipe(self, keep_user_id: str | None) -> None:
        """Delete every entity a backup carries, and every user but the kept one."""
        # Deleting projects cascades (DB-level ON DELETE CASCADE) to credentials,
        # connectors, proxies and both metrics tables. Users are independent.
        await self._session.execute(delete(ProjectModel))
        # Provider descriptors are global (not project-scoped) and their audit log
        # has no FK, so both are wiped explicitly. Likewise the attribution
        # settings row, the database rows (their blobs cascade) and the raw
        # observations; the exit aggregate cascades with connectors.
        await self._session.execute(delete(ProviderAuditModel))
        await self._session.execute(delete(ProviderDescriptorModel))
        await self._session.execute(delete(GeoSettingsModel))
        await self._session.execute(delete(GeoDatabaseModel))
        await self._session.execute(delete(IpObservationModel))
        # The WireGuard and OpenVPN identity rows are global too; their peers cascade with projects.
        await self._session.execute(delete(WireGuardSettingsModel))
        await self._session.execute(delete(OpenVpnSettingsModel))
        # Sign-in providers are global; linked sign-ins and tokens cascade with users.
        await self._session.execute(delete(AuthProviderModel))
        if keep_user_id is not None:
            await self._session.execute(delete(UserModel).where(UserModel.id != keep_user_id))
        else:
            await self._session.execute(delete(UserModel))
        await self._session.flush()
