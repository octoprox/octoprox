# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Backup service: export and import the full Octoprox setup.

Produces / consumes a passphrase-encrypted, self-contained backup file
covering every persistent entity (users, projects, credentials, connectors,
proxies and — optionally — historical metrics). See
:mod:`api.models.backup` for the file format.

Security model: the payload is gzipped JSON encrypted with Fernet (AES-128-CBC
+ HMAC). The key is derived from the admin's passphrase via PBKDF2-HMAC-SHA256
with a random per-file salt. The same passphrase is required to import.
"""

import base64
import gzip
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

import structlog
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import DateTime, delete, inspect, select, text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.base import Base
from api.db.models import (
    ConnectorModel,
    CredentialModel,
    ProjectMetricsModel,
    ProjectModel,
    ProxyMetricsModel,
    ProxyModel,
    UserModel,
)
from api.models.backup import (
    BackupEnvelope,
    BackupKdf,
    BackupPayload,
    ImportSummary,
    UserConflict,
)

logger = structlog.get_logger()

BACKUP_FORMAT = "octoprox-backup"
BACKUP_FORMAT_VERSION = 1
PBKDF2_ITERATIONS = 600_000


@dataclass(frozen=True)
class _EntitySpec:
    """Maps a payload key to its ORM model and import behaviour."""

    key: str
    model: type[Base]
    is_metric: bool = False
    # Metrics use an auto-increment integer PK that is referenced by nothing;
    # preserving it would clash with the table's sequence on later inserts, so
    # we drop it on import and let the sequence assign fresh ids.
    preserve_id: bool = True


# FK-safe order: parents before children, metrics last.
_ENTITY_SPECS: tuple[_EntitySpec, ...] = (
    _EntitySpec("users", UserModel),
    _EntitySpec("projects", ProjectModel),
    _EntitySpec("credentials", CredentialModel),
    _EntitySpec("connectors", ConnectorModel),
    _EntitySpec("proxies", ProxyModel),
    _EntitySpec("proxy_metrics", ProxyMetricsModel, is_metric=True, preserve_id=False),
    _EntitySpec("project_metrics", ProjectMetricsModel, is_metric=True, preserve_id=False),
)


class BackupError(Exception):
    """Base class for backup-related errors mapped to HTTP 400 by the route."""


class BackupIncompatibleError(BackupError):
    """The backup file is not compatible with this instance."""


class BackupDecryptError(BackupError):
    """The passphrase is wrong or the file is corrupt."""


# --------------------------------------------------------------------------- #
# Crypto helpers
# --------------------------------------------------------------------------- #


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """Derive a urlsafe-base64 Fernet key from a passphrase + salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


# --------------------------------------------------------------------------- #
# Column (de)serialization — generic over the SQLAlchemy models
# --------------------------------------------------------------------------- #


def _column_attrs(model: type[Base]) -> list[str]:
    """Return mapped column attribute names for a model (excludes relationships).

    Uses the ORM attribute name (e.g. ``metadata_``) rather than the DB column
    name (``metadata``) so values round-trip through the model constructor.
    """
    return [attr.key for attr in inspect(model).column_attrs]


def _datetime_attrs(model: type[Base]) -> set[str]:
    """Names of column attributes whose SQL type is DateTime."""
    return {
        attr.key
        for attr in inspect(model).column_attrs
        if isinstance(attr.columns[0].type, DateTime)
    }


def _dump_row(instance: Base) -> dict[str, Any]:
    """Serialize an ORM instance to a JSON-safe dict of all its columns."""
    row: dict[str, Any] = {}
    for name in _column_attrs(type(instance)):
        value = getattr(instance, name)
        if isinstance(value, datetime):
            value = value.isoformat()
        row[name] = value
    return row


def _build_model(spec: _EntitySpec, row: dict[str, Any]) -> Base:
    """Reconstruct an ORM instance from a serialized row dict.

    ISO datetime strings are parsed back to ``datetime`` for DateTime columns.
    Unknown keys are ignored defensively; the ``schema_version`` gate already
    guarantees the column set matches.
    """
    dt_attrs = _datetime_attrs(spec.model)
    valid = set(_column_attrs(spec.model))
    kwargs: dict[str, Any] = {}
    for name, value in row.items():
        if name not in valid:
            continue
        if not spec.preserve_id and name == "id":
            continue
        if name in dt_attrs and isinstance(value, str):
            value = datetime.fromisoformat(value)
        kwargs[name] = value
    return spec.model(**kwargs)


