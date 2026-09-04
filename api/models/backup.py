# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the admin backup (export/import) feature.

The backup file is a small *unencrypted* JSON envelope whose ``ciphertext``
field holds a Fernet-encrypted, gzipped JSON ``payload``. Keeping the envelope
unencrypted lets the importer read version/compatibility metadata *before* the
passphrase is applied, so an incompatible file is rejected without ever
attempting decryption.

Entity rows are carried as plain dicts (full per-column dumps). The exact
column shape is guaranteed identical between source and target by the
``schema_version`` (Alembic head) compatibility gate, so generic dicts are both
safe and resilient to schema evolution — no per-column duplication that could
drift away from the SQLAlchemy models.
"""

from typing import Any

from pydantic import BaseModel, Field

# Keys of the entity collections carried in a backup payload, in FK-safe
# insert order (users first, metrics last).
ENTITY_KEYS = (
    "users",
    "projects",
    "credentials",
    "connectors",
    "proxies",
    "proxy_metrics",
    "project_metrics",
)


class BackupKdf(BaseModel):
    """Key-derivation parameters needed to reproduce the encryption key."""

    algo: str = "pbkdf2-sha256"
    iterations: int
    salt: str  # base64


class BackupEnvelope(BaseModel):
    """Unencrypted outer wrapper of a backup file."""

    format: str
    format_version: int
    created_at: str
    app_version: str
    schema_version: str
    includes_metrics: bool
    kdf: BackupKdf
    ciphertext: str  # base64 Fernet token of gzip(json(payload))


class BackupPayload(BaseModel):
    """Decrypted contents of a backup: one list of rows per entity."""

    users: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    credentials: list[dict[str, Any]] = Field(default_factory=list)
    connectors: list[dict[str, Any]] = Field(default_factory=list)
    proxies: list[dict[str, Any]] = Field(default_factory=list)
    proxy_metrics: list[dict[str, Any]] = Field(default_factory=list)
    project_metrics: list[dict[str, Any]] = Field(default_factory=list)


class ExportRequest(BaseModel):
    """Request body for the export endpoint."""

    passphrase: str = Field(min_length=8)
    include_metrics: bool = False


class UserConflict(BaseModel):
    """Describes how an imported user was altered to coexist with the kept user."""

    original_username: str
    new_username: str
    new_id: bool = False
    email_cleared: bool = False


class ImportSummary(BaseModel):
    """Counts of rows restored per entity, returned by the import endpoint.

    ``users`` counts imported rows only; the kept current user (if any) is not
    included. ``user_conflicts`` lists imported users that were renamed,
    re-identified or had their email cleared to avoid clashing with the kept
    user.
    """

    users: int = 0
    projects: int = 0
    credentials: int = 0
    connectors: int = 0
    proxies: int = 0
    proxy_metrics: int = 0
    project_metrics: int = 0
    kept_current_user: bool = False
    user_conflicts: list[UserConflict] = Field(default_factory=list)
