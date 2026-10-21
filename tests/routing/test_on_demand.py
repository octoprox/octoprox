# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""On-demand country slot groups across roles: gateways ask, a worker provisions, the feed brings the group.

Everything is real but the loops between the roles, which :class:`Cluster` stands in for: it runs the ``provision``
entries on their job stream as a worker's runner does, compiles, and applies the new snapshot on every gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.roles import JobKind
from api.db.redis import JOB_STREAM_KEY, RedisClient
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.streams import ChangeLog, StreamEntry
from api.feed.compiler import FullFeedCompiler
from api.geo.models import GeoSettings
from api.geo.settings import GeoSettingsStore
from api.jobs.job import JobBatch
from api.jobs.provision import ProvisionJob
from api.jobs.queue import JobQueue
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.providers.registry import ProviderRegistry, build_registry
from api.providers.sdk.strategies import META_GEO_COUNTRY, META_SESSION_ID
from api.providers.store import ProviderStore
from api.routing.ondemand import FeedOnDemand
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.services.proxies import ProxyService
from tests.conftest import queued_job_keys
from tests.waits import until

DE = LocationTarget(country="DE")


class _NoJobs(JobQueue):
    """A job queue for waits alone: nothing may be published on it."""

    async def publish(self, kind: JobKind, keys: Iterable[str]) -> int:
        raise AssertionError("no job was expected")


class _Requests(JobQueue):
    """The job queue as the gateways reach it, remembering every key they published."""

    def __init__(self, redis_client: RedisClient) -> None:
        super().__init__(redis_client)
        self.keys: list[str] = []

    async def publish(self, kind: JobKind, keys: Iterable[str]) -> int:
        published = list(keys)
        count = await super().publish(kind, published)
        # Once Redis holds them, so a worker run after a wait for the keys finds them all.
        self.keys += published
        return count


@dataclass
class Gateway:
    router: Router
    on_demand: FeedOnDemand


class Cluster:
    """Postgres, Redis, a worker's provision job and the controller's compile; the loops between them by hand."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        registry: ProviderRegistry,
        changes: ChangeLog,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.registry = registry
        self.requests = _Requests(redis_client)
        self.proxies = ProxyService(session_factory, JobQueue(redis_client), redis_client, registry, changes=changes)
        self.job = ProvisionJob(session_factory, registry, self.proxies)
        self.compiler = FullFeedCompiler(
            session_factory,
            registry,
            descriptor_sink=ProviderStore(registry, None),
            geo_settings_sink=GeoSettingsStore(settings, None),
            geo_defaults=GeoSettings(),
            default_strategy=settings.default_strategy,
        )
        self.gateways: list[Gateway] = []
        # Provision jobs the worker ran.
        self.runs = 0

    async def gateway(self) -> Gateway:
        """A gateway that has applied the feed as it stands."""
        on_demand = FeedOnDemand(self.requests, self.redis_client)
        gateway = Gateway(Router(await self._snapshot(None), on_demand=on_demand), on_demand)
        await on_demand.applied(gateway.router.snapshot)
        self.gateways.append(gateway)
        return gateway

    async def work(self) -> None:
        """Run the ``provision`` entries on their stream a batch of one each, as a worker's runner does, then deliver."""
        stream = JOB_STREAM_KEY.format(kind=JobKind.PROVISION)
        for entry_id, fields in await self.redis_client.client.xrange(stream):
            await self.job.run(JobBatch.of([StreamEntry(entry_id, fields)]))
            await self.redis_client.client.xdel(stream, entry_id)
            self.runs += 1
        await self.deliver()

    async def deliver(self) -> None:
        """Compile, and apply the snapshot on every gateway as its follower would."""
        for gateway in self.gateways:
            snapshot = await self._snapshot(gateway.router.snapshot)
            gateway.router.swap(snapshot)
            await gateway.on_demand.applied(snapshot)

    async def stored_group(self, country: str) -> list[Proxy]:
        """The connector's rows Postgres holds for ``country``'s slot group."""
        async with self.session_factory() as session:
            rows = await ProxyRepository(session).get_by_connector("conn-any")
        return [proxy for proxy in rows if proxy.metadata.get(META_GEO_COUNTRY) == country]

    async def _snapshot(self, previous: RoutingSnapshot | None) -> RoutingSnapshot:
        compiled = await self.compiler.compile(quarantined={}, blocked_until={}, changes=None)
        return RoutingSnapshot.from_sections(compiled.sections, registry=self.registry, previous=previous)


