# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis-backed best-effort leases for singleton background workers.

Used to elect a single live owner per `name` across N Octoprox instances:
the leaseholder runs the work (metrics flush, autoscaler tick for a given
connector, …) while the others stand by, ready to take over within a few
seconds if the holder dies.

Acquisition uses ``SET NX PX``. Refresh and release use Lua scripts that
only mutate the key when the value still matches the lease holder's id —
so a slow holder whose lease has expired and been claimed by a peer
cannot accidentally extend or delete the peer's lease.

This is **not** fenced consensus (Chubby/etcd/Raft); under partition or
clock skew there may be brief windows where two holders both believe
they own the lease. For octoprox's workloads — metrics flushes,
health-check writes, scaling decisions — duplicates are safe and
recoverable. If a future workload requires strict mutual exclusion,
swap this primitive for a Postgres advisory lock (`pg_try_advisory_lock`)
behind the same interface.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from api.db.redis import LEASE_KEY, RedisClient

logger = structlog.get_logger()


# Lua: PEXPIRE iff the current value equals the holder id.
_REFRESH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Lua: DEL iff the current value equals the holder id.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class Lease:
    """A refreshing best-effort Redis lease.

    **Worker death semantics.** If the leaseholder process dies
    (SIGKILL, OOM, network partition) the refresh task stops firing.
    Redis expires the lease key after ``ttl_ms`` and any other instance
    can ``try_acquire`` it on its next poll — that is how failover
    happens automatically, with no explicit health check required.

    The release script is owner-checked: a zombie refresh that arrives
    after Redis has expired our key and a peer has taken the lease
    cannot accidentally extend or delete the peer's lease (its value no
    longer matches our owner_id).

    Args:
        redis_client: Connected Redis client.
        name: Lease name. The Redis key is ``lease:<name>``.
        owner_id: Identifier stored as the lease value. Use the process
            ``instance_id`` so logs and ownership checks are unambiguous.
        ttl_ms: Lease TTL in milliseconds. The lease is considered lost
            if the holder fails to refresh within this window.
        refresh_ms: How often the holder refreshes the TTL. Must be well
            under ``ttl_ms``; a 2-of-5 ratio gives a safe margin.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        name: str,
        owner_id: str,
        ttl_ms: int = 5000,
        refresh_ms: int = 2000,
    ) -> None:
        if refresh_ms >= ttl_ms:
            raise ValueError("refresh_ms must be less than ttl_ms")
        self._redis = redis_client
        self._name = name
        self._key = LEASE_KEY.format(name=name)
        self._owner_id = owner_id
        self._ttl_ms = ttl_ms
        self._refresh_ms = refresh_ms
        self._held = False
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_held(self) -> bool:
        return self._held

    async def try_acquire(self) -> bool:
        """Attempt to acquire the lease. Returns True if acquired.

        Idempotent: if already held by this instance, returns True
        without contacting Redis.
        """
        if self._held:
            return True
        try:
            acquired = await self._redis.client.set(
                self._key, self._owner_id, nx=True, px=self._ttl_ms
            )
        except Exception:
            logger.warning("Lease acquire failed", name=self._name, exc_info=True)
            return False
        if not acquired:
            return False
        self._held = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("Lease acquired", name=self._name, owner=self._owner_id)
        return True

    async def _refresh_loop(self) -> None:
        try:
            while self._held:
                await asyncio.sleep(self._refresh_ms / 1000.0)
                if not self._held:
                    return
                try:
                    ok = await self._redis.client.eval(  # type: ignore[misc]
                        _REFRESH_SCRIPT,
                        1,
                        self._key,
                        self._owner_id,
                        str(self._ttl_ms),
                    )
                except Exception:
                    logger.warning(
                        "Lease refresh raised", name=self._name, exc_info=True
                    )
                    self._held = False
                    return
                if int(ok) == 0:
                    logger.warning(
                        "Lease lost during refresh", name=self._name
                    )
                    self._held = False
                    return
        except asyncio.CancelledError:
            pass

    async def release(self) -> None:
        """Release the lease and stop refreshing.

        Safe to call multiple times; only deletes the Redis key when this
        instance still owns the lease.
        """
        if not self._held and self._refresh_task is None:
            return
        self._held = False
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        try:
            await self._redis.client.eval(  # type: ignore[misc]
                _RELEASE_SCRIPT, 1, self._key, self._owner_id
            )
        except Exception:
            logger.warning("Lease release failed", name=self._name, exc_info=True)
        logger.info("Lease released", name=self._name, owner=self._owner_id)
