# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""User accounts, people and service accounts alike, and the provider accounts linked to each.

Each change to a user is recorded in the audit log, and unlinking a sign-in is told to its hook; an answer shows the
user as the write left it in memory, so its ``updated_at`` is the one read before the write.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.password import hash_password, verify_password
from api.db.auth_repository import ApiTokenRepository, AuthIdentityRepository
from api.db.repository import UserRepository
from api.models.audit import AuditAction
from api.models.user import Invitation, User, UserKind, UserView
from api.services.errors import NameConflictError, NotFoundError, WriteRejectedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.audit import Actor
    from api.core.auth import CurrentUser
    from api.db.session import SessionFactory
    from api.models.auth import AuthIdentity
    from api.models.user import (
        ServiceAccountCreate,
        UserCreate,
        UserInviteCreate,
        UserSelfUpdate,
        UserUpdate,
    )

logger = structlog.get_logger()


def _refuse_on_a_service_account(
    user: User, *, email: str | None = None, password: str | None = None
) -> None:
    """Refuse a service account a password or an email: it authenticates with API tokens alone, and an email would
    let whoever holds that address at a sign-in provider sign in as it."""
    if not user.is_service:
        return
    if password is not None:
        raise WriteRejectedError("A service account has no password: it uses API tokens")
    if email:
        raise WriteRejectedError("A service account has no email: nobody signs in as it")