@pytest.fixture
async def cluster(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,  # empties the tables after the test
    redis_client: RedisClient,
    change_log: ChangeLog,
    test_settings: Settings,
) -> Cluster:
    """An "All countries" Oxylabs pool of two slots per country, holding two slots of no country."""
    async with db_session_factory() as session:
        await ProjectRepository(session).create(Project(id="project-1", name="P", username="p", password="pw"))
        await CredentialRepository(session).create(Credential(
            id="cred-oxy", name="Oxylabs", type="oxylabs", project_id="project-1",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ))
        await ConnectorRepository(session).create(Connector(
            id="conn-any", name="Oxy any", credential_id="cred-oxy", credential_type="oxylabs",
            project_id="project-1", config={"num_proxies": 2, "session_duration_minutes": 10},
        ))
        for sid in ("aaa", "bbb"):
            await ProxyRepository(session).create(Proxy(
                id=f"px-{sid}", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
                username=f"customer-alice-sessid-{sid}-sesstime-10", password="{password}",
                connector_id="conn-any", status=ProxyStatus.HEALTHY,
                metadata={"provider": "oxylabs", "proxy_type": "residential", "session_id": sid},
            ))
        await session.commit()
    return Cluster(db_session_factory, redis_client, build_registry(include_plugins=False), change_log, test_settings)


def _empty_snapshot() -> RoutingSnapshot:
    return RoutingSnapshot.from_sections({}, registry=ProviderRegistry())


