# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential writes: create, edit and delete, validated by the provider registry and announced with a ``credential`` entry.

The vendor's check runs before the transaction opens, and a credential a connector uses is never deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import IntegrityError

from api.db.repository import ConnectorRepository, CredentialRepository, ProjectRepository
from api.db.streams import ChangeKind
from api.models.credential import Credential, CredentialCreate, CredentialUpdate
from api.providers.registry import UnknownProviderError
from api.providers.sdk.discovery import CredentialValidator
from api.services.errors import (
    InvalidConfigError,
    NameConflictError,
    NotFoundError,
    WriteRejectedError,
    name_conflict,
    validated,
)

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.providers.registry import ProviderRegistry, ProviderType

logger = structlog.get_logger()

# The per-project unique index on credential names (migration 022; case-insensitive).
_NAME_UNIQUE = "ix_credentials_project_name_unique"


class CredentialService:
    """Creates, edits and deletes credentials."""

    def __init__(
        self, session_factory: SessionFactory, provider_registry: ProviderRegistry, *, changes: ChangeLog
    ) -> None:
        self._session_factory = session_factory
        self._registry = provider_registry
        self._changes = changes

    async def create(self, project_id: str, data: CredentialCreate) -> Credential:
        """Validate and store a new credential of the project; the saved credential."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
        ptype = self._provider_type(data.type)
        config = await self._verified(ptype, validated(ptype.credential_validator, data.config))
        credential = Credential(name=data.name, type=ptype.id, project_id=project_id, config=config)
        try:
            async with self._session_factory() as session:
                await CredentialRepository(session).create(credential)
                await session.commit()
        except IntegrityError as exc:
            raise _taken(exc, credential.name) from None
        logger.info("Added credential", credential_id=credential.id, name=credential.name)
        await self._changes.record(ChangeKind.CREDENTIAL, credential.id)
        return credential

    async def update(self, project_id: str, credential_id: str, data: CredentialUpdate) -> Credential:
        """Apply a new name or config to a credential of the project; the saved credential.

        The type never changes; a new config is validated for the credential's type, and checked with the vendor when a
        credential field changed.
        """
        async with self._session_factory() as session:
            current = await CredentialRepository(session).get_in_project(project_id, credential_id)
        if current is None:
            raise NotFoundError("Credential not found")
        changes: dict[str, Any] = {}
        if data.name is not None:
            changes["name"] = data.name
        if data.config is not None:
            changes["config"] = await self._edited_config(current, data.config)
        edited = current.model_copy(update=changes)
        async with self._session_factory() as session:
            try:
                saved = await CredentialRepository(session).update(edited)
                await session.commit()
            except IntegrityError as exc:
                raise _taken(exc, edited.name) from None
        if saved is None:
            # Deleted between the read and the write.
            raise NotFoundError("Credential not found")
        logger.info("Updated credential", credential_id=credential_id, name=saved.name)
        await self._changes.record(ChangeKind.CREDENTIAL, credential_id)
        return saved

    async def delete(self, project_id: str, credential_id: str) -> None:
        """Delete a credential of the project; refused while a connector uses it."""
        async with self._session_factory() as session:
            credentials = CredentialRepository(session)
            if await credentials.get_in_project(project_id, credential_id) is None:
                raise NotFoundError("Credential not found")
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            users = sum(1 for connector in connectors if connector.credential_id == credential_id)
            if users:
                raise WriteRejectedError(f"Cannot delete credential: {users} connector(s) are using it")
            try:
                await credentials.delete(credential_id)
                await session.commit()
            except IntegrityError:
                # A connector took it up between the check and the delete
                # (connectors reference credentials without ON DELETE).
                raise WriteRejectedError("Cannot delete credential: a connector is using it") from None
        logger.info("Removed credential", credential_id=credential_id)
        await self._changes.record(ChangeKind.CREDENTIAL, credential_id)

    def _provider_type(self, type_id: str) -> ProviderType:
        """The registered provider type ``type_id`` names; an unknown type is an invalid config."""
        try:
            return self._registry.require(type_id)
        except UnknownProviderError as exc:
            raise InvalidConfigError(str(exc)) from None

    async def _edited_config(self, current: Credential, incoming: dict[str, Any]) -> dict[str, Any]:
        """The config an edit stores: validated, carrying over captured values, and vendor-checked if secrets moved."""
        ptype = self._provider_type(current.type)
        stored = current.config or {}
        merged = dict(incoming)
        check = ptype.descriptor.validation if ptype.descriptor is not None else None
        if check is not None:
            # What an earlier vendor check captured (a customer id) is not a
            # field the user edits: kept unless the edit sets it.
            for key in check.capture:
                if key not in merged and key in stored:
                    merged[key] = stored[key]
        config = validated(ptype.credential_validator, merged)
        if any(stored.get(field.key) != config.get(field.key) for field in ptype.credential_fields):
            config = await self._verified(ptype, config)
        return config

    async def _verified(self, ptype: ProviderType, config: dict[str, Any]) -> dict[str, Any]:
        """``config`` after the provider's vendor-side check, with what it captured; refused when the vendor says no."""
        if ptype.descriptor is None or ptype.descriptor.validation is None:
            return config
        outcome = await CredentialValidator(ptype.descriptor, self._registry.runtime).validate(config)
        if not outcome.ok:
            raise WriteRejectedError(outcome.message)
        checked: dict[str, Any] = outcome.result or config
        return checked


def _taken(exc: IntegrityError, name: str) -> NameConflictError:
    """The per-project unique name ``name`` collided with."""
    return name_conflict(exc, {_NAME_UNIQUE: f"A credential named '{name}' already exists in this project"})
