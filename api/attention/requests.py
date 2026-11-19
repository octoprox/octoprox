# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alert checks asked for and made: the run-now request a write sends after its commit, and when each scope (a project,
or the install) was last checked, which the card's re-check spinner and its staleness read."""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import structlog
from redis.exceptions import RedisError

from api.attention.alert import CHECKS_STALE
from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.redis import ALERT_CHECK_KEY, ALERT_CHECK_TTL_SECONDS

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.jobs.queue import JobQueue

logger = structlog.get_logger()

# A request no worker took within this long stops showing as pending: the card's staleness, and the install's line
# that the checks are not running, take over.
PENDING_FOR = timedelta(seconds=60)


@dataclass(frozen=True)
class CheckState:
    """When a scope was last checked, whether a check asked for since is still waiting, and whether the last check is
    out of date."""

    checked_at: datetime | None
    pending: bool
    stale: bool


class AlertChecks:
    """Asks the workers to check a project's alerts now, and keeps when each scope was last checked."""

    def __init__(self, redis_client: RedisClient, jobs: JobQueue) -> None:
        self._redis = redis_client
        self._jobs = jobs

    async def request(self, project_id: str) -> None:
        """After a committed write: mark the project requested now and append its ``alerts`` run-now entry, in one
        MULTI (HSET requested, EXPIRE, XADD); a request Redis refuses is logged, the write standing."""
        await self._redis.after_commit(lambda: self._send(project_id), "ask for an alert check", project_id=project_id)

    async def mark_checked(self, scopes: Sequence[str], at: datetime) -> None:
        """Record that the scopes were checked with data read at ``at``, never moving a newer time back: one script
        call. Raises when Redis does not take it."""
        await self._redis.mark_alert_checks(scopes, _stamp(at))

    async def state(self, scope: str, now: datetime) -> CheckState:
        """The scope's last check; pending while a request newer than it is under ``PENDING_FOR`` old; stale when the
        check is older than ``CHECKS_STALE``; unknown (None, False, False), logged, while Redis cannot be read."""
        try:
            return await self.read(scope, now)
        except (RedisError, OSError) as exc:
            logger.warning("Alert check state unavailable", scope=scope, error=error_text(exc))
            return CheckState(checked_at=None, pending=False, stale=False)

    async def read(self, scope: str, now: datetime) -> CheckState:
        """As :meth:`state`, raising when Redis cannot be read."""
        checked, requested = await cast(
            "Awaitable[list[str | None]]",
            self._redis.client.hmget(ALERT_CHECK_KEY.format(scope=scope), ["checked", "requested"]),
        )
        checked_at, requested_at = _parsed(checked), _parsed(requested)
        pending = (
            requested_at is not None
            and (checked_at is None or requested_at > checked_at)
            and now - requested_at < PENDING_FOR
        )
        stale = checked_at is not None and now - checked_at > CHECKS_STALE
        return CheckState(checked_at=checked_at, pending=pending, stale=stale)

    async def _send(self, project_id: str) -> None:
        key = ALERT_CHECK_KEY.format(scope=project_id)
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.hset(key, "requested", _stamp(utc_now()))
        pipe.expire(key, ALERT_CHECK_TTL_SECONDS)
        self._jobs.append(pipe, JobKind.ALERTS, project_id)
        await pipe.execute()


def _stamp(at: datetime) -> str:
    """A time as the check hash keeps it: ISO with microseconds, so two compare as strings."""
    return at.isoformat(timespec="microseconds")


def _parsed(stored: str | None) -> datetime | None:
    """A stored time; None when there is none, or one this release cannot read."""
    if not stored:
        return None
    try:
        return datetime.fromisoformat(stored)
    except ValueError:
        return None
