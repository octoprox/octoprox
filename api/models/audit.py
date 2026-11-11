# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log's vocabulary: what an entry is about, the entry itself, and the base of every recorded entity model.

An entry says who changed what and when, with a diff ``{field: [before, after]}`` over the entity's recorded fields in
which a secret is only ever set, changed or cleared (see docs/architecture.md).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from api.core import utc_now

# How a masked value is written while it is set.
MASK = "********"
# The entity id of an install-wide singleton (a settings row) and of an install-level action (a backup import).
INSTALL_ENTITY_ID = "1"
# Days the audit log keeps entries until an admin says otherwise; 0 keeps them forever.
DEFAULT_AUDIT_RETENTION_DAYS = 365
# The longest retention an admin can set: a century, so the cutoff it gives is still a date.
MAX_AUDIT_RETENTION_DAYS = 36500
# The width of each bounded text column of ``audit_log``: an entry cuts a longer value to it.
AUDIT_TEXT_WIDTHS: dict[str, int] = {
    "action": 32,
    "entity_type": 32,
    "entity_id": 64,
    "entity_name": 255,
    "actor": 255,
    "request_id": 128,
}


class AuditEntityType(StrEnum):
    """What an audit entry is about: an entity of a project, or of the install."""

    PROJECT = "project"
    CONNECTOR = "connector"
    CREDENTIAL = "credential"
    PROXY = "proxy"
    WIREGUARD_DEVICE = "wireguard_device"
    OPENVPN_DEVICE = "openvpn_device"
    PROVIDER = "provider"
    USER = "user"
    SIGN_IN_PROVIDER = "sign_in_provider"
    GEO_SETTINGS = "geo_settings"
    GEO_DATABASE = "geo_database"
    WIREGUARD_SETTINGS = "wireguard_settings"
    OPENVPN_SETTINGS = "openvpn_settings"
    SYSTEM_SETTINGS = "system_settings"
    BACKUP = "backup"

    @property
    def admin_only(self) -> bool:
        """Whether only admins may read the type's history: every install-wide type."""
        return self not in _PROJECT_SCOPED


# The types whose history every signed-in user may read, as they may read the entities.
_PROJECT_SCOPED = frozenset(
    {
        AuditEntityType.PROJECT,
        AuditEntityType.CONNECTOR,
        AuditEntityType.CREDENTIAL,
        AuditEntityType.PROXY,
        AuditEntityType.WIREGUARD_DEVICE,
        AuditEntityType.OPENVPN_DEVICE,
    }
)