class UserService:
    """Reads, creates, invites, edits and deletes users, and reads and unlinks the provider accounts linked to them.

    Args:
        invite_expiry_hours: how long a set-password link works.
    """

    def __init__(self, session_factory: SessionFactory, *, invite_expiry_hours: int) -> None:
        self._session_factory = session_factory
        self._invite_expiry_hours = invite_expiry_hours

    async def every(self) -> list[UserView]:
        """Every user, people and service accounts alike, oldest first."""
        async with self._session_factory() as session:
            users = await UserRepository(session).get_all()
            providers = await AuthIdentityRepository(session).providers_by_user()
            tokens = await ApiTokenRepository(session).usage_by_user()
        return [UserView(user, providers.get(user.id) or [], tokens.get(user.id)) for user in users]

    async def get(self, user_id: str) -> UserView:
        """One user, with its linked sign-ins and its tokens' usage."""
        async with self._session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            if not user:
                raise NotFoundError("User not found")
            return await _view_of(session, user)

    async def create(self, data: UserCreate, actor: Actor) -> UserView:
        """A new person with a password; the username and the email must be free."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            await _refuse_taken(repo, data.username, data.email)
            user = User(
                username=data.username,
                email=data.email,
                password_hash=hash_password(data.password),
                role=data.role,
            )
            await repo.create(user)
            await actor.created(session, user)
            await session.commit()

        logger.info("User created", username=user.username, role=user.role.value)
        return UserView(user, [], None)

    async def invite(self, data: UserInviteCreate, base_url: str, actor: Actor) -> Invitation:
        """A new person without a password, and the link under ``base_url`` that sets it."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            await _refuse_taken(repo, data.username, data.email)

            token = secrets.token_urlsafe(48)
            expires_at = utc_now() + timedelta(hours=self._invite_expiry_hours)

            user = User(
                username=data.username,
                email=data.email,
                password_hash=None,
                role=data.role,
                invite_token=token,
                invite_token_expires_at=expires_at,
            )
            await repo.create(user)
            await actor.created(session, user, action=AuditAction.INVITED)
            await session.commit()

        logger.info("User invited", username=user.username, role=user.role.value)
        return Invitation(UserView(user, [], None), _invite_url(base_url, token))

    async def create_service_account(self, data: ServiceAccountCreate, actor: Actor) -> UserView:
        """A service account: a name and a role, with no email, password or invite link. A token caller may create
        one, since only a session can issue it the tokens it authenticates with."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            if await repo.get_by_username(data.username):
                raise NameConflictError(f"Username '{data.username}' already exists")

            user = User(username=data.username, role=data.role, kind=UserKind.SERVICE)
            await repo.create(user)
            await actor.created(session, user)
            await session.commit()

        logger.info("Service account created", username=user.username, role=user.role.value)
        return UserView(user, [], None)

    async def reinvite(
        self, user_id: str, base_url: str, caller: CurrentUser, actor: Actor
    ) -> Invitation:
        """A new set-password link for a user who has not set their password; the caller needs a session."""
        caller.require_session("send a set-password link")
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_id(user_id)
            if not user:
                raise NotFoundError("User not found")
            if user.is_service:
                raise WriteRejectedError("A service account has no password: it uses API tokens")
            if user.password_hash is not None:
                raise WriteRejectedError("User already has a password set")

            before = user.model_copy()
            token = secrets.token_urlsafe(48)
            user.invite_token = token
            user.invite_token_expires_at = utc_now() + timedelta(hours=self._invite_expiry_hours)
            await repo.update(user)
            await actor.updated(session, before, user, action=AuditAction.INVITE_REISSUED)
            view = await _view_of(session, user)
            await session.commit()

        logger.info("User reinvited", user_id=user_id, username=user.username)
        return Invitation(view, _invite_url(base_url, token))

    async def update(
        self, user_id: str, data: UserUpdate, caller: CurrentUser, actor: Actor
    ) -> UserView:
        """Edit a user. Setting a password needs a session, asked before the user is read; a service account takes
        neither a password nor an email, and no user changes kind."""
        if data.password is not None:
            caller.require_session("set a password")
        async with self._session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_id(user_id)
            if not user:
                raise NotFoundError("User not found")
            _refuse_on_a_service_account(user, email=data.email, password=data.password)
            before = user.model_copy()

            if data.username is not None:
                if data.username != user.username:
                    existing = await repo.get_by_username(data.username)
                    if existing:
                        raise NameConflictError(f"Username '{data.username}' already exists")
                user.username = data.username
            if data.email is not None:
                if data.email and data.email != user.email:
                    existing_email = await repo.get_by_email(data.email)
                    if existing_email:
                        raise NameConflictError(f"Email '{data.email}' already exists")
                user.email = data.email
            if data.password is not None:
                user.set_password(data.password)
            if data.role is not None:
                user.role = data.role
            if data.is_active is not None:
                user.is_active = data.is_active
            if data.theme_preference is not None:
                user.theme_preference = data.theme_preference

            await repo.update(user)
            await actor.updated(session, before, user)
            view = await _view_of(session, user)
            await session.commit()

        logger.info("User updated", user_id=user_id)
        return view

    async def update_own(self, data: UserSelfUpdate, caller: CurrentUser, actor: Actor) -> UserView:
        """The caller's edit of their own profile (email, password, theme); a password needs a session, and the
        current one when the account has a password. A service account sets neither a password nor an email."""
        async with self._session_factory() as session:
            repo = UserRepository(session)
            db_user = await repo.get_by_id(caller.id)
            if not db_user:
                raise NotFoundError("User not found")
            _refuse_on_a_service_account(db_user, email=data.email, password=data.password)
            before = db_user.model_copy()

            if data.password is not None:
                caller.require_session("set a password")
                if db_user.password_hash:
                    if not data.current_password:
                        raise WriteRejectedError("Current password is required to change password")
                    if not verify_password(data.current_password, db_user.password_hash):
                        raise WriteRejectedError("Current password is incorrect")
                db_user.set_password(data.password)

            if data.email is not None:
                if data.email and data.email != db_user.email:
                    existing_email = await repo.get_by_email(data.email)
                    if existing_email:
                        raise NameConflictError(f"Email '{data.email}' already exists")
                db_user.email = data.email

            if data.theme_preference is not None:
                db_user.theme_preference = data.theme_preference

            await repo.update(db_user)
            await actor.updated(session, before, db_user)
            view = await _view_of(session, db_user)
            await session.commit()
        return view

    async def delete(self, user_id: str, caller: CurrentUser, actor: Actor) -> None:
        """Delete a user; nobody deletes their own account."""
        if caller.id == user_id:
            raise WriteRejectedError("Cannot delete your own account")

        async with self._session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_id(user_id)
            if user is None or not await repo.delete(user_id):
                raise NotFoundError("User not found")
            await actor.deleted(session, user)
            await session.commit()

        logger.info("User deleted", user_id=user_id)

    # --- Linked sign-ins ---------------------------------------------------------------

    async def own_identities(self, caller: CurrentUser) -> list[AuthIdentity]:
        """The provider accounts the caller can sign in with; none for a session naming no user."""
        async with self._session_factory() as session:
            return await AuthIdentityRepository(session).list_for_user(caller.id)

    async def identities_of(self, user_id: str) -> list[AuthIdentity]:
        """A user's linked provider accounts."""
        async with self._session_factory() as session:
            if await UserRepository(session).get_by_id(user_id) is None:
                raise NotFoundError("User not found")
            return await AuthIdentityRepository(session).list_for_user(user_id)

    async def unlink(self, user_id: str, identity_id: str, actor: Actor) -> None:
        """Unlink one of a user's provider accounts; signing in with it again relinks it if the email still matches."""
        async with self._session_factory() as session:
            identity = await AuthIdentityRepository(session).delete(user_id, identity_id)
            if identity is None:
                raise NotFoundError("Linked sign-in not found")
            await actor.identity_unlinked(session, identity)
            await session.commit()
        logger.info(
            "Sign-in provider unlinked", user_id=user_id, identity_id=identity_id, by=actor.name
        )


async def _view_of(session: AsyncSession, user: User) -> UserView:
    """One user with its linked sign-ins and its tokens' usage read for it."""
    usage = await ApiTokenRepository(session).usage_by_user([user.id])
    providers = [
        i.provider.value for i in await AuthIdentityRepository(session).list_for_user(user.id)
    ]
    return UserView(user, providers, usage.get(user.id))


async def _refuse_taken(repo: UserRepository, username: str, email: str) -> None:
    """Refuse a new user a username, or a non-empty email, another user has."""
    existing = await repo.get_by_username(username)
    if existing:
        raise NameConflictError(f"Username '{username}' already exists")
    if email:
        existing_email = await repo.get_by_email(email)
        if existing_email:
            raise NameConflictError(f"Email '{email}' already exists")


def _invite_url(base_url: str, token: str) -> str:
    """The set-password link under the install's public URL (or the request's base URL)."""
    return f"{base_url}/set-password/{token}"
