# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""ApiTokenRepository as token auth uses it: a token read with its own user, and a use stamped once per granularity."""

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.api_tokens import hash_token
from api.db.auth_repository import ApiTokenRepository
from api.db.repository import UserRepository
from api.models.auth import ApiToken
from api.models.user import User, UserKind, UserRole

MINUTE = timedelta(minutes=1)


async def _token(session: AsyncSession, name: str, last_used_at: datetime | None = None) -> ApiToken:
    """A token of its own new service account, committed."""
    user = await UserRepository(session).create(User(username=name, kind=UserKind.SERVICE, role=UserRole.EDITOR))
    token = await ApiTokenRepository(session).create(
        ApiToken(
            user_id=user.id,
            name=name,
            token_hash=hash_token(f"octo_{name}"),
            token_prefix="octo_",
            last_used_at=last_used_at,
        )
    )
    await session.commit()
    return token


async def _stamp(session: AsyncSession, token: ApiToken) -> datetime | None:
    """The token's last use as stored."""
    result = await session.execute(text("SELECT last_used_at FROM api_tokens WHERE id = :id"), {"id": token.id})
    stamp: datetime | None = result.scalar_one()
    return stamp


class TestGetWithUser:
    async def test_a_token_comes_with_its_own_user(self, db_session: AsyncSession) -> None:
        await _token(db_session, "first-bot")
        token = await _token(db_session, "second-bot")

        found = await ApiTokenRepository(db_session).get_with_user(token.token_hash)

        assert found is not None
        stored, user = found
        assert (stored.id, stored.name) == (token.id, "second-bot")
        assert (user.id, user.username, user.role, user.kind, user.is_active) == (
            token.user_id,
            "second-bot",
            UserRole.EDITOR,
            UserKind.SERVICE,
            True,
        )

    async def test_an_unknown_hash_finds_nothing(self, db_session: AsyncSession) -> None:
        await _token(db_session, "bot")

        assert await ApiTokenRepository(db_session).get_with_user(hash_token("octo_nope")) is None


class TestTouchLastUsed:
    async def test_a_stamp_moves_only_once_it_is_the_granularity_old(self, db_session: AsyncSession) -> None:
        stamped = utc_now() - timedelta(hours=1)
        token = await _token(db_session, "bot", last_used_at=stamped)
        tokens = ApiTokenRepository(db_session)

        await tokens.touch_last_used(token.id, stamped + MINUTE - timedelta(seconds=1), MINUTE)
        too_soon = await _stamp(db_session, token)
        await tokens.touch_last_used(token.id, stamped + MINUTE, MINUTE)
        due = await _stamp(db_session, token)
        # A call that found the old stamp due, after another call moved it.
        await tokens.touch_last_used(token.id, stamped + MINUTE + timedelta(seconds=30), MINUTE)
        raced = await _stamp(db_session, token)

        assert (too_soon, due, raced) == (stamped, stamped + MINUTE, stamped + MINUTE)

    async def test_a_token_never_used_is_left_to_its_first_use(self, db_session: AsyncSession) -> None:
        token = await _token(db_session, "bot")

        await ApiTokenRepository(db_session).touch_last_used(token.id, utc_now(), MINUTE)

        assert await _stamp(db_session, token) is None