class TestFeedOnDemand:
    async def test_a_request_publishes_one_provision_entry_for_the_group(self, redis_client: RedisClient) -> None:
        on_demand = FeedOnDemand(JobQueue(redis_client), redis_client)

        await on_demand.request("conn-any", "de")

        assert await queued_job_keys(redis_client, JobKind.PROVISION) == ["conn-any:DE"]
        # A second ask, a wait later: an entry of its own, which the worker finds served.
        await on_demand.request("conn-any", "DE")
        assert await queued_job_keys(redis_client, JobKind.PROVISION) == ["conn-any:DE", "conn-any:DE"]

    async def test_a_redis_that_does_not_answer_fails_the_request_at_its_deadline(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """The requests waiting on the ask are refused in time, rather than held for the client's reply bound."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        try:
            on_demand = FeedOnDemand(JobQueue(client), client)
            await client.client.ping()
            await redis_client.client.client_pause(1500, all=True)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(TimeoutError):
                await on_demand.request("conn-any", "DE")
            assert loop.time() - started < 0.6
        finally:
            await client.close()

    async def test_a_wait_ends_with_the_first_applied_snapshot_that_satisfies_it(
        self, redis_client: RedisClient
    ) -> None:
        on_demand = FeedOnDemand(_NoJobs(redis_client), redis_client)
        older, newer = _empty_snapshot(), _empty_snapshot()
        waiting = asyncio.create_task(on_demand.wait(lambda snapshot: snapshot is newer, 3.0))
        await on_demand.applied(older)
        await asyncio.sleep(0.01)
        assert not waiting.done()

        await on_demand.applied(newer)
        assert await asyncio.wait_for(waiting, 1.0) is True

    async def test_a_snapshot_applied_before_the_wait_is_not_waited_for(self, redis_client: RedisClient) -> None:
        on_demand = FeedOnDemand(_NoJobs(redis_client), redis_client)
        snapshot = _empty_snapshot()
        await on_demand.applied(snapshot)
        assert await on_demand.wait(lambda applied: applied is snapshot, 0.01) is True

    async def test_a_wait_nothing_satisfies_times_out(self, redis_client: RedisClient) -> None:
        on_demand = FeedOnDemand(_NoJobs(redis_client), redis_client)
        assert await on_demand.wait(lambda _applied: True, 0.05) is False
        await on_demand.applied(_empty_snapshot())
        assert await on_demand.wait(lambda _applied: False, 0.05) is False


class TestOnDemandCountryGroups:
    async def test_an_ask_cut_by_the_deadline_refuses_at_once_and_is_logged_by_its_type(
        self, cluster: Cluster, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type."""
        gateway = await cluster.gateway()
        monkeypatch.setattr(cluster.requests, "publish", AsyncMock(side_effect=TimeoutError))

        with structlog.testing.capture_logs() as logs:
            assert await gateway.router.select_proxy_for_project("project-1", location=DE) is None

        failed = [log for log in logs if log["event"] == "Country slot group could not be requested"]
        assert [(log["connector_id"], log["error"]) for log in failed] == [("conn-any", "TimeoutError")]

    async def test_first_request_waits_for_the_group_and_selects_from_it(self, cluster: Cluster) -> None:
        gateway = await cluster.gateway()
        assert gateway.router.get_routable_proxies_for_project("project-1", location=DE) == []

        request = asyncio.create_task(gateway.router.select_proxy_for_project("project-1", location=DE))
        await until(lambda: cluster.requests.keys == ["conn-any:DE"])
        assert not request.done()
        await cluster.work()
        selected = await asyncio.wait_for(request, 3.0)

        assert selected is not None
        assert selected.metadata[META_GEO_COUNTRY] == "DE"
        assert selected.username is not None
        assert selected.username.startswith("customer-alice-cc-DE-sessid-")
        assert selected.username.endswith("-sesstime-10")
        assert selected.password == "s3cret"  # the group arrives through the feed with its credentials resolved
        group = await cluster.stored_group("DE")
        assert len(group) == 2  # num_proxies is per country
        assert all(proxy.status == ProxyStatus.HEALTHY for proxy in group)
        # The group is routable now: the next request neither asks nor waits.
        assert await gateway.router.select_proxy_for_project("project-1", location=DE) is not None
        assert cluster.requests.keys == ["conn-any:DE"]

    async def test_misses_on_two_gateways_give_one_group(self, cluster: Cluster) -> None:
        first, second = await cluster.gateway(), await cluster.gateway()

        requests = [
            asyncio.create_task(gateway.router.select_proxy_for_project("project-1", location=DE))
            for gateway in (first, second)
        ]
        await until(lambda: len(cluster.requests.keys) == 2)
        await cluster.work()
        selected = await asyncio.wait_for(asyncio.gather(*requests), 3.0)

        # Each gateway asked; the first entry stored the group, and the second found it there.
        assert cluster.requests.keys == ["conn-any:DE", "conn-any:DE"]
        assert cluster.runs == 2
        assert all(proxy is not None and proxy.metadata[META_GEO_COUNTRY] == "DE" for proxy in selected)
        group = await cluster.stored_group("DE")
        assert len(group) == 2
        # A late ask (a gateway that missed before the feed reached it) finds the group and adds none.
        await cluster.requests.publish(JobKind.PROVISION, ["conn-any:DE"])
        await cluster.work()
        assert cluster.runs == 3
        assert {proxy.id for proxy in await cluster.stored_group("DE")} == {proxy.id for proxy in group}

    async def test_a_group_rotated_away_comes_back_with_fresh_sessions(self, cluster: Cluster) -> None:
        gateway = await cluster.gateway()
        request = asyncio.create_task(gateway.router.select_proxy_for_project("project-1", location=DE))
        await until(lambda: len(cluster.requests.keys) == 1)
        await cluster.work()
        assert await asyncio.wait_for(request, 3.0) is not None
        first_group = await cluster.stored_group("DE")

        # Rotation deletes refused slots through the proxy service; here it emptied the group.
        for proxy in first_group:
            assert await cluster.proxies.delete(proxy.id)
        await cluster.deliver()
        assert gateway.router.get_routable_proxies_for_project("project-1", location=DE) == []

        request = asyncio.create_task(gateway.router.select_proxy_for_project("project-1", location=DE))
        await until(lambda: len(cluster.requests.keys) == 2)
        await cluster.work()
        selected = await asyncio.wait_for(request, 3.0)

        second_group = await cluster.stored_group("DE")
        assert selected is not None and selected.id in {proxy.id for proxy in second_group}
        assert len(second_group) == 2
        sessions = {proxy.metadata[META_SESSION_ID] for proxy in second_group}
        assert sessions.isdisjoint(proxy.metadata[META_SESSION_ID] for proxy in first_group)
        assert {proxy.id for proxy in second_group}.isdisjoint(proxy.id for proxy in first_group)