class AuditAction(StrEnum):
    """What the public code records; the column is plain text, so an extension records actions of its own too."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    IMPORTED = "imported"
    TRAFFIC_RESET = "traffic_reset"
    INVITED = "invited"
    INVITE_REISSUED = "invite_reissued"
    ROTATED = "rotated"


class Audited(BaseModel):
    """The base of every entity model whose user-driven changes the audit log records, in place of ``BaseModel``.

    A subclass declares its type and the fields it leaves out, masks or compares one key down; to list its recorded
    fields positively, or record a derived one, it overrides ``audit_fields``, and masking still applies on top.
    """

    # What the model's entries are about.
    audit_type: ClassVar[AuditEntityType]
    # Fields never compared or recorded.
    audit_ignored: ClassVar[frozenset[str]] = frozenset({"id", "created_at", "updated_at"})
    # Keys written only as set or empty (MASK or None): secrets, and values too large to keep. A field's name masks
    # it and every key under it, ``<field>.*`` every key under it.
    audit_masked: ClassVar[frozenset[str]] = frozenset()
    # JSON settings compared one key at a time, as ``<field>.<key>``, so one key of them can be masked.
    audit_nested: ClassVar[frozenset[str]] = frozenset()

    def audit_id(self) -> str:
        """The entity's id in its entries."""
        return str(getattr(self, "id", ""))

    def audit_name(self) -> str:
        """The entity's name at the time, which keeps its entries readable once it is gone."""
        return str(getattr(self, "name", ""))

    def audit_project(self) -> str | None:
        """The project the entity belongs to; None for an entity of the install."""
        project_id = getattr(self, "project_id", None)
        return None if project_id is None else str(project_id)

    def audit_fields(self) -> dict[str, Any]:
        """The recorded fields with their real values, JSON-safe: every field but ``audit_ignored``.

        A model overrides it to list its fields positively or to record a derived value; masking and the nesting of
        ``audit_nested`` apply on top.
        """
        return self.model_dump(mode="json", exclude=set(self.audit_ignored))

    @classmethod
    def audit_changes(
        cls, before: Audited | None, after: Audited | None, masked: Collection[str] = ()
    ) -> dict[str, list[Any]]:
        """``{key: [before, after]}``: every key with a value for a creation (no ``before``) or a deletion (no
        ``after``), and the keys whose values differ for an update, so an update that changes nothing gives none.

        A masked key is compared on its real values and written as MASK when set and None when empty.
        """
        old = before._audit_flat() if before is not None else {}
        new = after._audit_flat() if after is not None else {}
        hidden = cls.audit_masked | frozenset(masked)
        changes: dict[str, list[Any]] = {}
        for key in dict.fromkeys([*old, *new]):
            was, now = old.get(key), new.get(key)
            if was == now or (_empty(was) and _empty(now)):
                continue
            changes[key] = [_written(was), _written(now)] if _is_hidden(key, hidden) else [was, now]
        return changes

    def _audit_flat(self) -> dict[str, Any]:
        """``audit_fields`` with each ``audit_nested`` field's keys in its place, as ``<field>.<key>``."""
        flat: dict[str, Any] = {}
        for name, value in self.audit_fields().items():
            if name in self.audit_nested and isinstance(value, dict):
                flat.update({f"{name}.{key}": item for key, item in value.items()})
            else:
                flat[name] = value
        return flat


def _empty(value: Any) -> bool:
    """Whether a recorded value says nothing: None, or an empty string, list or object (a JSON default)."""
    return value is None or (isinstance(value, str | list | dict) and not value)


def _written(value: Any) -> str | None:
    """A masked value as an entry writes it: MASK when set, None when empty."""
    return None if _empty(value) else MASK


def _is_hidden(key: str, hidden: frozenset[str]) -> bool:
    """Whether ``hidden`` masks ``key``: by name, by the field it is a key of, or by ``<field>.*``."""
    if key in hidden:
        return True
    field, _dot, rest = key.partition(".")
    return bool(rest) and (field in hidden or f"{field}.*" in hidden)


class AuditEntry(BaseModel):
    """One row of the audit log: who did what to which entity and when, the diff, and the context of the action."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=utc_now)
    # An AuditAction, or an extension's own action.
    action: str
    entity_type: str
    # '' for an action on no stored entity, or a sign-in by a name no user has.
    entity_id: str = ""
    # The entity's name at the time.
    entity_name: str = ""
    project_id: str | None = None
    # The user who acted, by id and by username at the time; None and '' for nobody known.
    actor_id: str | None = None
    actor: str = ""
    # The X-Request-ID of the request that acted.
    request_id: str | None = None
    changes: dict[str, list[Any]] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator(*AUDIT_TEXT_WIDTHS)
    @classmethod
    def _fit_column(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Cut to the column's width, so text a caller sent can never fail the insert."""
        return value if value is None else value[: AUDIT_TEXT_WIDTHS[str(info.field_name)]]


class AuditPage(BaseModel):
    """A page of entries, and how many match in all."""

    entries: list[AuditEntry]
    total: int


class AuditHistory(BaseModel):
    """One entity's entries, newest first, and whether older ones remain (read on ``before`` the last one)."""

    entries: list[AuditEntry]
    more: bool


class AuditRetention(BaseModel):
    """How many days audit entries are kept, 0 for ever; the hourly compaction applies it."""

    days: int


class AuditRetentionUpdate(BaseModel):
    """A new retention for the audit log, in days (0 keeps entries forever)."""

    days: int = Field(ge=0, le=MAX_AUDIT_RETENTION_DAYS)
