# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Recording the audit log: the log of an api process, and the actor whose changes it records.

A route turns the signed-in user into an :class:`Actor` and passes it to the write, which records in its own
transaction; a worker job holds no actor, so a system write is never recorded or credited to a person.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from api.db.audit_repository import AuditRepository
from api.models.audit import AuditAction, Audited, AuditEntry

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.auth import ApiToken, AuthIdentity
    from api.models.user import User


class AuditLog:
    """Where an api process records audit entries: rows added to the caller's transaction.

    The sign-in and token hooks, which the public routes call as those happen, record nothing in the free edition; an
    extension's subclass records them, and may do more with every entry.
    """

    async def record(self, session: AsyncSession, entries: Sequence[AuditEntry]) -> None:
        """Add ``entries`` to ``session``'s transaction, in one statement; the caller commits."""
        await AuditRepository(session).add(entries)

    def actor(
        self, id: str | None, name: str, request_id: str | None = None, *, details: Mapping[str, Any] | None = None
    ) -> Actor:
        """Someone whose changes this log records: a user's id and username, the request they act in, and what
        each of their entries carries in ``details``."""
        return Actor(self, id, name, request_id, details)

    async def signed_in(
        self, request: Request, user: User, method: str, *, provider: str | None = None, linked: bool = False
    ) -> None:
        """A user signed in, by password, invite link or a sign-in provider (``linked`` when it linked the account).

        An extension records it in a transaction of its own and never fails the sign-in, as for every access event.
        """

    async def sign_in_failed(
        self,
        request: Request,
        method: str,
        reason: str,
        *,
        user: User | None = None,
        username: str | None = None,
        provider: str | None = None,
    ) -> None:
        """A sign-in was refused for ``reason``, before the refusal is answered; ``username`` is as typed when no user
        has it."""

    async def token_issued(self, session: AsyncSession, actor: Actor, token: ApiToken) -> None:
        """A personal access token was created, in the transaction that stores it."""

    async def token_first_used(self, request: Request, token: ApiToken, user: User) -> None:
        """A personal access token authenticated a request for the first time, once its first use is stamped."""

    async def token_revoked(self, session: AsyncSession, actor: Actor, token: ApiToken) -> None:
        """A personal access token was deleted, in the transaction that deletes it."""

    async def identity_unlinked(self, session: AsyncSession, actor: Actor, identity: AuthIdentity) -> None:
        """A linked sign-in was removed from a user, in the transaction that removes it."""


class Actor:
    """Who is making a change, and the audit log their changes go to.

    Each method records one write's entries in the write's session, after the repository write and before the commit.
    """

    def __init__(
        self,
        log: AuditLog,
        id: str | None,
        name: str,
        request_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._log = log
        self.id = id
        self.name = name
        self.request_id = request_id
        # What every entry of this actor carries, such as the personal access token it acts through.
        self.details: dict[str, Any] = dict(details or {})

    async def created(
        self,
        session: AsyncSession,
        entity_or_entities: Audited | Sequence[Audited],
        *,
        masked: Collection[str] = (),
        project_id: str | None = None,
        action: str = AuditAction.CREATED,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one entry per entity created, every recorded field with a value as ``[null, value]``."""
        entities = [entity_or_entities] if isinstance(entity_or_entities, Audited) else list(entity_or_entities)
        await self._log.record(
            session,
            [
                self._entry(action, entity, type(entity).audit_changes(None, entity, masked), project_id, details)
                for entity in entities
            ],
        )

    async def updated(
        self,
        session: AsyncSession,
        before: Audited,
        after: Audited,
        *,
        masked: Collection[str] = (),
        project_id: str | None = None,
        action: str = AuditAction.UPDATED,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the fields that differ between ``before`` and ``after``; nothing when none do."""
        changes = type(after).audit_changes(before, after, masked)
        if changes:
            await self._log.record(session, [self._entry(action, after, changes, project_id, details)])

    async def deleted(
        self,
        session: AsyncSession,
        entity: Audited,
        *,
        masked: Collection[str] = (),
        project_id: str | None = None,
        action: str = AuditAction.DELETED,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a deletion, every recorded field with a value as ``[value, null]``, so the last state outlives it."""
        changes = type(entity).audit_changes(entity, None, masked)
        await self._log.record(session, [self._entry(action, entity, changes, project_id, details)])

    async def acted(
        self,
        session: AsyncSession,
        action: str,
        entity_type: str,
        *,
        entity_id: str = "",
        name: str = "",
        project_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an action on something that is not a stored entity, such as a backup import."""
        entry = AuditEntry(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=name,
            project_id=project_id,
            actor_id=self.id,
            actor=self.name,
            request_id=self.request_id,
            details=self._details(details),
        )
        await self._log.record(session, [entry])

    async def token_issued(self, session: AsyncSession, token: ApiToken) -> None:
        """Tell the log of a personal access token this actor created, in the transaction that stores it."""
        await self._log.token_issued(session, self, token)

    async def token_revoked(self, session: AsyncSession, token: ApiToken) -> None:
        """Tell the log of a personal access token this actor deleted, in the transaction that deletes it."""
        await self._log.token_revoked(session, self, token)

    async def identity_unlinked(self, session: AsyncSession, identity: AuthIdentity) -> None:
        """Tell the log of a linked sign-in this actor removed, in the transaction that removes it."""
        await self._log.identity_unlinked(session, self, identity)

    def _entry(
        self,
        action: str,
        entity: Audited,
        changes: dict[str, list[Any]],
        project_id: str | None,
        details: Mapping[str, Any] | None,
    ) -> AuditEntry:
        """The entry of one entity, its project ``project_id`` when given."""
        return AuditEntry(
            action=action,
            entity_type=entity.audit_type,
            entity_id=entity.audit_id(),
            entity_name=entity.audit_name(),
            project_id=project_id if project_id is not None else entity.audit_project(),
            actor_id=self.id,
            actor=self.name,
            request_id=self.request_id,
            changes=changes,
            details=self._details(details),
        )

    def _details(self, details: Mapping[str, Any] | None) -> dict[str, Any]:
        """The actor's details and the call's (an upload's ``via``), the call's value for a key both name."""
        return {**self.details, **(details or {})}
