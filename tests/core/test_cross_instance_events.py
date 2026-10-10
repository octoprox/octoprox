# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for cross-instance cache invalidation over Redis Pub/Sub.

The production setup runs many ProxyManager processes against a shared
Redis + Postgres. When one process mutates an entity, the EventBus
publishes a tiny invalidation message on `octoprox:events`; other
processes' subscriber loops call the corresponding `reload_<entity>(id)`
to refresh their cache from Postgres.

These tests stand up ONE ProxyManager and simulate a *peer* instance by
publishing the same kind of payload directly to Redis with a different
`instance_id`. The subscriber loop should pick it up and reload.
"""
import asyncio
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.event_bus import EVENT_CHANNEL
from api.core.proxy_manager import ProxyManager
from api.db.redis import METRIC_DELTAS_CHANNEL, RedisClient
from api.db.repository import ProjectRepository
from api.models.project import Project


@pytest.fixture
async def started_proxy_manager(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    test_settings: Settings,
    db_session: AsyncSession,  # ensure tables are cleaned up
) -> ProxyManager:
    """A fully-started ProxyManager (background tasks running)."""
    manager = ProxyManager(
        session_factory=db_session_factory,
        redis_client=redis_client,
        settings=test_settings,
    )
    await manager.start()
    # Give the subscriber loop a moment to actually subscribe to the channel.
    await asyncio.sleep(0.2)
    yield manager
    await manager.stop()


async def _wait_until(
    predicate, *, timeout: float = 3.0, interval: float = 0.05
) -> bool:
    """Poll `predicate()` until it returns truthy or timeout elapses.

    Accepts both sync and async predicates.
    """
    async def _check() -> bool:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _check():
            return True
        await asyncio.sleep(interval)
    return await _check()


async def _seed_proxy(manager: ProxyManager, host_prefix: str):
    """Create a project/credential/connector/proxy through ``manager``.

    Driven through the manager rather than the repositories so the normal
    events fire and the proxy lands in its cache, which is the state a peer
    event arrives into.
    """
    from api.models.connector import Connector
    from api.models.credential import Credential, CredentialType
    from api.models.proxy import Proxy, ProxyProtocol

    project = Project(name=f"{host_prefix} project", username=host_prefix, password="p")
    credential = Credential(
        name=f"{host_prefix} cred",
        type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id,
        config={},
    )
    connector = Connector(
        name=f"{host_prefix} conn",
        credential_id=credential.id,
        credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id,
        config={},
    )
    proxy = Proxy(
        host=f"{host_prefix}.example.com",
        port=8080,
        protocol=ProxyProtocol.HTTP,
        connector_id=connector.id,
    )
    await manager.add_project(project)
    await manager.add_credential(credential)
    await manager.add_connector(connector)
    await manager.add_proxy(proxy)
    return proxy


class TestCrossInstanceProjectReload:
    """A peer instance's project_changed event should refresh our cache."""

    async def test_peer_added_project_propagates(
        self,
        started_proxy_manager: ProxyManager,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        # Simulate a peer instance: insert a project directly into Postgres,
        # bypassing started_proxy_manager.add_project so no in-process event
        # fires.
        peer_project = Project(
            name="Peer Added",
            username="peeruser1",
            password="pass",
        )
        async with db_session_factory() as session:
            await ProjectRepository(session).create(peer_project)
            await session.commit()

        # Sanity: cache doesn't know about it yet.
        assert started_proxy_manager.get_project(peer_project.id) is None

        # Publish the peer's invalidation message.
        payload = json.dumps(
            {
                "signal": "project-changed",
                "instance_id": "peer-instance",
                "entity_id": peer_project.id,
            }
        )
        await redis_client.client.publish(EVENT_CHANNEL, payload)

        # Cache should pick it up via the subscriber.
        assert await _wait_until(
            lambda: started_proxy_manager.get_project(peer_project.id) is not None
        )

    async def test_self_echoes_are_dropped(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        # A message tagged with our own instance_id should be ignored even
        # if the entity id is bogus - no exception should be raised.
        my_id = started_proxy_manager._settings.instance_id
        payload = json.dumps(
            {
                "signal": "project-changed",
                "instance_id": my_id,
                "entity_id": "nonexistent",
            }
        )
        await redis_client.client.publish(EVENT_CHANNEL, payload)
        await asyncio.sleep(0.2)
        # No project added, no exception
        assert started_proxy_manager.get_project("nonexistent") is None


class TestCrossInstanceProxyReload:
    """A peer instance's proxy_changed event refreshes status from Redis."""

    async def test_peer_status_change_propagates_via_redis(
        self,
        started_proxy_manager: ProxyManager,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        from api.db.repository import (
            ConnectorRepository,
            CredentialRepository,
            ProxyRepository,
        )
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus

        project = Project(name="Status Project", username="statususer", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="status.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        # Drive the writes through our manager so events fire normally.
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        # Simulate a peer instance: update the proxy row directly in Postgres
        # (e.g., its host changed), then publish the proxy-changed event.
        proxy.host = "peer-updated.example.com"
        async with db_session_factory() as session:
            await ProxyRepository(session).update(proxy)
            await session.commit()

        # Also update Redis status as a peer health-check would.
        await redis_client.set_proxy_status(
            proxy.id, ProxyStatus.UNHEALTHY, latency_ms=999.0, consecutive_failures=5
        )

        # Now publish the cross-instance event tagged as a peer.
        payload = json.dumps(
            {
                "signal": "proxy-changed",
                "instance_id": "peer-instance",
                "entity_id": proxy.id,
            }
        )
        await redis_client.client.publish(EVENT_CHANNEL, payload)

        # Wait for cache to reflect both the Postgres update and the
        # Redis status hydration.
        async def _ready() -> bool:
            cached = started_proxy_manager.get_proxy(proxy.id)
            return (
                cached is not None
                and cached.host == "peer-updated.example.com"
                and cached.status == ProxyStatus.UNHEALTHY
            )

        assert await _wait_until(_ready)
        # Silence unused-import warnings on these models in some linters.
        _ = (ConnectorRepository, CredentialRepository)


class TestCrossInstanceStatusOnlyEvents:
    """A peer's health flip is applied from Redis, without touching Postgres.

    ``update_proxy_status`` publishes ``proxy_changed`` with ``op="status"``
    because the proxies row is untouched - only the Redis status hash moved.
    Health flips are the most frequent cross-instance event on an otherwise
    idle install, so making each peer read the database to apply one is the
    difference between a Redis GET and a Postgres round-trip per flap.
    """

    async def test_status_op_skips_the_postgres_read(
        self,
        started_proxy_manager: ProxyManager,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        from api.db.repository import ProxyRepository
        from api.models.proxy import ProxyStatus

        proxy = await _seed_proxy(started_proxy_manager, "statusonly")

        # Change the row in Postgres as well. Nothing should pick this up: it
        # is the tell that the handler went to the database, and a status
        # event promises it did not have to. Written through a copy because
        # the manager caches the very object we were handed.
        async with db_session_factory() as session:
            await ProxyRepository(session).update(
                proxy.model_copy(update={"host": "must-not-be-read.example.com"})
            )
            await session.commit()

        await redis_client.set_proxy_status(
            proxy.id, ProxyStatus.DEGRADED, latency_ms=42.0, consecutive_failures=1
        )
        await redis_client.client.publish(
            EVENT_CHANNEL,
            json.dumps(
                {
                    "signal": "proxy-changed",
                    "instance_id": "peer-instance",
                    "entity_id": proxy.id,
                    "op": "status",
                }
            ),
        )

        cached = started_proxy_manager.get_proxy(proxy.id)
        assert await _wait_until(
            lambda: cached is not None and cached.status == ProxyStatus.DEGRADED
        )
        assert cached is not None
        assert cached.last_check_latency_ms == 42.0
        assert cached.consecutive_failures == 1
        # The definition is whatever we had, not what Postgres now holds.
        assert cached.host == "statusonly.example.com"

    async def test_status_op_for_an_unknown_proxy_falls_back_to_a_full_reload(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        """A peer can flip a proxy this instance has never cached."""
        from api.models.proxy import ProxyStatus

        proxy = await _seed_proxy(started_proxy_manager, "uncached")
        await redis_client.set_proxy_status(
            proxy.id, ProxyStatus.UNHEALTHY, latency_ms=0.0, consecutive_failures=3
        )
        # Simulate an instance that has not seen this proxy yet - its own
        # "added" event was dropped, say. There is no definition to patch, so
        # the status path must not silently do nothing.
        started_proxy_manager._proxies.pop(proxy.id)

        await redis_client.client.publish(
            EVENT_CHANNEL,
            json.dumps(
                {
                    "signal": "proxy-changed",
                    "instance_id": "peer-instance",
                    "entity_id": proxy.id,
                    "op": "status",
                }
            ),
        )

        async def _ready() -> bool:
            cached = started_proxy_manager.get_proxy(proxy.id)
            return cached is not None and cached.status == ProxyStatus.UNHEALTHY

        assert await _wait_until(_ready)
        cached = started_proxy_manager.get_proxy(proxy.id)
        assert cached is not None and cached.host == "uncached.example.com"


class TestReloadPreservesRuntimeState:
    """Guard against the bug where reload paths zeroed counters / status.

    Both ``reload_proxy`` (fired by cross-instance ``proxy_changed``) and
    the periodic ``full_reload`` used to construct a fresh ``Proxy`` from
    Postgres and replace the cached entry. The fresh entry's defaults
    (``status=UNKNOWN``, ``request_count=0``) clobbered the live runtime
    state, causing 502s and UI counter flapping.
    """

    async def test_reload_proxy_preserves_counters_and_status(
        self,
        started_proxy_manager: ProxyManager,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from api.db.repository import ProxyRepository
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus

        project = Project(name="Counters Project", username="cprx-1", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="counters.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        # Simulate runtime activity: status set by health checker, plus
        # some per-request increments that only live in memory.
        await started_proxy_manager.update_proxy_status(
            proxy.id, ProxyStatus.HEALTHY, latency_ms=42.0, consecutive_failures=0
        )
        cached = started_proxy_manager.get_proxy(proxy.id)
        assert cached is not None
        cached.request_count = 17
        cached.success_count = 15
        cached.failure_count = 2
        cached.avg_latency_ms = 100.0

        # Touch a Postgres-backed field directly and trigger reload.
        # If reload regresses to the old behaviour it'll replace the
        # cached Proxy with a fresh one whose counters are 0 and whose
        # status defaults to UNKNOWN.
        async with db_session_factory() as session:
            db_proxy = await ProxyRepository(session).get_by_id(proxy.id)
            assert db_proxy is not None
            db_proxy.host = "renamed.example.com"
            await ProxyRepository(session).update(db_proxy)
            await session.commit()

        await started_proxy_manager.reload_proxy(proxy.id)

        after = started_proxy_manager.get_proxy(proxy.id)
        assert after is not None
        # Definition field picked up from Postgres
        assert after.host == "renamed.example.com"
        # Status preserved (came from Redis via the reload)
        assert after.status == ProxyStatus.HEALTHY
        # Counters preserved (would have been zeroed by the old code)
        assert after.request_count == 17
        assert after.success_count == 15
        assert after.failure_count == 2

    async def test_full_reload_preserves_status_and_converges_counters_from_redis(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        """Status survives full_reload; counters converge to the Redis total.

        full_reload reloads definitions from Postgres and then runs
        ``_hydrate_from_redis`` so the in-memory counters match the
        cross-instance Redis-backed total. This is the right behaviour
        for a cluster: everyone re-converges every 60s. The earlier bug
        was that the proxy entity was REPLACED, momentarily zeroing
        status (default UNKNOWN) before any re-hydration could happen.
        """
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus

        project = Project(name="FR Project", username="frprx-1", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="fullreload.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        await started_proxy_manager.update_proxy_status(
            proxy.id, ProxyStatus.HEALTHY, latency_ms=12.0, consecutive_failures=0
        )

        # Simulate a handful of completed requests writing to Redis as
        # ``_handle_request_stats`` does in production.
        for _ in range(5):
            await redis_client.update_proxy_metrics(
                proxy.id, success=True, latency_ms=50.0, bytes_sent=10, bytes_received=20
            )

        await started_proxy_manager.full_reload()

        after = started_proxy_manager.get_proxy(proxy.id)
        assert after is not None
        # Status was set in Redis and must survive the reload.
        assert after.status == ProxyStatus.HEALTHY
        # Counters now reflect the Redis-backed total - definitely not 0.
        assert after.request_count == 5
        assert after.success_count == 5


class TestCrossInstanceMetricDeltas:
    """Peer-published metric deltas land in in-memory counters.

    The hot path no longer writes Redis per request - instead each
    instance accumulates deltas locally, flushes every few seconds in
    one Redis pipeline, and announces the deltas on the
    ``METRIC_DELTAS_CHANNEL`` Pub/Sub channel. The subscriber on every
    other instance folds the deltas into its in-memory ``Proxy`` /
    ``Project`` so the UI sees cluster-wide totals without polling
    Redis.
    """

    async def test_peer_delta_updates_local_counters(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol

        project = Project(name="Delta Project", username="dlt-1", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="delta.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        # Sanity: counters start at 0 on this instance.
        cached = started_proxy_manager.get_proxy(proxy.id)
        assert cached is not None and cached.request_count == 0

        # Simulate a peer flushing deltas (3 successful requests on its side).
        payload = json.dumps(
            {
                "instance_id": "peer-instance",
                "proxy_deltas": {
                    proxy.id: {
                        "request_count": 3,
                        "success_count": 3,
                        "failure_count": 0,
                        "latency_sum_ms": 90.0,
                        "bytes_sent": 100,
                        "bytes_received": 200,
                    }
                },
                "project_deltas": {
                    project.id: {
                        "request_count": 3,
                        "success_count": 3,
                        "failure_count": 0,
                        "latency_sum_ms": 90.0,
                        "bytes_sent": 100,
                        "bytes_received": 200,
                    }
                },
            }
        )
        await redis_client.client.publish(METRIC_DELTAS_CHANNEL, payload)

        async def _ready() -> bool:
            p = started_proxy_manager.get_proxy(proxy.id)
            pr = started_proxy_manager.get_project(project.id)
            return (
                p is not None and p.request_count == 3 and p.success_count == 3
                and pr is not None and pr.request_count == 3
            )

        assert await _wait_until(_ready)

        # Weighted-average latency from the peer's deltas.
        final = started_proxy_manager.get_proxy(proxy.id)
        assert final is not None
        assert final.avg_latency_ms == pytest.approx(30.0)
        assert final.bytes_sent == 100
        assert final.bytes_received == 200

    async def test_self_echoes_are_ignored(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        my_id = started_proxy_manager._settings.instance_id
        # If we accidentally applied our own published delta we'd double-count.
        payload = json.dumps(
            {
                "instance_id": my_id,
                "proxy_deltas": {
                    "doesnt-exist": {
                        "request_count": 999,
                        "success_count": 999,
                        "failure_count": 0,
                        "latency_sum_ms": 0.0,
                        "bytes_sent": 0,
                        "bytes_received": 0,
                    }
                },
                "project_deltas": {},
            }
        )
        await redis_client.client.publish(METRIC_DELTAS_CHANNEL, payload)
        await asyncio.sleep(0.2)
        # No proxy with that id; in-memory caches are unaffected.
        assert started_proxy_manager.get_proxy("doesnt-exist") is None

    async def test_flush_loop_does_not_double_apply_locally(
        self,
        started_proxy_manager: ProxyManager,
    ) -> None:
        """``_flush_pending_metrics`` applies the delta to in-memory AND
        publishes the same delta on the channel. The publisher's own
        subscriber receives the message too (Redis Pub/Sub is
        broadcast), but must drop it by ``instance_id`` to avoid
        double-counting locally.
        """
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol

        project = Project(name="Self-echo Project", username="se-1", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="se.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        # Drive 7 request completions, then flush.
        for _ in range(7):
            await started_proxy_manager._handle_request_stats(
                proxy_id=proxy.id, project_id=project.id,
                success=True, latency_ms=10.0,
                bytes_sent=1, bytes_received=2,
            )
        await started_proxy_manager._flush_pending_metrics()

        # Local apply happened inside flush. The subsequent Pub/Sub
        # round-trip - which the publisher's own subscriber also
        # receives - must NOT bump the counters a second time. Give
        # the subscriber loop comfortably more time than a self-echo
        # would take to land.
        await asyncio.sleep(0.5)
        cached = started_proxy_manager.get_proxy(proxy.id)
        assert cached is not None
        assert cached.request_count == 7
        assert cached.success_count == 7
        # If self-echo had been applied we'd see 14 here.
        cached_project = started_proxy_manager.get_project(project.id)
        assert cached_project is not None
        assert cached_project.request_count == 7

        # And no leftover pending - the flush drained everything.
        assert proxy.id not in started_proxy_manager._pending_proxy_deltas
        assert project.id not in started_proxy_manager._pending_project_deltas

    async def test_handle_request_stats_accumulates_pending_delta(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        """The hot path neither writes Redis nor bumps in-memory counters.

        Per-request handlers only accumulate into the pending delta -
        in-memory counters and Redis are both updated at the next
        ``_flush_pending_metrics`` tick, in the same code path peers
        run when their subscribers fire. This is what keeps the
        cluster-wide view consistent: every instance applies the same
        delta at the same logical moment.
        """
        from api.models.connector import Connector
        from api.models.credential import Credential, CredentialType
        from api.models.proxy import Proxy, ProxyProtocol

        project = Project(name="Pending Project", username="pnd-1", password="p")
        credential = Credential(
            name="Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        connector = Connector(
            name="Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        proxy = Proxy(
            host="pending.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await started_proxy_manager.add_project(project)
        await started_proxy_manager.add_credential(credential)
        await started_proxy_manager.add_connector(connector)
        await started_proxy_manager.add_proxy(proxy)

        # Drive 4 request completions through the internal handler.
        for _ in range(4):
            await started_proxy_manager._handle_request_stats(
                proxy_id=proxy.id,
                project_id=project.id,
                success=True,
                latency_ms=50.0,
                bytes_sent=10,
                bytes_received=20,
            )

        # Hot path didn't touch in-memory - peers must see the same
        # values, so the local instance can't be ahead.
        cached = started_proxy_manager.get_proxy(proxy.id)
        assert cached is not None
        assert cached.request_count == 0

        # Pending delta carries the accumulated batch - no Redis write yet.
        pending = started_proxy_manager._pending_proxy_deltas[proxy.id]
        assert pending.request_count == 4
        assert pending.success_count == 4
        from api.db.redis import PROXY_METRICS_KEY
        ttl_or_data = await redis_client.client.hgetall(PROXY_METRICS_KEY.format(proxy_id=proxy.id))
        # Redis untouched on the hot path. Whatever's there came from
        # prior unrelated activity (empty in a clean test).
        assert not ttl_or_data or int(ttl_or_data.get("request_count", "0")) == 0

        # Flush drains pending → Redis HINCRBY → applies locally → publishes.
        await started_proxy_manager._flush_pending_metrics()
        assert proxy.id not in started_proxy_manager._pending_proxy_deltas
        data = await redis_client.client.hgetall(PROXY_METRICS_KEY.format(proxy_id=proxy.id))
        assert int(data["request_count"]) == 4
        assert int(data["success_count"]) == 4
        cached_after = started_proxy_manager.get_proxy(proxy.id)
        assert cached_after is not None
        assert cached_after.request_count == 4
        assert cached_after.success_count == 4


class TestAdditiveHandlers:
    """A subsystem's handler for a signal the manager handles itself runs after the built-in one."""

    async def test_extra_project_handler_runs_alongside_the_eviction(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        from api.core.signals import project_changed

        seen: list[tuple[str, str | None]] = []

        async def extra(entity_id: str, op: str | None) -> None:
            seen.append((entity_id, op))

        manager = ProxyManager(
            session_factory=db_session_factory,
            redis_client=redis_client,
            settings=test_settings,
            cross_instance_handlers={project_changed: extra},
        )
        await manager.start()
        try:
            await asyncio.sleep(0.2)
            project = Project(name="Doomed", username="doomed", password="pw")
            manager._projects[project.id] = project
            payload = json.dumps(
                {"signal": project_changed.name, "instance_id": "peer", "entity_id": project.id, "op": "removed"}
            )
            await redis_client.client.publish(EVENT_CHANNEL, payload)
            assert await _wait_until(lambda: seen == [(project.id, "removed")])
            # The manager's own handler still ran: the project is gone from its cache.
            assert manager.get_project(project.id) is None
        finally:
            await manager.stop()


class TestTunnelPeerDeltas:
    """Tunnel devices ride the same pipeline: pending delta, Redis hash, pub/sub, running totals."""

    async def test_device_requests_flush_and_fold_into_totals(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        from api.core.signals import tunnel_encrypted_dns_blocked, tunnel_name_unresolved
        from api.core.stats import TunnelPeerMetricDelta
        from api.db.redis import TUNNEL_PEER_METRICS_KEY

        manager = started_proxy_manager
        # A request from a device of a project this instance may not even
        # cache: the device is counted regardless of the proxy lookup.
        await manager._handle_request_stats(
            proxy_id="unknown", project_id="unknown", success=True, latency_ms=50.0,
            bytes_sent=10, bytes_received=20, peer_id="device-1",
        )
        manager._record_traffic_progress("unknown", "unknown", "unknown", "device-1", 5, 5)
        await tunnel_name_unresolved.send_async(None, peer_id="device-1")
        await tunnel_encrypted_dns_blocked.send_async(None, peer_id="device-1")
        await tunnel_encrypted_dns_blocked.send_async(None, peer_id="device-1")
        pending = manager._pending_tunnel_peer_deltas["device-1"]
        assert pending == TunnelPeerMetricDelta(
            request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=15, bytes_received=25,
            by_address=1, encrypted_dns_blocked=2,
        )
        assert manager.tunnel_peer_metrics("device-1") == TunnelPeerMetricDelta()

        await manager._flush_pending_metrics()
        assert "device-1" not in manager._pending_tunnel_peer_deltas
        data = await redis_client.client.hgetall(TUNNEL_PEER_METRICS_KEY.format(peer_id="device-1"))
        assert int(data["request_count"]) == 1 and int(data["by_address"]) == 1
        assert int(data["encrypted_dns_blocked"]) == 2 and int(data["bytes_sent"]) == 15
        assert await redis_client.get_tunnel_peer_metrics("device-1") == pending
        assert manager.tunnel_peer_metrics("device-1") == pending

        # A peer instance's flush lands in the same totals.
        payload = json.dumps({
            "instance_id": "peer-instance",
            "proxy_deltas": {},
            "project_deltas": {},
            "tunnel_peer_deltas": {"device-1": {"request_count": 2, "failure_count": 2, "latency_sum_ms": 10.0, "by_address": 4}},
        })
        await redis_client.client.publish(METRIC_DELTAS_CHANNEL, payload)
        assert await _wait_until(lambda: manager.tunnel_peer_metrics("device-1").request_count == 3)
        totals = manager.tunnel_peer_metrics("device-1")
        assert (totals.failure_count, totals.by_address, totals.encrypted_dns_blocked) == (2, 5, 2)
        assert totals.avg_latency_ms == pytest.approx(20.0)

        # Hydration rebuilds the totals from Postgres history plus the Redis window.
        await manager._hydrate_from_redis()
        assert manager.tunnel_peer_metrics("device-1") == pending
        await redis_client.reset_tunnel_peer_metrics("device-1")


class TestHostDeltas:
    """Destination hosts ride the pipeline too: pending delta per (project, connector, host), then a Redis hash."""

    async def test_requests_and_progress_count_on_the_host_under_the_connector(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        from api.core.stats import MetricDelta
        from api.db.redis import HOST_METRICS_KEY

        manager = started_proxy_manager
        proxy = await _seed_proxy(manager, "hosts")
        connector_id = proxy.connector_id
        project_id = manager.get_connector(connector_id).project_id  # type: ignore[union-attr]

        await manager._handle_request_stats(
            proxy_id=proxy.id, project_id=project_id, success=True, latency_ms=50.0,
            bytes_sent=10, bytes_received=20, target_host="Shop.Example.com.",
        )
        # A running transfer's progress lands on the same host row; a request
        # with no known destination counts on nothing host-wise.
        manager._record_traffic_progress(proxy.id, project_id, connector_id, None, 5, 5, "shop.example.com")
        await manager._handle_request_stats(
            proxy_id=proxy.id, project_id=project_id, success=False, latency_ms=10.0,
            bytes_sent=1, bytes_received=1, target_host=None,
        )
        key = (project_id, connector_id, "shop.example.com")
        assert manager._pending_host_deltas == {
            key: MetricDelta(request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=15, bytes_received=25),
        }
        # Proxy, connector and project saw both requests regardless.
        assert manager._pending_connector_deltas[connector_id].request_count == 2

        await manager._flush_pending_metrics()
        assert manager._pending_host_deltas == {}
        data = await redis_client.client.hgetall(
            HOST_METRICS_KEY.format(project_id=project_id, connector_id=connector_id, host="shop.example.com")
        )
        assert int(data["request_count"]) == 1 and int(data["bytes_sent"]) == 15
        assert await redis_client.get_all_host_metrics() == {
            key: MetricDelta(request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=15, bytes_received=25),
        }

    async def test_every_host_is_named_by_default(
        self,
        started_proxy_manager: ProxyManager,
    ) -> None:
        manager = started_proxy_manager
        assert manager._settings.host_metrics_max_hosts == 0
        proxy = await _seed_proxy(manager, "all")
        project_id = manager.get_connector(proxy.connector_id).project_id  # type: ignore[union-attr]
        for i in range(50):
            await manager._handle_request_stats(
                proxy_id=proxy.id, project_id=project_id, success=True, latency_ms=1.0,
                bytes_sent=0, bytes_received=0, target_host=f"h{i}.example.com",
            )
        assert len(manager._fold_host_tail(manager._pending_host_deltas)) == 50

    async def test_a_ceiling_keeps_the_busiest_hosts_and_folds_the_rest(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from api.core.stats import HOST_OVERFLOW

        manager = started_proxy_manager
        monkeypatch.setattr(manager._settings, "host_metrics_max_hosts", 2)
        proxy = await _seed_proxy(manager, "capped")
        connector_id = proxy.connector_id
        project_id = manager.get_connector(connector_id).project_id  # type: ignore[union-attr]
        # Arrival order is not rank: the one-off hosts come first, the busy ones after.
        for host in ["a.example.com", "b.example.com", "c.example.com", "d.example.com", "d.example.com", "e.example.com", "e.example.com", "e.example.com"]:
            await manager._handle_request_stats(
                proxy_id=proxy.id, project_id=project_id, success=True, latency_ms=1.0,
                bytes_sent=1, bytes_received=1, target_host=host,
            )
        # Nothing is folded on the hot path.
        assert len(manager._pending_host_deltas) == 5

        await manager._flush_pending_metrics()
        flushed = await redis_client.get_all_host_metrics()
        counts = {host: d.request_count for (_p, _c, host), d in flushed.items()}
        assert counts == {"e.example.com": 3, "d.example.com": 2, HOST_OVERFLOW: 3}
        assert flushed[(project_id, connector_id, HOST_OVERFLOW)].bytes_sent == 3

    async def test_the_ceiling_is_per_project_and_keeps_connectors_apart(
        self,
        started_proxy_manager: ProxyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from api.core.stats import HOST_OVERFLOW, MetricDelta

        manager = started_proxy_manager
        monkeypatch.setattr(manager._settings, "host_metrics_max_hosts", 1)
        one = MetricDelta(request_count=1, success_count=1)
        pending = {
            # Project P: "big" is spread over two connectors (3 requests) and beats "small" (2 on one).
            ("P", "c1", "big.example.com"): MetricDelta(request_count=1, success_count=1),
            ("P", "c2", "big.example.com"): MetricDelta(request_count=2, success_count=2),
            ("P", "c1", "small.example.com"): MetricDelta(request_count=2, success_count=2),
            ("P", "c2", "tiny.example.com"): one,
            # Project Q is ranked on its own.
            ("Q", "c3", "only.example.com"): one,
        }
        folded = manager._fold_host_tail(pending)
        assert {k: d.request_count for k, d in folded.items()} == {
            ("P", "c1", "big.example.com"): 1,
            ("P", "c2", "big.example.com"): 2,
            ("P", "c1", HOST_OVERFLOW): 2,
            ("P", "c2", HOST_OVERFLOW): 1,
            ("Q", "c3", "only.example.com"): 1,
        }

    async def test_disabled_counts_nothing(
        self,
        started_proxy_manager: ProxyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = started_proxy_manager
        monkeypatch.setattr(manager._settings, "host_metrics_enabled", False)
        proxy = await _seed_proxy(manager, "off")
        project_id = manager.get_connector(proxy.connector_id).project_id  # type: ignore[union-attr]
        await manager._handle_request_stats(
            proxy_id=proxy.id, project_id=project_id, success=True, latency_ms=1.0,
            bytes_sent=0, bytes_received=0, target_host="a.example.com",
        )
        assert manager._pending_host_deltas == {}
        assert manager._pending_connector_deltas[proxy.connector_id].request_count == 1

    async def test_removing_the_connector_clears_its_host_hashes_and_deltas(
        self,
        started_proxy_manager: ProxyManager,
        redis_client: RedisClient,
    ) -> None:
        manager = started_proxy_manager
        proxy = await _seed_proxy(manager, "cleanup")
        other = await _seed_proxy(manager, "kept")
        for p in (proxy, other):
            project_id = manager.get_connector(p.connector_id).project_id  # type: ignore[union-attr]
            await manager._handle_request_stats(
                proxy_id=p.id, project_id=project_id, success=True, latency_ms=1.0,
                bytes_sent=0, bytes_received=0, target_host="a.example.com",
            )
        await manager._flush_pending_metrics()
        assert len(await redis_client.get_all_host_metrics()) == 2
        # One more, un-flushed, that must not come back after the removal.
        project_id = manager.get_connector(proxy.connector_id).project_id  # type: ignore[union-attr]
        await manager._handle_request_stats(
            proxy_id=proxy.id, project_id=project_id, success=True, latency_ms=1.0,
            bytes_sent=0, bytes_received=0, target_host="b.example.com",
        )

        assert await manager.remove_connector(proxy.connector_id) is True
        remaining = await redis_client.get_all_host_metrics()
        assert [c for (_p, c, _h) in remaining] == [other.connector_id]
        assert all(c != proxy.connector_id for (_p, c, _h) in manager._pending_host_deltas)

        kept_project = manager.get_connector(other.connector_id).project_id  # type: ignore[union-attr]
        assert await manager.remove_project(kept_project) is True
        assert await redis_client.get_all_host_metrics() == {}