# --------------------------------------------------------------------------- #
# Schema version (compatibility gate)
# --------------------------------------------------------------------------- #


async def get_schema_version(session: AsyncSession) -> str:
    """Return the Alembic migration revision applied on the connected DB."""
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    revision = result.scalar_one_or_none()
    if not revision:
        raise BackupError("Database has no applied migration revision.")
    return str(revision)


def _app_version() -> str:
    try:
        return pkg_version("octoprox")
    except PackageNotFoundError:  # pragma: no cover - dev environments
        return "unknown"


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


async def export_payload(session: AsyncSession, include_metrics: bool) -> dict[str, Any]:
    """Read every entity from the DB into a serializable payload dict."""
    payload: dict[str, Any] = {}
    for spec in _ENTITY_SPECS:
        if spec.is_metric and not include_metrics:
            payload[spec.key] = []
            continue
        result: Result[Any] = await session.execute(select(spec.model))
        payload[spec.key] = [_dump_row(m) for m in result.scalars().all()]
    return payload


def build_backup_file(
    payload: dict[str, Any],
    passphrase: str,
    *,
    schema_version: str,
    include_metrics: bool,
) -> bytes:
    """Encrypt a payload and wrap it in the JSON envelope, returning file bytes."""
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt, PBKDF2_ITERATIONS)
    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
    token = Fernet(key).encrypt(compressed)

    envelope = BackupEnvelope(
        format=BACKUP_FORMAT,
        format_version=BACKUP_FORMAT_VERSION,
        created_at=utc_now().isoformat(),
        app_version=_app_version(),
        schema_version=schema_version,
        includes_metrics=include_metrics,
        kdf=BackupKdf(
            iterations=PBKDF2_ITERATIONS,
            salt=base64.b64encode(salt).decode("ascii"),
        ),
        ciphertext=base64.b64encode(token).decode("ascii"),
    )
    return envelope.model_dump_json(indent=2).encode("utf-8")


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def read_envelope(file_bytes: bytes) -> BackupEnvelope:
    """Parse the unencrypted envelope (no passphrase needed)."""
    try:
        data = json.loads(file_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError("File is not a valid Octoprox backup.") from exc
    try:
        return BackupEnvelope.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - normalize to a clean 400
        raise BackupError("File is not a valid Octoprox backup.") from exc


def check_compatibility(envelope: BackupEnvelope, current_schema_version: str) -> None:
    """Reject a backup that this instance cannot safely import.

    Runs before decryption so an incompatible file fails fast and without
    needing the passphrase. See the plan for the rules.
    """
    if envelope.format != BACKUP_FORMAT:
        raise BackupIncompatibleError("This file is not an Octoprox backup.")
    if envelope.format_version > BACKUP_FORMAT_VERSION:
        raise BackupIncompatibleError(
            "Backup was created by a newer version of Octoprox; "
            "upgrade this instance to import it."
        )
    if envelope.schema_version != current_schema_version:
        raise BackupIncompatibleError(
            f"Backup schema ('{envelope.schema_version}') does not match this "
            f"instance ('{current_schema_version}'). Migrate both instances to "
            "the same Octoprox version before importing."
        )


def decrypt_payload(envelope: BackupEnvelope, passphrase: str) -> BackupPayload:
    """Decrypt, decompress and validate the payload. Raises on bad passphrase."""
    try:
        salt = base64.b64decode(envelope.kdf.salt)
        key = _derive_key(passphrase, salt, envelope.kdf.iterations)
        token = base64.b64decode(envelope.ciphertext)
        compressed = Fernet(key).decrypt(token)
    except (InvalidToken, ValueError) as exc:
        raise BackupDecryptError(
            "Incorrect passphrase or corrupt backup file."
        ) from exc
    raw = gzip.decompress(compressed)
    return BackupPayload.model_validate(json.loads(raw))


@dataclass(frozen=True)
class ImportResult:
    """Outcome of a replace-import, including ids needing operational cleanup."""

    summary: ImportSummary
    old_project_ids: list[str]
    old_proxy_ids: list[str]


def _resolve_user_conflicts(
    rows: list[dict[str, Any]], kept: dict[str, Any]
) -> list[UserConflict]:
    """Mutate imported user rows so none collide with the kept (current) user.

    Users are referenced by nothing else in the schema, so an imported user
    whose ``id`` equals the kept user's id simply receives a fresh uuid. A
    clashing ``username`` is suffixed with ``-imported`` (then ``-imported-2``,
    ``-imported-3``, ... if that is also taken by another imported user). A
    clashing non-empty ``email`` is cleared, because the column carries a
    unique index and there is no meaningful alternative address to invent.
    Returns one :class:`UserConflict` per row that was modified.
    """
    imported_usernames = {str(r.get("username", "")) for r in rows}
    kept_email = kept.get("email") or ""
    conflicts: list[UserConflict] = []

    for row in rows:
        original_username = str(row.get("username", ""))
        new_username = original_username
        new_id = False
        email_cleared = False

        if row.get("id") == kept["id"]:
            row["id"] = str(uuid.uuid4())
            new_id = True

        if original_username == kept["username"]:
            base = f"{original_username}-imported"
            candidate = base
            n = 2
            while candidate == kept["username"] or candidate in imported_usernames:
                candidate = f"{base}-{n}"
                n += 1
            imported_usernames.add(candidate)
            row["username"] = candidate
            new_username = candidate

        if kept_email and row.get("email") == kept_email:
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


async def replace_all(
    session: AsyncSession, payload: BackupPayload, keep_user_id: str | None = None
) -> ImportResult:
    """Wipe all entities and restore from ``payload`` within the session.

    The caller's session is committed by its dependency; raising here rolls the
    whole operation back, so the instance is never left half-wiped. Returns the
    pre-wipe project/proxy ids so the caller can purge stale Redis state.

    If ``keep_user_id`` is given, that user row survives the wipe and any
    imported user that would collide with it (same id, username or email) is
    adjusted — see :func:`_resolve_user_conflicts`.
    """
    kept_row: dict[str, Any] | None = None
    if keep_user_id is not None:
        kept_user = await session.get(UserModel, keep_user_id)
        if kept_user is None:
            raise BackupError(
                "Your user account was not found in the database, so it cannot be kept. "
                "Import without keeping the current user instead."
            )
        kept_row = _dump_row(kept_user)

    # Capture pre-wipe ids for downstream Redis cleanup.
    old_project_ids = list(
        (await session.execute(select(ProjectModel.id))).scalars().all()
    )
    old_proxy_ids = list(
        (await session.execute(select(ProxyModel.id))).scalars().all()
    )

    # Deleting projects cascades (DB-level ON DELETE CASCADE) to credentials,
    # connectors, proxies and both metrics tables. Users are independent.
    await session.execute(delete(ProjectModel))
    if keep_user_id is not None:
        await session.execute(delete(UserModel).where(UserModel.id != keep_user_id))
    else:
        await session.execute(delete(UserModel))
    await session.flush()

    data = payload.model_dump()
    user_conflicts: list[UserConflict] = []
    if kept_row is not None:
        user_conflicts = _resolve_user_conflicts(data["users"], kept_row)

    counts: dict[str, int] = {}
    for spec in _ENTITY_SPECS:
        rows = data.get(spec.key, [])
        for row in rows:
            session.add(_build_model(spec, row))
        counts[spec.key] = len(rows)
        # Flush per entity so FK targets exist before children are added.
        await session.flush()

    logger.info(
        "Backup import restored entities",
        kept_current_user=kept_row is not None,
        user_conflicts=len(user_conflicts),
        **counts,
    )
    return ImportResult(
        summary=ImportSummary(
            **counts,
            kept_current_user=kept_row is not None,
            user_conflicts=user_conflicts,
        ),
        old_project_ids=old_project_ids,
        old_proxy_ids=old_proxy_ids,
    )
