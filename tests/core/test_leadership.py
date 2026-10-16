# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Redis lease: ``confirm`` asks Redis rather than trusting the last refresh."""

from unittest.mock import patch

import pytest

from api.core.leadership import Lease
from api.db.redis import LEASE_KEY, RedisClient

KEY = LEASE_KEY.format(name="test")


class TestConfirm:
    async def test_a_held_lease_is_confirmed(self, redis_client: RedisClient) -> None:
        lease = Lease(redis_client, name="test", owner_id="me")
        assert await lease.try_acquire()
        try:
            assert await lease.confirm()
            assert lease.is_held
        finally:
            await lease.release()

    async def test_a_lease_taken_over_unnoticed_is_given_up(self, redis_client: RedisClient) -> None:
        lease = Lease(redis_client, name="test", owner_id="me")
        assert await lease.try_acquire()
        try:
            # The key expired before the next refresh and a peer took it.
            await redis_client.client.set(KEY, "peer")
            assert lease.is_held
            assert not await lease.confirm()
            assert not lease.is_held
        finally:
            await lease.release()
        # Giving up never touches the peer's lease.
        assert await redis_client.client.get(KEY) == "peer"

    async def test_an_expired_lease_is_given_up(self, redis_client: RedisClient) -> None:
        lease = Lease(redis_client, name="test", owner_id="me")
        assert await lease.try_acquire()
        try:
            await redis_client.client.delete(KEY)
            assert not await lease.confirm()
            assert not lease.is_held
        finally:
            await lease.release()

    async def test_giving_up_stops_the_refresh(self, redis_client: RedisClient) -> None:
        """A refresh left sleeping would wake to a re-acquired lease and run beside its new refresh."""
        lease = Lease(redis_client, name="test", owner_id="me")
        assert await lease.try_acquire()
        refresh = lease._refresh_task
        try:
            await redis_client.client.delete(KEY)
            assert not await lease.confirm()
            assert refresh is not None and refresh.done()
        finally:
            await lease.release()

    async def test_a_lease_never_acquired_is_not_confirmed(self, redis_client: RedisClient) -> None:
        assert not await Lease(redis_client, name="test", owner_id="me").confirm()


class TestTransitionLogging:
    @pytest.mark.parametrize(("per_cycle", "level"), [(False, "info"), (True, "debug")])
    async def test_a_per_cycle_lock_logs_its_transitions_at_debug(
        self, redis_client: RedisClient, per_cycle: bool, level: str
    ) -> None:
        """A leader taking over is worth a line; a lock taken every cycle per connector is not."""
        with patch("api.core.leadership.logger") as logger:
            lease = Lease(redis_client, name="test", owner_id="me", per_cycle=per_cycle)
            assert await lease.try_acquire()
            await lease.release()

        events = [call.args[0] for call in getattr(logger, level).call_args_list]
        assert events == ["Lease acquired", "Lease released"]
        assert not getattr(logger, "debug" if level == "info" else "info").called
