# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the scale job: scaling, rotation, draining and termination of a cloud connector's instances.

The job runs against the real Postgres and Redis, on a fake cloud account (:class:`FakeAccount`) the tests steer, with
the demand a test sets; runs of one connector that overlap run as concurrent tasks.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.demand_tracker import DEMAND_WINDOW_SECONDS, DemandLevel, DemandTracker
from api.core.stats import MetricDelta
from api.db.redis import (
    AUTOSCALER_STATE_KEY,
    PROXY_WINDOWS,
    SCALE_CREATING_KEY,
    TRAFFIC_BLOCKED_KEY,
    MetricWindows,
    RedisClient,
)
from api.db.repository import ConnectorRepository, MetricsRepository, ProxyRepository
from api.jobs.job import JobBatch
from api.jobs.scale import (
    CREATE_TIMEOUT_SECONDS,
    LISTING_ERROR_PREFIX,
    LISTING_INTERVAL_SECONDS,
    SCALING_COOLDOWN_SECONDS,
    AutoscaleJob,
    AutoscalerState,
    Creation,
    Drain,
)
from api.models.connector import MAX_ERROR_BACKOFF_MINUTES, CloudConnectorConfig, Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.cloud import CloudInstance, CloudProvider
from api.providers.registry import ProviderRegistry
from api.services.connectors import ConnectorService
from api.services.proxies import ProxyService
from tests.conftest import Clock
from tests.jobs.conftest import Rows

AWS_CONFIG: dict[str, Any] = {
    "region": "us-east-1",
    "instance_type": "t3.micro",
    "instance_name": "octo",
    "key_pair_name": "octo",
    "security_group": "sg-1",
    "min_proxies": 1,
    "max_proxies": 5,
    "min_rotation_period_minutes": 60,
    "max_rotation_period_minutes": 120,
}
# What a run that finds an instance with neither a row nor an entry logs, at warning.
UNRECORDED = "Terminating a cloud instance with no row"


@dataclass
class FakeInstance:
    """An instance of the fake account: whose it is, the ID its row has or would have, and whether it is gone."""

    connector_id: str
    proxy_id: str
    gone: bool = False


class FakeAccount:
    """A cloud account every run's :class:`FakeCloud` shares: its instances by name, what was asked of it, and what fails."""

    def __init__(self) -> None:
        self.instances: dict[str, FakeInstance] = {}
        # Every create's name and every termination's proxy ID, in call order, and how many listings were asked for.
        self.created: list[str] = []
        self.terminated: list[str] = []
        self.listings = 0
        self.create_error: Exception | None = None
        self.list_error: Exception | None = None
        self.terminate_error: Exception | None = None
        # The next create waits on this, once its instance boots, and sets holding meanwhile.
        self.held: asyncio.Event | None = None
        self.holding = asyncio.Event()
        # Run once as a listing starts, and once it is read before it is returned: what an overlapping run does meanwhile.
        self.before_listing: Callable[[], Awaitable[None]] | None = None
        self.after_listing: Callable[[], Awaitable[None]] | None = None
        # Run once as a create starts, before its instance exists to be listed.
        self.before_create: Callable[[], Awaitable[None]] | None = None

    def hold(self) -> asyncio.Event:
        """Hold the next create until the returned event is set; :meth:`held_create` waits for it to start."""
        self.holding.clear()
        self.held = asyncio.Event()
        return self.held

    async def held_create(self) -> None:
        """Wait until the held create starts, failing after 10 seconds rather than hanging when none comes."""
        async with asyncio.timeout(10):
            await self.holding.wait()

    def boot(self, connector: Connector, name: str) -> FakeInstance:
        """An instance of the connector that no row records yet, as a create whose run died leaves it."""
        instance = FakeInstance(connector.id, f"fake-{name}")
        self.instances[name] = instance
        return instance

    def proxy(self, connector: Connector, name: str) -> Proxy:
        """The row a create of the instance ``name`` saves, its ID derived from the name as a cloud's is."""
        return Proxy(
            id=f"fake-{name}", host=f"10.0.0.{len(self.instances) + 1}", port=3128, username="u", password="p",
            connector_id=connector.id, status=ProxyStatus.INITIALIZING,
        )


class FakeCloud(CloudProvider):
    """A connector's view of a :class:`FakeAccount`, made for each run as the job makes its cloud API."""

    NAME_LIMIT = 40
    ID_PREFIX = "fake-"
    ROW_TAG = "fake"
    KEY_FIELD = "name"

    def __init__(self, connector: Connector, credential: Credential, account: FakeAccount) -> None:
        super().__init__(connector, credential)
        self._config = connector.cloud_config
        self._account = account

    @property
    def place(self) -> str:
        return "here"

    async def create_instance(self, name: str) -> Proxy:
        account = self._account
        account.created.append(name)
        if account.create_error is not None:
            raise account.create_error
        if (before := account.before_create) is not None:
            account.before_create = None
            await before()
        account.boot(self.connector, name)
        if (held := account.held) is not None:
            account.held = None
            account.holding.set()
            await held.wait()
        # It answers as the cloud last reported the instance running, even if a termination landed since.
        return account.proxy(self.connector, name)

    async def tagged_instances(self) -> list[CloudInstance]:
        account = self._account
        account.listings += 1
        if (before := account.before_listing) is not None:
            account.before_listing = None
            await before()
        if account.list_error is not None:
            raise account.list_error
        listed = [
            CloudInstance(name, instance.proxy_id)
            for name, instance in account.instances.items()
            if not instance.gone and instance.connector_id == self.connector.id
        ]
        if (after := account.after_listing) is not None:
            account.after_listing = None
            await after()
        return listed

    async def terminate_instance(self, proxy_id: str) -> None:
        if self._account.terminate_error is not None:
            raise self._account.terminate_error
        self._account.terminated.append(proxy_id)
        for instance in self._account.instances.values():
            if instance.proxy_id == proxy_id:
                instance.gone = True


class Demand(DemandTracker):
    """The project's demand as the test sets it, and the previous levels the job passed."""

    def __init__(self, redis_client: RedisClient) -> None:
        super().__init__(redis_client)
        self.level = DemandLevel.LOW
        self.recent = True
        self.previous: list[DemandLevel | None] = []
        # Awaited as a run asks for the level, once it has read all it reads before its step: a barrier's wait, say.
        self.before_level: Callable[[], Awaitable[object]] | None = None

    async def get_demand_level(
        self, project_id: str, current_proxy_count: int, previous_level: DemandLevel | None = None
    ) -> DemandLevel:
        self.previous.append(previous_level)
        if self.before_level is not None:
            await self.before_level()
        return self.level

    async def has_recent_activity(self, project_id: str) -> bool:
        return self.recent


@dataclass
class Harness:
    job: AutoscaleJob
    account: FakeAccount
    demand: Demand

    async def run(self, *connectors: Connector) -> None:
        """One run of the job over a batch naming ``connectors``, as a runner hands it one."""
        keys = tuple(connector.id for connector in connectors)
        await self.job.run(JobBatch(ids=tuple(f"{n}-0" for n in range(1, len(keys) + 1)), keys=keys))


@pytest.fixture
def harness(
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    registry: ProviderRegistry,
    proxy_service: ProxyService,
    connector_service: ConnectorService,
) -> Harness:
    account, demand = FakeAccount(), Demand(redis_client)
    job = AutoscaleJob(
        sessions,
        redis_client,
        registry,
        proxy_service,
        connector_service,
        demand,
        cloud=lambda connector, credential: FakeCloud(connector, credential, account),
    )
    return Harness(job, account, demand)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


@pytest.fixture
async def aws(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project, "aws", config=AWS_CONFIG)


@pytest.fixture
async def emptiable(rows: Rows, project: Project) -> Connector:
    """A connector whose minimum is zero, so a run over it creates no instance of its own."""
    return await rows.connector(project, "aws", config={**AWS_CONFIG, "min_proxies": 0})


@pytest.fixture
async def doomed(rows: Rows, project: Project) -> Connector:
    """A cloud connector its user removed: disabled, and pending deletion until a run finds it holds nothing."""
    return await rows.connector(project, "aws", config=AWS_CONFIG, enabled=False, pending_deletion=True)


async def _state(redis_client: RedisClient, connector: Connector) -> AutoscalerState:
    fields = await redis_client.client.hgetall(AUTOSCALER_STATE_KEY.format(connector_id=connector.id))
    return AutoscalerState.from_fields(fields, now=-1.0)


async def _seed(redis_client: RedisClient, connector: Connector, state: AutoscalerState) -> None:
    await redis_client.client.hset(AUTOSCALER_STATE_KEY.format(connector_id=connector.id), mapping=state.to_fields())


async def _seasoned(redis_client: RedisClient, connector: Connector, **fields: Any) -> None:
    """State that has watched demand for longer than a window, so scaling in is allowed."""
    await _seed(redis_client, connector, AutoscalerState(since=time.time() - DEMAND_WINDOW_SECONDS - 1, **fields))


async def _an_hour_on(redis_client: RedisClient, connector: Connector) -> None:
    """Move the connector's last successful listing back by the listing interval and a second, as time passing would."""
    state = await _state(redis_client, connector)
    assert state.listed_at is not None
    state.listed_at -= LISTING_INTERVAL_SECONDS + 1
    await _seed(redis_client, connector, state)


async def _creating(redis_client: RedisClient, connector: Connector) -> dict[str, dict[str, Any]]:
    """The connector's creating hash, each entry's JSON read."""
    fields = await redis_client.client.hgetall(SCALE_CREATING_KEY.format(connector_id=connector.id))
    return {name: json.loads(raw) for name, raw in fields.items()}


async def _entry(redis_client: RedisClient, connector: Connector, name: str, purpose: str, age: float) -> None:
    """An entry of the creating hash, written ``age`` seconds ago."""
    value = json.dumps({"purpose": purpose, "at": time.time() - age})
    await redis_client.client.hset(SCALE_CREATING_KEY.format(connector_id=connector.id), name, value)


def _before_reading_entries(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: RedisClient,
    connector: Connector,
    meanwhile: Callable[[], Awaitable[None]],
) -> None:
    """Run ``meanwhile`` once, just before the job next reads the connector's creating hash."""
    key = SCALE_CREATING_KEY.format(connector_id=connector.id)
    hgetall = redis_client.client.hgetall
    pending = [meanwhile]

    async def reading(name: str) -> Any:
        if name == key and pending:
            await pending.pop()()
        return await hgetall(name)

    monkeypatch.setattr(redis_client.client, "hgetall", reading)


def _on_writing_entries(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: RedisClient,
    connector: Connector,
    meanwhile: Callable[[], Awaitable[None]],
    *,
    before: bool = False,
) -> None:
    """Run ``meanwhile`` once as the job next writes entries of the connector's creating hash, which its script does: just after the write, or just before it with ``before``."""
    key = SCALE_CREATING_KEY.format(connector_id=connector.id)
    evalsha = redis_client.client.evalsha
    pending = [meanwhile]

    async def writing(sha: str, numkeys: int, *keys_and_args: Any) -> Any:
        ours = bool(numkeys) and keys_and_args[0] == key
        if ours and before and pending:
            await pending.pop()()
        written = await evalsha(sha, numkeys, *keys_and_args)
        if ours and pending:
            await pending.pop()()
        return written

    monkeypatch.setattr(redis_client.client, "evalsha", writing)


async def _instances(
    rows: Rows, connector: Connector, count: int, status: ProxyStatus = ProxyStatus.HEALTHY
) -> list[Proxy]:
    return [await rows.proxy(connector, host=f"10.1.0.{i}", port=3128, status=status) for i in range(count)]


async def _statuses(rows: Rows, connector: Connector) -> list[ProxyStatus]:
    return [proxy.status for proxy in await rows.of(connector)]


async def _with_error(
    sessions: async_sessionmaker[AsyncSession], connector: Connector, errors: int, minutes_ago: float
) -> None:
    async with sessions() as session:
        await ConnectorRepository(session).update(connector.model_copy(update={
            "last_error": "Cloud provider error",
            "last_error_at": utc_now() - timedelta(minutes=minutes_ago),
            "consecutive_errors": errors,
        }))
        await session.commit()


async def _backoff_over(sessions: async_sessionmaker[AsyncSession], connector: Connector) -> None:
    """Move the connector's last cloud error back past the backoff ceiling, as time passing would; its text and count stay."""
    async with sessions() as session:
        stored = await ConnectorRepository(session).get_by_id(connector.id)
        assert stored is not None and stored.last_error_at is not None
        await ConnectorRepository(session).update(stored.model_copy(update={
            "last_error_at": utc_now() - timedelta(minutes=MAX_ERROR_BACKOFF_MINUTES + 1),
        }))
        await session.commit()


async def _no_error(rows: Rows, connector: Connector) -> bool:
    """Whether the connector, as stored, holds no cloud error."""
    stored = await rows.connector_as_stored(connector.id)
    return stored is not None and (stored.consecutive_errors, stored.last_error) == (0, None)


async def _meter(redis_client: RedisClient, proxy: Proxy, delta: MetricDelta) -> None:
    """Add ``delta`` to the proxy's live window, as a gateway's metering does."""
    parts = redis_client.metering_batch(MetricWindows(proxies={proxy.id: delta}))
    assert all(reply.landed for reply in await redis_client.send_batch(parts))


def _connector(**fields: Any) -> Connector:
    return Connector(
        name="c", credential_id="cr", credential_type=CredentialType.AWS, project_id="p", config=AWS_CONFIG, **fields
    )


def _range(min_proxies: int, max_proxies: int) -> CloudConnectorConfig:
    return CloudConnectorConfig(min_proxies=min_proxies, max_proxies=max_proxies)


class TestRules:
    def test_demand_levels_target_the_minimum_the_midpoint_or_the_maximum(self) -> None:
        assert _range(2, 10).target_count(DemandLevel.LOW) == 2
        assert _range(2, 10).target_count(DemandLevel.MEDIUM) == 6
        assert _range(2, 10).target_count(DemandLevel.HIGH) == 10

    def test_steps_are_a_third_up_and_a_sixth_down_of_the_range_at_least_one(self) -> None:
        assert _range(1, 5).max_steps == (2, 1)
        assert _range(1, 10).max_steps == (3, 2)
        assert _range(2, 20).max_steps == (6, 3)
        assert _range(1, 2).max_steps == (1, 1)
        assert _range(5, 5).max_steps == (1, 1)

    def test_errors_back_off_exponentially_up_to_the_ceiling(self) -> None:
        now = utc_now()
        assert not _connector().in_error_backoff
        assert not _connector(last_error="x", last_error_at=now - timedelta(minutes=1)).in_error_backoff
        assert not _connector(last_error="x", last_error_at=None, consecutive_errors=5).in_error_backoff
        assert _connector(last_error_at=now - timedelta(minutes=1), consecutive_errors=1).in_error_backoff
        assert not _connector(last_error_at=now - timedelta(minutes=3), consecutive_errors=1).in_error_backoff
        assert _connector(last_error_at=now - timedelta(minutes=3), consecutive_errors=2).in_error_backoff
        assert not _connector(last_error_at=now - timedelta(minutes=5), consecutive_errors=2).in_error_backoff
        assert _connector(last_error_at=now - timedelta(minutes=7), consecutive_errors=3).in_error_backoff
        assert not _connector(last_error_at=now - timedelta(minutes=9), consecutive_errors=3).in_error_backoff
        assert MAX_ERROR_BACKOFF_MINUTES == 30
        assert _connector(last_error_at=now - timedelta(minutes=29), consecutive_errors=10).in_error_backoff
        assert not _connector(last_error_at=now - timedelta(minutes=31), consecutive_errors=10).in_error_backoff

    def test_the_state_round_trips_through_its_hash_and_skips_what_it_cannot_read(self) -> None:
        state = AutoscalerState(
            since=100.5,
            level=DemandLevel.MEDIUM,
            last_action=200.25,
            listed_at=150.75,
            rotations={"a": 300.0},
            drains={"b": Drain(1, 2, 1)},
        )
        assert state.to_fields()["drain:b"] == "1 2 1"
        assert AutoscalerState.from_fields(state.to_fields(), now=0.0) == state
        garbled = {
            **state.to_fields(), "level": "frantic", "listed_at": "lately", "rotate:c": "soon", "drain:d": "1 2", "drain:e": "x y z"
        }
        assert AutoscalerState.from_fields(garbled, now=0.0) == AutoscalerState(
            since=100.5, last_action=200.25, rotations={"a": 300.0}, drains={"b": Drain(1, 2, 1)}
        )
        assert AutoscalerState.from_fields({}, now=42.0) == AutoscalerState(since=42.0)

    def test_an_entry_names_its_purpose_and_reads_stale_past_the_create_timeout(self) -> None:
        entry = Creation.rotation("p1", at=1000.0)
        assert json.loads(entry.to_json()) == {"purpose": "rotate:p1", "at": 1000.0}
        assert Creation.parse(entry.to_json()) == entry
        assert (entry.replaces, entry.scales) == ("p1", False)
        assert (Creation.scale(1000.0).replaces, Creation.scale(1000.0).scales) == (None, True)
        assert not entry.stale(1000.0 + CREATE_TIMEOUT_SECONDS) and entry.stale(1000.0 + CREATE_TIMEOUT_SECONDS + 1)

    def test_an_unreadable_entry_reads_as_a_stale_scale_entry_so_a_run_clears_it(self) -> None:
        for raw in ("{", "[]", '{"purpose": "scale"}', '{"purpose": "scale", "at": "soon"}'):
            assert Creation.parse(raw) == Creation.scale(0.0), raw


class TestScaling:
    async def test_below_the_minimum_an_instance_is_created_and_scheduled_for_rotation(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        await harness.run(aws)

        [name] = harness.account.created
        assert re.fullmatch(r"octo-\d{10}-[0-9a-f]{6}", name)
        [instance] = await rows.of(aws)
        assert (instance.id, instance.status) == (f"fake-{name}", ProxyStatus.INITIALIZING)
        state = await _state(redis_client, aws)
        assert state.last_action is not None and state.level == DemandLevel.LOW
        assert time.time() + 60 * 60 - 5 <= state.rotations[instance.id] <= time.time() + 120 * 60 + 5

    async def test_scale_up_is_capped_at_a_step(self, harness: Harness, rows: Rows, aws: Connector) -> None:
        await _instances(rows, aws, 1)
        harness.demand.level = DemandLevel.HIGH
        await harness.run(aws)
        # Target 5, one step up is ceil(4 / 3) = 2.
        assert len(harness.account.created) == 2

    async def test_nothing_changes_at_the_target(self, harness: Harness, rows: Rows, aws: Connector) -> None:
        await _instances(rows, aws, 1)
        await harness.run(aws)
        assert harness.account.created == [] and await _statuses(rows, aws) == [ProxyStatus.HEALTHY]

    async def test_the_last_demand_level_is_remembered_for_hysteresis(
        self, harness: Harness, rows: Rows, aws: Connector
    ) -> None:
        await _instances(rows, aws, 1)
        harness.demand.level = DemandLevel.MEDIUM
        harness.demand.recent = False
        await harness.run(aws)
        await harness.run(aws)
        assert harness.demand.previous == [None, DemandLevel.MEDIUM]

    async def test_above_the_target_instances_drain_a_step_at_a_time_unhealthy_then_oldest(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        await _seasoned(redis_client, aws)
        old, unhealthy, new = (
            await rows.proxy(aws, created_at=utc_now() - timedelta(hours=2), status=ProxyStatus.HEALTHY),
            await rows.proxy(aws, status=ProxyStatus.UNHEALTHY),
            await rows.proxy(aws, status=ProxyStatus.HEALTHY),
        )
        await rows.proxy(aws, created_at=utc_now() - timedelta(hours=1), status=ProxyStatus.HEALTHY)

        # Target one, one step down is ceil(4 / 6) = 1.
        await harness.run(aws)
        first = {p.id: p.status for p in await rows.of(aws)}
        assert first[unhealthy.id] == ProxyStatus.DRAINING
        assert list(first.values()).count(ProxyStatus.DRAINING) == 1

        # The next step comes after the cooldown: the oldest healthy one.
        await _seasoned(redis_client, aws, last_action=time.time() - SCALING_COOLDOWN_SECONDS - 1)
        await harness.run(aws)
        second = {p.id: p.status for p in await rows.of(aws)}
        assert second[old.id] == ProxyStatus.DRAINING and second[new.id] == ProxyStatus.HEALTHY

    async def test_a_lost_state_scales_nothing_in_until_a_demand_window_is_watched(
        self, harness: Harness, rows: Rows, aws: Connector
    ) -> None:
        """Demand read after a Redis wipe looks like none; draining on it would shrink a busy pool."""
        await _instances(rows, aws, 3)
        await harness.run(aws)
        assert await _statuses(rows, aws) == [ProxyStatus.HEALTHY] * 3

    async def test_scaling_waits_out_the_cooldown(
        self, harness: Harness, aws: Connector, redis_client: RedisClient
    ) -> None:
        harness.demand.level = DemandLevel.HIGH
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), last_action=time.time() - 60))
        await harness.run(aws)
        assert harness.account.created == []

        expired = time.time() - SCALING_COOLDOWN_SECONDS - 10
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), last_action=expired))
        await harness.run(aws)
        assert len(harness.account.created) == 2

    async def test_scaling_waits_while_an_instance_initializes(
        self, harness: Harness, rows: Rows, aws: Connector
    ) -> None:
        await _instances(rows, aws, 1)
        await _instances(rows, aws, 1, ProxyStatus.INITIALIZING)
        harness.demand.level = DemandLevel.HIGH
        await harness.run(aws)
        assert harness.account.created == []

    async def test_scale_up_needs_recent_activity_except_to_reach_the_minimum(
        self, harness: Harness, rows: Rows, project: Project
    ) -> None:
        harness.demand.recent = False
        harness.demand.level = DemandLevel.HIGH
        stale = await rows.connector(project, "aws", config=AWS_CONFIG)
        await _instances(rows, stale, 1)
        await harness.run(stale)
        assert harness.account.created == []

        # Below the minimum (one), it is reached whatever the traffic.
        empty = await rows.connector(project, "aws", config=AWS_CONFIG)
        harness.demand.level = DemandLevel.LOW
        await harness.run(empty)
        assert len(harness.account.created) == 1

    async def test_rejected_requests_scale_a_pool_up_from_zero(
        self, harness: Harness, rows: Rows, project: Project
    ) -> None:
        zero = await rows.connector(project, "aws", config={**AWS_CONFIG, "min_proxies": 0})
        harness.demand.level = DemandLevel.HIGH
        await harness.run(zero)
        assert len(harness.account.created) == 2

    async def test_scale_down_is_not_gated_by_recent_activity(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        await _seasoned(redis_client, aws)
        await _instances(rows, aws, 3)
        harness.demand.recent = False
        await harness.run(aws)
        assert (await _statuses(rows, aws)).count(ProxyStatus.DRAINING) == 1
        assert (await _state(redis_client, aws)).last_action is not None

    async def test_a_connector_blocked_by_its_traffic_limit_is_not_scaled_until_the_block_ends(
        self, harness: Harness, aws: Connector, redis_client: RedisClient
    ) -> None:
        harness.demand.level = DemandLevel.HIGH
        until = (utc_now() + timedelta(hours=1)).isoformat()
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, aws.id, until)
        await harness.run(aws)
        assert harness.account.created == []

        # An entry past its period, not yet decided again, blocks nothing.
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, aws.id, (utc_now() - timedelta(minutes=1)).isoformat())
        await harness.run(aws)
        assert len(harness.account.created) == 2

    async def test_no_scaling_while_backing_off_after_errors_and_again_after(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await _seasoned(redis_client, aws)
        await _instances(rows, aws, 3)
        await _with_error(sessions, aws, errors=2, minutes_ago=1)
        await harness.run(aws)
        assert ProxyStatus.DRAINING not in await _statuses(rows, aws)

        await _with_error(sessions, aws, errors=2, minutes_ago=10)
        await harness.run(aws)
        assert (await _statuses(rows, aws)).count(ProxyStatus.DRAINING) == 1

    async def test_a_cloud_error_is_recorded_and_ends_the_step_and_a_success_clears_it(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        harness.demand.level = DemandLevel.HIGH
        harness.account.create_error = RuntimeError("AWS AuthFailure: invalid credentials")
        await harness.run(aws)

        stored = await rows.connector_as_stored(aws.id)
        assert stored is not None and stored.consecutive_errors == 1
        assert stored.last_error is not None and "AWS AuthFailure" in stored.last_error
        assert len(harness.account.created) == 1 and await rows.of(aws) == []
        # The failed create's entry is cleared: an instance it made anyway is terminated once listed.
        assert await _creating(redis_client, aws) == {}

        # Past the backoff the next creation succeeds and clears it.
        async with sessions() as session:
            backed_off = stored.model_copy(update={"last_error_at": utc_now() - timedelta(minutes=5)})
            await ConnectorRepository(session).update(backed_off)
            await session.commit()
        harness.account.create_error = None
        await harness.run(aws)
        cleared = await rows.connector_as_stored(aws.id)
        assert cleared is not None and (cleared.last_error, cleared.consecutive_errors) == (None, 0)

    async def test_a_batch_scales_each_connector_it_names(
        self, harness: Harness, rows: Rows, aws: Connector, project: Project
    ) -> None:
        other = await rows.connector(project, "aws", config=AWS_CONFIG)
        await harness.run(aws, other)
        assert len(await rows.of(aws)) == 1 and len(await rows.of(other)) == 1


class TestCreatingEntries:
    """An entry of the creating hash covers each create, so runs that overlap count the instance."""

    async def test_the_entry_is_written_before_the_create_and_cleared_after_the_row(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        redis_client: RedisClient,
        proxy_service: ProxyService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add = proxy_service.add
        entries_at_save: list[dict[str, dict[str, Any]]] = []

        async def saving(proxy: Proxy) -> None:
            entries_at_save.append(await _creating(redis_client, aws))
            await add(proxy)

        monkeypatch.setattr(proxy_service, "add", saving)
        held = harness.account.hold()
        first = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()

        [name] = harness.account.created
        assert await _creating(redis_client, aws) == {name: {"purpose": "scale", "at": pytest.approx(time.time(), abs=5)}}
        assert await rows.of(aws) == []
        held.set()
        await first

        assert list(entries_at_save[0]) == [name]
        assert await _creating(redis_client, aws) == {}
        assert [proxy.id for proxy in await rows.of(aws)] == [f"fake-{name}"]

    async def test_a_run_beside_a_create_creates_no_further_instance(
        self, harness: Harness, aws: Connector, redis_client: RedisClient
    ) -> None:
        harness.demand.level = DemandLevel.HIGH
        await _entry(redis_client, aws, "octo-1-abcdef", "scale", age=10)
        await harness.run(aws)
        assert harness.account.created == []

    async def test_a_proxy_whose_replacement_is_being_created_is_not_rotated_again(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        await _entry(redis_client, aws, "octo-1-abcdef", f"rotate:{old.id}", age=10)
        await harness.run(aws)
        assert harness.account.created == [] and await _statuses(rows, aws) == [ProxyStatus.HEALTHY]

    async def test_a_creator_whose_instance_was_terminated_under_it_saves_no_row(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        """Redis lost the creator's entry while it waited, so a run beside it found an instance nobody records."""
        held = harness.account.hold()
        creator = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()
        [lost] = harness.account.created
        await redis_client.client.delete(SCALE_CREATING_KEY.format(connector_id=aws.id))

        # The run beside it terminates the instance, then creates the connector's minimum itself.
        await harness.run(aws)
        assert harness.account.terminated == [f"fake-{lost}"]
        [_, kept] = harness.account.created

        with structlog.testing.capture_logs() as logs:
            held.set()
            await creator

        assert [proxy.id for proxy in await rows.of(aws)] == [f"fake-{kept}"]
        assert await _creating(redis_client, aws) == {}
        assert await _no_error(rows, aws)
        assert [(log["log_level"], log["name"]) for log in logs if log["event"].startswith("Saving no row")] == [
            ("warning", lost)
        ]
        # The creator added no instance, so its state (saved last) starts no cooldown.
        assert (await _state(redis_client, aws)).last_action is None

    async def test_a_step_writes_the_entries_of_all_its_instances_before_its_first_create(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        harness.demand.level = DemandLevel.HIGH
        held = harness.account.hold()
        step = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()

        # Target 5, one step up is 2: the second instance's entry is there while the first is created.
        [first] = harness.account.created
        entries = await _creating(redis_client, aws)
        assert len(entries) == 2 and first in entries
        assert {entry["purpose"] for entry in entries.values()} == {"scale"}
        held.set()
        await step

        assert len(await rows.of(aws)) == 2
        assert await _creating(redis_client, aws) == {}


class TestRunsTogether:
    """Runs of one connector that overlap at a step or a rotation: one script writes the entries only while no other run holds a fresh one of that purpose, and a step reads the rows again once its entries are written."""

    # What a step logs, at warning, when another run took a step while its own entries were stale.
    ENDED = "Ending a scaling step: another run is creating instances"
    # What a step logs, at debug, when the rows it reads again once its entries are written hold another number of active instances.
    CHANGED = "Skipping scaling: the connector's instances changed since the run read them"

    async def test_two_runs_that_start_together_take_one_scaling_step(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        """Each reads the hash and the rows before the other writes an entry."""
        harness.demand.level = DemandLevel.HIGH
        harness.demand.before_level = asyncio.Barrier(2).wait
        await asyncio.gather(harness.run(aws), harness.run(aws))

        # Target 5, one step up is 2: the run whose script wrote first took the step, and the other none.
        assert len(harness.account.created) == 2
        assert len(await rows.of(aws)) == 2
        assert await _creating(redis_client, aws) == {}

    async def test_a_run_that_another_runs_whole_step_overtook_creates_nothing(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other run's step ends, its rows saved and its entries cleared, just before this run writes its own: the rows read again after the write show it."""
        harness.demand.level = DemandLevel.HIGH
        _on_writing_entries(monkeypatch, redis_client, aws, lambda: harness.run(aws), before=True)
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)

        # Target 5, one step up is 2: the other run's two instances, and none of this run's.
        assert len(harness.account.created) == 2
        assert len(await rows.of(aws)) == 2
        assert await _creating(redis_client, aws) == {}
        assert [(log["log_level"], log["read"], log["stored"]) for log in logs if log["event"] == self.CHANGED] == [
            ("debug", 0, 2)
        ]

    async def test_a_retiring_row_deleted_meanwhile_does_not_hold_the_step_off(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        redis_client: RedisClient,
        proxy_service: ProxyService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another run deletes a terminated instance's row just before this run writes its entries: the active count is the one this run read."""
        [retired] = await _instances(rows, aws, 1, ProxyStatus.TERMINATING)

        async def terminated() -> None:
            await proxy_service.delete(retired.id)

        _on_writing_entries(monkeypatch, redis_client, aws, terminated, before=True)
        await harness.run(aws)

        # Below the minimum of one, so the step creates it.
        [name] = harness.account.created
        assert [proxy.id for proxy in await rows.of(aws)] == [f"fake-{name}"]

    async def test_two_runs_that_start_together_replace_a_due_proxy_once(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        harness.demand.before_level = asyncio.Barrier(2).wait
        await asyncio.gather(harness.run(aws), harness.run(aws))

        [name] = harness.account.created
        statuses = {p.id: p.status for p in await rows.of(aws)}
        assert statuses == {old.id: ProxyStatus.DRAINING, f"fake-{name}": ProxyStatus.INITIALIZING}
        assert await _creating(redis_client, aws) == {}

    async def test_only_a_fresh_entry_holds_a_step_off(
        self, harness: Harness, aws: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry written after the run read the hash, and stale by its step: its writer died, and the run's reads took longer than the create timeout."""
        clock = Clock(time.time())
        monkeypatch.setattr("api.jobs.scale.time", clock)
        harness.demand.level = DemandLevel.HIGH

        async def written_and_left() -> None:
            harness.demand.before_level = None
            entry = json.dumps({"purpose": "scale", "at": clock.now})
            await redis_client.client.hset(SCALE_CREATING_KEY.format(connector_id=aws.id), "octo-1-abcdef", entry)
            clock.now += CREATE_TIMEOUT_SECONDS + 1

        harness.demand.before_level = written_and_left
        await harness.run(aws)

        # The step went ahead; the stale entry is the next run's to settle.
        assert len(harness.account.created) == 2
        assert list(await _creating(redis_client, aws)) == ["octo-1-abcdef"]

    async def test_a_rotation_reads_the_row_after_writing_its_entry(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another run starts once this run has read the row: it finds this run's entry and leaves the proxy alone, where a row read before the entry would let its whole rotation slip in between."""
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        get_by_id = ProxyRepository.get_by_id
        pending = [lambda: harness.run(aws)]

        async def reading(repository: ProxyRepository, proxy_id: str) -> Proxy | None:
            row = await get_by_id(repository, proxy_id)
            if pending:
                await pending.pop()()
            return row

        monkeypatch.setattr(ProxyRepository, "get_by_id", reading)
        await harness.run(aws)

        assert len(harness.account.created) == 1
        assert {p.id: p.status for p in await rows.of(aws)}[old.id] == ProxyStatus.DRAINING
        assert await _creating(redis_client, aws) == {}

    async def test_a_run_beside_a_step_longer_than_the_create_timeout_finds_its_entries_fresh(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each create takes 400 seconds: a step writes the entries still to create again before each create."""
        clock = Clock(time.time())
        monkeypatch.setattr("api.jobs.scale.time", clock)
        harness.demand.level = DemandLevel.HIGH
        first = harness.account.hold()
        step = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()
        clock.now += 400
        second = harness.account.hold()
        first.set()
        await harness.account.held_create()
        clock.now += 400

        # 800 seconds after the step began, 400 after its second entry was written again.
        await harness.run(aws)
        assert harness.account.terminated == [] and len(harness.account.created) == 2

        second.set()
        await step
        assert len(await rows.of(aws)) == 2
        assert await _creating(redis_client, aws) == {}

    async def test_a_step_that_finds_another_runs_step_under_way_ends(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A create slower than the create timeout: a run beside it terminates its instance, clears its stale entries and takes a step of its own."""
        clock = Clock(time.time())
        monkeypatch.setattr("api.jobs.scale.time", clock)
        harness.demand.level = DemandLevel.HIGH
        held = harness.account.hold()
        slow = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()
        [lost] = harness.account.created
        clock.now += CREATE_TIMEOUT_SECONDS + 1

        beside_held = harness.account.hold()
        beside = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()
        assert harness.account.terminated == [f"fake-{lost}"]

        with structlog.testing.capture_logs() as logs:
            held.set()
            await slow
        beside_held.set()
        await beside

        # The slow step saved no row for its lost instance and created nothing more: the step beside it made two.
        assert len(harness.account.created) == 3
        assert len(await rows.of(aws)) == 2
        assert [log["log_level"] for log in logs if log["event"] == self.ENDED] == ["warning"]
        assert await _creating(redis_client, aws) == {}


class TestConnectorReadAgain:
    """A run reads the connector again once its entry is written, so a removal and a deletion run cannot leave an instance behind."""

    # What a run logs, at info, when the connector it reads again no longer wants the instance.
    NOT_SCALED = "Creating no instance: the connector was disabled or removed"

    @pytest.mark.parametrize("change", ["removed", "disabled", "deleted"])
    async def test_a_change_between_the_entry_and_the_read_stops_the_create_and_clears_the_entry(
        self,
        change: str,
        harness: Harness,
        rows: Rows,
        project: Project,
        aws: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        connector_service: ConnectorService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The run read the connector as enabled; the change lands once its entry is written, before it reads again."""

        async def meanwhile() -> None:
            if change == "removed":
                await connector_service.remove(project.id, aws.id)
                # A deletion run now finds the entry, so it waits for it.
                await harness.run(aws)
                assert await rows.connector_as_stored(aws.id) is not None
            else:
                async with sessions() as session:
                    if change == "disabled":
                        await ConnectorRepository(session).update(aws.model_copy(update={"enabled": False}))
                    else:
                        await ConnectorRepository(session).delete(aws.id)
                    await session.commit()

        _on_writing_entries(monkeypatch, redis_client, aws, meanwhile)
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)

        assert harness.account.created == []
        assert await _creating(redis_client, aws) == {}
        assert [(log["log_level"], log["connector_id"]) for log in logs if log["event"] == self.NOT_SCALED] == [
            ("info", aws.id)
        ]
        # Nothing was added, so no cooldown starts.
        assert (await _state(redis_client, aws)).last_action is None
        if change == "removed":
            # Nothing is left to wait for: the next run deletes the connector.
            await harness.run(aws)
            assert await rows.connector_as_stored(aws.id) is None

    async def test_a_removal_after_the_read_makes_the_deletion_wait_for_the_entry(
        self,
        harness: Harness,
        rows: Rows,
        project: Project,
        aws: Connector,
        redis_client: RedisClient,
        connector_service: ConnectorService,
    ) -> None:
        """The run found the connector enabled and creates; a deletion run before the instance is listed finds the entry."""

        async def meanwhile() -> None:
            await connector_service.remove(project.id, aws.id)
            await harness.run(aws)
            assert await rows.connector_as_stored(aws.id) is not None
            assert harness.account.terminated == []

        harness.account.before_create = meanwhile
        await harness.run(aws)

        # The instance got its row, so the runs that follow retire it with the connector: none is left running.
        [name] = harness.account.created
        assert [proxy.id for proxy in await rows.of(aws)] == [f"fake-{name}"]
        for _ in range(5):  # drained, its baseline, two quiet checks, terminated
            await harness.run(aws)
        assert harness.account.terminated == [f"fake-{name}"]
        assert await rows.connector_as_stored(aws.id) is not None
        await harness.run(aws)
        assert await rows.connector_as_stored(aws.id) is None

    async def test_a_connector_pending_deletion_creates_nothing_though_enabled_again(
        self, harness: Harness, rows: Rows, project: Project, redis_client: RedisClient
    ) -> None:
        """An edit can enable a connector its user removed; it is still on its way out."""
        connector = await rows.connector(project, "aws", config=AWS_CONFIG, pending_deletion=True)

        await harness.run(connector)

        assert harness.account.created == []
        assert await _creating(redis_client, connector) == {}

    async def test_a_step_reads_the_connector_again_before_each_create(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """The connector is disabled as the step's first create starts: that one gets its row, and the read before the second stops the step."""

        async def disabled() -> None:
            async with sessions() as session:
                await ConnectorRepository(session).update(aws.model_copy(update={"enabled": False}))
                await session.commit()

        harness.demand.level = DemandLevel.HIGH
        harness.account.before_create = disabled
        await harness.run(aws)

        [name] = harness.account.created
        assert [proxy.id for proxy in await rows.of(aws)] == [f"fake-{name}"]
        assert await _creating(redis_client, aws) == {}

    async def test_a_rotation_whose_connector_was_disabled_meanwhile_creates_nothing_and_leaves_the_proxy_serving(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The next run, which reads the connector disabled, drains every instance."""
        [old] = await _instances(rows, aws, 1)
        await _seasoned(redis_client, aws, rotations={old.id: time.time() - 60})

        async def disabled() -> None:
            async with sessions() as session:
                await ConnectorRepository(session).update(aws.model_copy(update={"enabled": False}))
                await session.commit()

        _on_writing_entries(monkeypatch, redis_client, aws, disabled)
        await harness.run(aws)

        assert harness.account.created == []
        assert await _creating(redis_client, aws) == {}
        assert await _statuses(rows, aws) == [ProxyStatus.HEALTHY]


class TestStaleEntries:
    """An entry older than the create timeout was left by a run that died or lost Redis: a run with a listing counts it as none, then clears it."""

    async def test_its_instance_with_no_row_is_terminated_as_an_unrecorded_one_is_then_the_entry_cleared(
        self, harness: Harness, rows: Rows, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        stale = harness.account.boot(emptiable, "octo-1-aaaaaa")
        unrecorded = harness.account.boot(emptiable, "octo-2-bbbbbb")
        await _entry(redis_client, emptiable, "octo-1-aaaaaa", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        with structlog.testing.capture_logs() as logs:
            await harness.run(emptiable)

        assert harness.account.terminated == [stale.proxy_id, unrecorded.proxy_id]
        assert await _creating(redis_client, emptiable) == {} and await rows.of(emptiable) == []
        # One warning for both: the stale entry records nothing.
        assert [(log["log_level"], log["name"]) for log in logs if log["event"] == UNRECORDED] == [
            ("warning", "octo-1-aaaaaa"),
            ("warning", "octo-2-bbbbbb"),
        ]

    async def test_an_entry_whose_termination_fails_is_left_for_a_later_run_and_the_other_stale_ones_cleared(
        self, harness: Harness, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        instance = harness.account.boot(emptiable, "octo-1-abcdef")
        await _entry(redis_client, emptiable, "octo-1-abcdef", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        # Its instance never came, so no failed termination keeps it.
        await _entry(redis_client, emptiable, "octo-2-bbbbbb", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        harness.account.terminate_error = RuntimeError("API down")
        await harness.run(emptiable)
        assert list(await _creating(redis_client, emptiable)) == ["octo-1-abcdef"]

        harness.account.terminate_error = None
        await harness.run(emptiable)
        assert harness.account.terminated == [instance.proxy_id]
        assert await _creating(redis_client, emptiable) == {}

    async def test_a_rotations_replacement_is_terminated_and_the_old_proxy_goes_on_serving(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        replacement = harness.account.boot(aws, "octo-1-abcdef")
        await _entry(redis_client, aws, "octo-1-abcdef", f"rotate:{old.id}", age=CREATE_TIMEOUT_SECONDS + 5)
        await harness.run(aws)

        assert harness.account.terminated == [replacement.proxy_id]
        assert {p.id: p.status for p in await rows.of(aws)} == {old.id: ProxyStatus.HEALTHY}
        assert await _creating(redis_client, aws) == {}

    async def test_an_entry_whose_instance_has_its_row_is_cleared_and_the_instance_kept(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient, proxy_service: ProxyService
    ) -> None:
        """Its run saved the row and stopped before it cleared the entry."""
        instance = harness.account.boot(aws, "octo-1-abcdef")
        await proxy_service.add(harness.account.proxy(aws, "octo-1-abcdef").model_copy(update={"status": ProxyStatus.HEALTHY}))
        due = time.time() + 3 * 3600
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={instance.proxy_id: due}))
        await _entry(redis_client, aws, "octo-1-abcdef", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        await harness.run(aws)

        assert harness.account.terminated == []
        assert await _creating(redis_client, aws) == {}
        assert await _statuses(rows, aws) == [ProxyStatus.HEALTHY]
        assert (await _state(redis_client, aws)).rotations == {instance.proxy_id: due}

    async def test_an_entry_whose_instance_never_came_is_cleared(
        self, harness: Harness, aws: Connector, redis_client: RedisClient
    ) -> None:
        await _entry(redis_client, aws, "octo-1-abcdef", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        await harness.run(aws)

        assert harness.account.terminated == []
        assert await _creating(redis_client, aws) == {}

    async def test_entries_stay_while_the_listing_fails_so_a_stale_scale_entry_holds_scaling_until_one_succeeds(
        self, harness: Harness, aws: Connector, redis_client: RedisClient, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Without a listing a run cannot tell whether the entry's instance runs: the first run that lists, once the failed listing's backoff is over, settles it."""
        instance = harness.account.boot(aws, "octo-1-abcdef")
        await _entry(redis_client, aws, "octo-1-abcdef", "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        harness.account.list_error = RuntimeError("API down")
        await harness.run(aws)
        assert harness.account.terminated == [] and harness.account.created == []
        assert list(await _creating(redis_client, aws)) == ["octo-1-abcdef"]

        harness.account.list_error = None
        await _backoff_over(sessions, aws)
        await harness.run(aws)
        assert harness.account.terminated == [instance.proxy_id]
        # The entry gone, the run scales to the connector's minimum.
        assert len(harness.account.created) == 1 and await _creating(redis_client, aws) == {}

    async def test_a_stale_rotation_entry_holds_its_proxys_rotation_while_the_listing_fails(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        """The run's own read holds a rotation off any entry for the proxy, as it holds a step off any scale entry."""
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        await _entry(redis_client, aws, "octo-1-abcdef", f"rotate:{old.id}", age=CREATE_TIMEOUT_SECONDS + 5)
        harness.account.list_error = RuntimeError("API down")
        await harness.run(aws)

        assert harness.account.created == [] and await _statuses(rows, aws) == [ProxyStatus.HEALTHY]
        assert list(await _creating(redis_client, aws)) == ["octo-1-abcdef"]


class TestCreatesCutShort:
    """A worker's clean stop cancels a run still going after its grace: a create it cuts short keeps its entry, as a crash leaves it, so runs list until the entry is stale and then settle its instance."""

    @staticmethod
    async def _cut_short(harness: Harness, connector: Connector) -> str:
        """Run the job over the connector until its next create is under way, then cancel the run as the stop does; the create's name."""
        harness.account.hold()
        run = asyncio.create_task(harness.run(connector))
        await harness.account.held_create()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        return harness.account.created[-1]

    async def test_a_step_keeps_the_entry_of_its_create_and_clears_those_it_never_started(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), listed_at=time.time() - 60))
        # Target 5, one step up is 2: the first create is cut short, the second never starts.
        harness.demand.level = DemandLevel.HIGH
        name = await self._cut_short(harness, aws)

        assert harness.account.created == [name]
        assert await _creating(redis_client, aws) == {name: {"purpose": "scale", "at": pytest.approx(time.time(), abs=5)}}
        assert not harness.account.instances[name].gone and await rows.of(aws) == []

        # Within the hour of the last listing, the entry alone makes the next run list; fresh, it keeps the instance
        # and holds the step.
        await harness.run(aws)
        assert harness.account.listings == 1
        assert harness.account.terminated == [] and harness.account.created == [name]

        # Once the entry is stale, a run's listing terminates the instance, which has no row, and the step goes on.
        await _entry(redis_client, aws, name, "scale", age=CREATE_TIMEOUT_SECONDS + 5)
        await harness.run(aws)
        assert harness.account.terminated == [f"fake-{name}"]
        assert len(harness.account.created) == 3 and len(await rows.of(aws)) == 2
        assert await _creating(redis_client, aws) == {}

    async def test_a_rotation_keeps_its_entry(
        self, harness: Harness, rows: Rows, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, emptiable, 1)
        await _seed(
            redis_client,
            emptiable,
            AutoscalerState(since=time.time(), listed_at=time.time() - 60, rotations={old.id: time.time() - 60}),
        )
        name = await self._cut_short(harness, emptiable)

        entries = await _creating(redis_client, emptiable)
        assert entries == {name: {"purpose": f"rotate:{old.id}", "at": pytest.approx(time.time(), abs=5)}}
        # The due proxy drains, replaced or not; its replacement runs with no row.
        assert {p.id: p.status for p in await rows.of(emptiable)} == {old.id: ProxyStatus.DRAINING}
        assert not harness.account.instances[name].gone

        await harness.run(emptiable)
        assert harness.account.listings == 1 and harness.account.terminated == []
        assert list(await _creating(redis_client, emptiable)) == [name]

        await _entry(redis_client, emptiable, name, f"rotate:{old.id}", age=CREATE_TIMEOUT_SECONDS + 5)
        await harness.run(emptiable)
        assert harness.account.terminated == [f"fake-{name}"]
        assert await _creating(redis_client, emptiable) == {}


class TestUnrecordedInstances:
    """A run that lists terminates the instances its cloud lists that have neither a row nor a fresh entry: their creator is gone."""

    async def test_each_is_terminated_and_logged_at_warning_with_its_name(
        self, harness: Harness, rows: Rows, emptiable: Connector
    ) -> None:
        first = harness.account.boot(emptiable, "octo-1-aaaaaa")
        second = harness.account.boot(emptiable, "octo-2-bbbbbb")
        with structlog.testing.capture_logs() as logs:
            await harness.run(emptiable)

        assert harness.account.terminated == [first.proxy_id, second.proxy_id]
        assert await rows.of(emptiable) == []
        assert [(log["log_level"], log["name"]) for log in logs if log["event"] == UNRECORDED] == [
            ("warning", "octo-1-aaaaaa"),
            ("warning", "octo-2-bbbbbb"),
        ]

    async def test_an_instance_with_a_row_or_a_fresh_entry_is_left_alone(
        self,
        harness: Harness,
        rows: Rows,
        emptiable: Connector,
        redis_client: RedisClient,
        proxy_service: ProxyService,
    ) -> None:
        harness.account.boot(emptiable, "octo-1-aaaaaa")
        await proxy_service.add(
            harness.account.proxy(emptiable, "octo-1-aaaaaa").model_copy(update={"status": ProxyStatus.HEALTHY})
        )
        harness.account.boot(emptiable, "octo-2-bbbbbb")
        await _entry(redis_client, emptiable, "octo-2-bbbbbb", "scale", age=10)
        await harness.run(emptiable)

        assert harness.account.terminated == []
        assert await _statuses(rows, emptiable) == [ProxyStatus.HEALTHY]
        assert list(await _creating(redis_client, emptiable)) == ["octo-2-bbbbbb"]

    async def test_the_rows_are_read_after_the_entries_so_a_creator_finishing_meanwhile_keeps_its_instance(
        self,
        harness: Harness,
        rows: Rows,
        emptiable: Connector,
        redis_client: RedisClient,
        proxy_service: ProxyService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A creator saves its row, then clears its entry: the hash read before the rows shows one or the other."""
        instance = harness.account.boot(emptiable, "octo-1-abcdef")
        await _entry(redis_client, emptiable, "octo-1-abcdef", "scale", age=10)

        async def creator_finishes() -> None:
            await proxy_service.add(harness.account.proxy(emptiable, "octo-1-abcdef"))
            await redis_client.client.hdel(SCALE_CREATING_KEY.format(connector_id=emptiable.id), "octo-1-abcdef")

        _before_reading_entries(monkeypatch, redis_client, emptiable, creator_finishes)
        await harness.run(emptiable)

        assert harness.account.terminated == []
        assert [proxy.id for proxy in await rows.of(emptiable)] == [instance.proxy_id]

    async def test_the_entries_are_read_after_the_listing_so_a_creator_starting_meanwhile_keeps_its_instance(
        self, harness: Harness, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        """A creator writes its entry, then creates: an instance the listing shows has its entry in the hash read after."""

        async def creator_starts() -> None:
            await _entry(redis_client, emptiable, "octo-1-abcdef", "scale", age=0)
            harness.account.boot(emptiable, "octo-1-abcdef")

        harness.account.before_listing = creator_starts
        await harness.run(emptiable)

        assert harness.account.terminated == []
        assert list(await _creating(redis_client, emptiable)) == ["octo-1-abcdef"]

    async def test_a_termination_that_fails_is_logged_the_run_goes_on_and_the_next_listing_tries_again(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        instance = harness.account.boot(aws, "octo-1-abcdef")
        harness.account.terminate_error = RuntimeError("API down")
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)
        failed = [log for log in logs if log["event"] == "Failed to terminate a cloud instance with no row"]
        assert [(log["log_level"], log["name"], log["error"]) for log in failed] == [("warning", "octo-1-abcdef", "API down")]
        # The run went on to scale to the connector's minimum, and recorded nothing on the connector.
        assert len(harness.account.created) == 1 and await _no_error(rows, aws)

        # The listing succeeded, so a run within the hour lists nothing; the one after the hour tries again.
        harness.account.terminate_error = None
        await harness.run(aws)
        assert harness.account.terminated == []
        await _an_hour_on(redis_client, aws)
        await harness.run(aws)
        assert harness.account.terminated == [instance.proxy_id]

    async def test_a_listing_that_fails_terminates_nothing_and_holds_scaling_until_its_backoff_is_over(
        self, harness: Harness, rows: Rows, aws: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A failed listing is a cloud error like a failed create: recorded on the connector, which backs off."""
        instance = harness.account.boot(aws, "octo-1-abcdef")
        harness.account.list_error = RuntimeError("API down")
        await harness.run(aws)
        assert harness.account.terminated == [] and harness.account.created == []
        assert await rows.of(aws) == []

        harness.account.list_error = None
        await _backoff_over(sessions, aws)
        await harness.run(aws)
        assert harness.account.terminated == [instance.proxy_id]
        assert [p.id for p in await rows.of(aws)] == [f"fake-{harness.account.created[0]}"]
        assert await _no_error(rows, aws)


class TestListing:
    """A run lists the connector's instances only while its creating hash holds an entry or it is pending deletion, and once its last successful listing is over an hour old."""

    async def test_within_the_hour_of_a_listing_a_run_lists_nothing_so_an_unrecorded_instance_waits(
        self, harness: Harness, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        listed_at = time.time() - 60
        await _seed(redis_client, emptiable, AutoscalerState(since=time.time(), listed_at=listed_at))
        harness.account.boot(emptiable, "octo-1-abcdef")
        await harness.run(emptiable)

        assert harness.account.listings == 0 and harness.account.terminated == []
        assert (await _state(redis_client, emptiable)).listed_at == listed_at

    @pytest.mark.parametrize("age", [10, CREATE_TIMEOUT_SECONDS + 5], ids=["fresh", "stale"])
    async def test_an_entry_in_the_creating_hash_makes_a_run_list(
        self, age: float, harness: Harness, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        await _seed(redis_client, emptiable, AutoscalerState(since=time.time(), listed_at=time.time() - 60))
        await _entry(redis_client, emptiable, "octo-1-abcdef", "scale", age=age)
        unrecorded = harness.account.boot(emptiable, "octo-2-bbbbbb")
        await harness.run(emptiable)

        assert harness.account.listings == 1 and harness.account.terminated == [unrecorded.proxy_id]
        assert (await _state(redis_client, emptiable)).listed_at == pytest.approx(time.time(), abs=5)

    async def test_a_connector_pending_deletion_lists_at_every_run(
        self, harness: Harness, rows: Rows, doomed: Connector, redis_client: RedisClient
    ) -> None:
        await _seed(redis_client, doomed, AutoscalerState(since=time.time(), listed_at=time.time() - 60))
        instance = harness.account.boot(doomed, "octo-1-abcdef")
        await harness.run(doomed)
        assert harness.account.terminated == [instance.proxy_id]
        assert await rows.connector_as_stored(doomed.id) is not None

        # The next run, within the hour of the first's listing, lists again: nothing is left, so the connector goes.
        await harness.run(doomed)
        assert harness.account.listings == 2
        assert await rows.connector_as_stored(doomed.id) is None

    @pytest.mark.parametrize("ago", [None, LISTING_INTERVAL_SECONDS + 1], ids=["none-recorded", "over-an-hour-ago"])
    async def test_a_run_lists_when_no_listing_succeeded_within_the_hour(
        self, ago: float | None, harness: Harness, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        listed_at = None if ago is None else time.time() - ago
        await _seed(redis_client, emptiable, AutoscalerState(since=time.time(), listed_at=listed_at))
        instance = harness.account.boot(emptiable, "octo-1-abcdef")
        await harness.run(emptiable)

        assert harness.account.listings == 1 and harness.account.terminated == [instance.proxy_id]
        assert (await _state(redis_client, emptiable)).listed_at == pytest.approx(time.time(), abs=5)

    async def test_a_successful_listing_is_recorded_and_the_next_comes_an_hour_later(
        self, harness: Harness, emptiable: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = Clock(time.time())
        monkeypatch.setattr("api.jobs.scale.time", clock)
        await harness.run(emptiable)
        assert harness.account.listings == 1
        assert (await _state(redis_client, emptiable)).listed_at == clock.now

        # An instance whose creator lost its entry, booted after that listing: a run an hour on still leaves it.
        instance = harness.account.boot(emptiable, "octo-1-abcdef")
        clock.now += LISTING_INTERVAL_SECONDS
        await harness.run(emptiable)
        assert harness.account.listings == 1 and harness.account.terminated == []

        clock.now += 1
        await harness.run(emptiable)
        assert harness.account.listings == 2 and harness.account.terminated == [instance.proxy_id]

    async def test_a_failed_listing_records_its_error_and_the_next_waits_for_the_backoff(
        self, harness: Harness, rows: Rows, emptiable: Connector, redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        listed_at = time.time() - LISTING_INTERVAL_SECONDS - 1
        await _seed(redis_client, emptiable, AutoscalerState(since=time.time(), listed_at=listed_at))
        instance = harness.account.boot(emptiable, "octo-1-abcdef")
        harness.account.list_error = RuntimeError("API down")
        await harness.run(emptiable)
        assert harness.account.terminated == []
        assert (await _state(redis_client, emptiable)).listed_at == listed_at
        stored = await rows.connector_as_stored(emptiable.id)
        assert stored is not None
        assert (stored.last_error, stored.consecutive_errors) == (f"{LISTING_ERROR_PREFIX}API down", 1)

        # Within the backoff a run lists nothing, failing or not.
        harness.account.list_error = None
        await harness.run(emptiable)
        assert harness.account.listings == 1

        # Once it is over the run lists, and the success clears the listing's error.
        await _backoff_over(sessions, emptiable)
        await harness.run(emptiable)
        assert harness.account.listings == 2 and harness.account.terminated == [instance.proxy_id]
        assert await _no_error(rows, emptiable)

    async def test_failed_listings_count_the_backoff_up(
        self, harness: Harness, rows: Rows, emptiable: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        harness.account.list_error = RuntimeError("API down")
        for _ in range(3):
            await harness.run(emptiable)
            await _backoff_over(sessions, emptiable)
        stored = await rows.connector_as_stored(emptiable.id)
        assert stored is not None and stored.consecutive_errors == 3
        assert harness.account.listings == 3

    async def test_a_successful_listing_leaves_another_cloud_error_to_count_its_backoff(
        self, harness: Harness, rows: Rows, emptiable: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A create's error clears once a create succeeds: a listing that succeeds meanwhile leaves it, and its count, as they were."""
        await _with_error(sessions, emptiable, errors=3, minutes_ago=MAX_ERROR_BACKOFF_MINUTES + 1)
        await harness.run(emptiable)
        assert harness.account.listings == 1
        stored = await rows.connector_as_stored(emptiable.id)
        assert stored is not None
        assert (stored.last_error, stored.consecutive_errors) == ("Cloud provider error", 3)

    async def test_a_long_listing_error_is_recorded_cut_to_the_column(
        self, harness: Harness, rows: Rows, emptiable: Connector
    ) -> None:
        """A cloud SDK's error can carry trace and correlation IDs past the column's 1024 characters."""
        harness.account.list_error = RuntimeError("x" * 2000)
        await harness.run(emptiable)
        stored = await rows.connector_as_stored(emptiable.id)
        assert stored is not None and stored.last_error is not None
        assert len(stored.last_error) == 1024 and stored.last_error.startswith(LISTING_ERROR_PREFIX)


class TestDraining:
    async def _draining(self, rows: Rows, emptiable: Connector) -> Proxy:
        return (await _instances(rows, emptiable, 1, ProxyStatus.DRAINING))[0]

    async def test_an_instance_is_terminated_once_its_traffic_stood_still_for_two_checks(
        self,
        harness: Harness,
        rows: Rows,
        emptiable: Connector,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        proxy = await self._draining(rows, emptiable)
        async with sessions() as session:
            await MetricsRepository(session).upsert_totals(PROXY_WINDOWS, {proxy.id: MetricDelta(bytes_sent=1000, bytes_received=5000)})
            await session.commit()
        await _meter(redis_client, proxy, MetricDelta(bytes_sent=10))

        # First look: the baseline is taken from the totals plus the window.
        await harness.run(emptiable)
        assert (await _state(redis_client, emptiable)).drains == {proxy.id: Drain(1010, 5000)}

        # Traffic still flowing.
        await _meter(redis_client, proxy, MetricDelta(bytes_received=1))
        await harness.run(emptiable)
        assert (await _state(redis_client, emptiable)).drains == {proxy.id: Drain(1010, 5001)}

        # Nothing moved since: one quiet check, then a second, then terminating.
        await harness.run(emptiable)
        assert (await _state(redis_client, emptiable)).drains == {proxy.id: Drain(1010, 5001, 1)}
        assert await _statuses(rows, emptiable) == [ProxyStatus.DRAINING]
        await harness.run(emptiable)
        assert await _statuses(rows, emptiable) == [ProxyStatus.TERMINATING]
        assert (await _state(redis_client, emptiable)).drains == {}

    async def test_a_flush_between_two_checks_does_not_end_a_drain_that_carried_bytes(
        self,
        harness: Harness,
        rows: Rows,
        emptiable: Connector,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """A flush takes counts out of the live window seconds before it commits them, so one check sees them nowhere."""
        proxy = await self._draining(rows, emptiable)
        async with sessions() as session:
            await MetricsRepository(session).upsert_totals(PROXY_WINDOWS, {proxy.id: MetricDelta(bytes_sent=1000)})
            await session.commit()
        await harness.run(emptiable)
        assert (await _state(redis_client, emptiable)).drains == {proxy.id: Drain(1000, 0)}

        await _meter(redis_client, proxy, MetricDelta(bytes_sent=10))
        taken = await redis_client.take_metric_windows("1-0")
        await harness.run(emptiable)
        assert await _statuses(rows, emptiable) == [ProxyStatus.DRAINING]

        # The flush commits what it took: the next check sees the bytes and starts over.
        async with sessions() as session:
            await MetricsRepository(session).upsert_totals(PROXY_WINDOWS, taken.proxies)
            await session.commit()
        await redis_client.clear_metric_windows("1-0")
        await harness.run(emptiable)
        assert (await _state(redis_client, emptiable)).drains == {proxy.id: Drain(1010, 0)}

        # Two quiet checks end it.
        await harness.run(emptiable)
        assert await _statuses(rows, emptiable) == [ProxyStatus.DRAINING]
        await harness.run(emptiable)
        assert await _statuses(rows, emptiable) == [ProxyStatus.TERMINATING]


class TestTermination:
    async def test_a_terminating_instance_is_deleted_in_the_cloud_then_its_row(
        self, harness: Harness, rows: Rows, emptiable: Connector, redis_client: RedisClient
    ) -> None:
        [proxy] = await _instances(rows, emptiable, 1, ProxyStatus.TERMINATING)
        await harness.run(emptiable)
        assert harness.account.terminated == [proxy.id]
        assert await rows.of(emptiable) == []
        assert proxy.id not in (await _state(redis_client, emptiable)).rotations

    async def test_a_failed_termination_is_recorded_and_the_row_kept(
        self, harness: Harness, rows: Rows, emptiable: Connector
    ) -> None:
        await _instances(rows, emptiable, 1, ProxyStatus.TERMINATING)
        harness.account.terminate_error = RuntimeError("API down")
        await harness.run(emptiable)
        stored = await rows.connector_as_stored(emptiable.id)
        assert stored is not None and (stored.consecutive_errors, stored.last_error) == (1, "API down")
        assert await _statuses(rows, emptiable) == [ProxyStatus.TERMINATING]

    async def test_no_termination_while_backing_off(
        self, harness: Harness, rows: Rows, emptiable: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await _instances(rows, emptiable, 1, ProxyStatus.TERMINATING)
        await _with_error(sessions, emptiable, errors=2, minutes_ago=1)
        await harness.run(emptiable)
        assert harness.account.terminated == []

    async def test_a_run_held_in_a_slow_create_terminates_after_another_run_did_and_records_no_error(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [retiring] = await _instances(rows, aws, 1, ProxyStatus.TERMINATING)
        held = harness.account.hold()
        slow = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()

        # The second run counts the first's create, so scales nothing, and terminates the proxy.
        await harness.run(aws)
        assert harness.account.terminated == [retiring.id] and len(harness.account.created) == 1

        held.set()
        await slow
        assert harness.account.terminated == [retiring.id, retiring.id]
        assert await _no_error(rows, aws)
        assert [p.id for p in await rows.of(aws)] == [f"fake-{harness.account.created[0]}"]


class TestRotation:
    async def test_a_due_instance_is_replaced_under_an_entry_then_drained(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        held = harness.account.hold()
        rotating = asyncio.create_task(harness.run(aws))
        await harness.account.held_create()

        [name] = harness.account.created
        assert (await _creating(redis_client, aws))[name]["purpose"] == f"rotate:{old.id}"
        held.set()
        await rotating

        statuses = {p.id: p.status for p in await rows.of(aws)}
        assert statuses == {old.id: ProxyStatus.DRAINING, f"fake-{name}": ProxyStatus.INITIALIZING}
        assert (await _state(redis_client, aws)).rotations[f"fake-{name}"] > time.time()
        assert await _creating(redis_client, aws) == {}

    async def test_a_failed_replacement_is_recorded_and_the_instance_drains_anyway(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        harness.account.create_error = RuntimeError("quota")
        await harness.run(aws)
        assert await _statuses(rows, aws) == [ProxyStatus.DRAINING]
        stored = await rows.connector_as_stored(aws.id)
        assert stored is not None and stored.last_error == "quota"
        assert await _creating(redis_client, aws) == {}

    async def test_no_rotation_while_backing_off(
        self,
        harness: Harness,
        rows: Rows,
        aws: Connector,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        await _with_error(sessions, aws, errors=2, minutes_ago=1)
        await harness.run(aws)
        assert await _statuses(rows, aws) == [ProxyStatus.HEALTHY] and harness.account.created == []

    async def test_a_rotation_whose_read_fails_clears_its_entry_and_fails_the_run(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run is delivered again, and finds no entry of its own holding the proxy's rotation."""
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))

        async def failing(repository: ProxyRepository, proxy_id: str) -> Proxy | None:
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(ProxyRepository, "get_by_id", failing)
        with pytest.raises(ConnectionError):
            await harness.run(aws)

        assert harness.account.created == [] and await _creating(redis_client, aws) == {}
        assert await _statuses(rows, aws) == [ProxyStatus.HEALTHY]

    async def test_a_run_that_read_a_proxy_before_another_run_rotated_it_does_not_rotate_it_again(
        self, harness: Harness, rows: Rows, project: Project, redis_client: RedisClient
    ) -> None:
        pool = await rows.connector(project, "aws", config={**AWS_CONFIG, "min_proxies": 2})
        [old] = await _instances(rows, pool, 1)
        await _seed(redis_client, pool, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        held = harness.account.hold()
        slow = asyncio.create_task(harness.run(pool))
        await harness.account.held_create()

        # The second run rotates the proxy while the first is still creating.
        await harness.run(pool)
        assert len(harness.account.created) == 2
        held.set()
        await slow

        assert len(harness.account.created) == 2
        assert {p.id: p.status for p in await rows.of(pool)}[old.id] == ProxyStatus.DRAINING


class TestDisabledConnectors:
    async def test_a_disabled_connector_drains_terminates_and_once_empty_is_deleted_if_pending_deletion(
        self, harness: Harness, rows: Rows, doomed: Connector, redis_client: RedisClient
    ) -> None:
        await _instances(rows, doomed, 2)

        await harness.run(doomed)
        assert await _statuses(rows, doomed) == [ProxyStatus.DRAINING] * 2
        await harness.run(doomed)  # baselines
        await harness.run(doomed)  # one quiet check
        await harness.run(doomed)  # two: terminating
        assert await _statuses(rows, doomed) == [ProxyStatus.TERMINATING] * 2
        await harness.run(doomed)  # terminated
        assert await rows.of(doomed) == [] and len(harness.account.terminated) == 2

        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is None
        assert not await redis_client.client.exists(AUTOSCALER_STATE_KEY.format(connector_id=doomed.id))

    async def test_a_disabled_connector_that_is_kept_stays_once_empty(
        self, harness: Harness, rows: Rows, project: Project
    ) -> None:
        connector = await rows.connector(project, "aws", config=AWS_CONFIG, enabled=False)
        await harness.run(connector)
        assert await rows.connector_as_stored(connector.id) is not None

    async def test_a_disabled_connector_whose_listing_fails_shows_the_error_and_backs_off(
        self, harness: Harness, rows: Rows, project: Project
    ) -> None:
        """An expired cloud credential on a disabled connector: one listing, its error on the connector, then quiet runs every period of the backoff."""
        connector = await rows.connector(project, "aws", config=AWS_CONFIG, enabled=False)
        harness.account.list_error = RuntimeError("credential expired")
        for _ in range(5):
            await harness.run(connector)
        assert harness.account.listings == 1
        stored = await rows.connector_as_stored(connector.id)
        assert stored is not None
        assert (stored.last_error, stored.consecutive_errors) == (f"{LISTING_ERROR_PREFIX}credential expired", 1)

    async def test_a_connector_pending_deletion_waits_for_its_creating_entries(
        self, harness: Harness, rows: Rows, doomed: Connector, redis_client: RedisClient
    ) -> None:
        await _entry(redis_client, doomed, "octo-1-abcdef", "scale", age=10)
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is not None

        await redis_client.client.delete(SCALE_CREATING_KEY.format(connector_id=doomed.id))
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is None

    async def test_a_connector_pending_deletion_has_its_unrecorded_instances_terminated_and_goes_once_none_is_listed(
        self, harness: Harness, rows: Rows, doomed: Connector
    ) -> None:
        """Its creator died and its entry was lost: the instance has neither a row nor an entry, but the cloud bills it."""
        instance = harness.account.boot(doomed, "octo-1-abcdef")
        await harness.run(doomed)
        assert harness.account.terminated == [instance.proxy_id]
        assert await rows.connector_as_stored(doomed.id) is not None

        # The next run's listing is empty.
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is None

    async def test_a_listing_that_fails_holds_the_deletion(
        self, harness: Harness, rows: Rows, doomed: Connector, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        harness.account.list_error = RuntimeError("API down")
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is not None

        # Within the backoff the deletion waits even with the cloud answering again.
        harness.account.list_error = None
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is not None

        await _backoff_over(sessions, doomed)
        await harness.run(doomed)
        assert await rows.connector_as_stored(doomed.id) is None


class TestWhatIsScaled:
    async def test_a_connector_that_is_gone_has_both_hashes_deleted_and_one_that_is_no_cloud_is_skipped(
        self, harness: Harness, rows: Rows, project: Project, redis_client: RedisClient
    ) -> None:
        static = await rows.connector(project)
        await redis_client.client.hset(AUTOSCALER_STATE_KEY.format(connector_id="gone"), mapping={"since": "1"})
        await redis_client.client.hset(SCALE_CREATING_KEY.format(connector_id="gone"), "octo-1-abcdef", "{}")
        await harness.job.run(JobBatch(ids=("1-0", "2-0"), keys=("gone", static.id)))
        assert not await redis_client.client.exists(AUTOSCALER_STATE_KEY.format(connector_id="gone"))
        assert not await redis_client.client.exists(SCALE_CREATING_KEY.format(connector_id="gone"))
        assert not await redis_client.client.exists(AUTOSCALER_STATE_KEY.format(connector_id=static.id))

    async def test_a_cloud_type_without_a_cloud_api_is_skipped(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        registry: ProviderRegistry,
        proxy_service: ProxyService,
        connector_service: ConnectorService,
        rows: Rows,
        aws: Connector,
    ) -> None:
        job = AutoscaleJob(
            sessions, redis_client, registry, proxy_service, connector_service, Demand(redis_client),
            cloud=lambda connector, credential: None,
        )
        await job.run(JobBatch(ids=("1-0",), keys=(aws.id,)))
        assert await rows.of(aws) == []
        assert not await redis_client.client.exists(AUTOSCALER_STATE_KEY.format(connector_id=aws.id))

    async def test_instances_that_are_gone_leave_the_state(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        await _instances(rows, aws, 1)
        remembered = AutoscalerState(since=time.time(), rotations={"gone": 1e12}, drains={"gone": Drain(1, 1)})
        await _seed(redis_client, aws, remembered)
        await harness.run(aws)
        state = await _state(redis_client, aws)
        assert "gone" not in state.rotations and state.drains == {}


class TestErrorsWithoutText:
    """A cloud call cut by a deadline raises a TimeoutError with no text: each failure's log line names its type, and so does the error a failure records on the connector."""

    @staticmethod
    def _errors(logs: list[dict[str, Any]], event: str) -> list[str]:
        return [log["error"] for log in logs if log["event"] == event]

    @staticmethod
    async def _recorded(rows: Rows, connector: Connector) -> str | None:
        """The cloud error the connector holds as stored, which the Connectors page shows."""
        stored = await rows.connector_as_stored(connector.id)
        assert stored is not None
        return stored.last_error

    async def test_a_listing(self, harness: Harness, rows: Rows, aws: Connector) -> None:
        harness.account.list_error = TimeoutError()
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)
        assert self._errors(logs, "Failed to list the connector's instances") == ["TimeoutError"]
        assert await self._recorded(rows, aws) == f"{LISTING_ERROR_PREFIX}TimeoutError"

    async def test_a_termination_of_an_instance_with_no_row(self, harness: Harness, aws: Connector) -> None:
        harness.account.boot(aws, "octo-1-abcdef")
        harness.account.terminate_error = TimeoutError()
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)
        assert self._errors(logs, "Failed to terminate a cloud instance with no row") == ["TimeoutError"]

    async def test_a_create(self, harness: Harness, aws: Connector) -> None:
        harness.account.create_error = TimeoutError()
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)
        assert self._errors(logs, "Failed to create proxy instance") == ["TimeoutError"]

    async def test_a_replacement(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        harness.account.create_error = TimeoutError()
        with structlog.testing.capture_logs() as logs:
            await harness.run(aws)
        assert self._errors(logs, "Failed to create replacement proxy") == ["TimeoutError"]

    async def test_a_termination_of_a_terminating_instance(
        self, harness: Harness, rows: Rows, emptiable: Connector
    ) -> None:
        await _instances(rows, emptiable, 1, ProxyStatus.TERMINATING)
        harness.account.terminate_error = TimeoutError()
        with structlog.testing.capture_logs() as logs:
            await harness.run(emptiable)
        assert self._errors(logs, "Error terminating proxy instance") == ["TimeoutError"]

    async def test_a_create_records_its_type_on_the_connector(
        self, harness: Harness, rows: Rows, aws: Connector
    ) -> None:
        harness.account.create_error = TimeoutError()
        await harness.run(aws)
        assert await self._recorded(rows, aws) == "TimeoutError"

    async def test_a_replacement_records_its_type_on_the_connector(
        self, harness: Harness, rows: Rows, aws: Connector, redis_client: RedisClient
    ) -> None:
        [old] = await _instances(rows, aws, 1)
        await _seed(redis_client, aws, AutoscalerState(since=time.time(), rotations={old.id: time.time() - 60}))
        harness.account.create_error = TimeoutError()
        await harness.run(aws)
        assert await self._recorded(rows, aws) == "TimeoutError"

    async def test_a_termination_of_a_terminating_instance_records_its_type_on_the_connector(
        self, harness: Harness, rows: Rows, emptiable: Connector
    ) -> None:
        await _instances(rows, emptiable, 1, ProxyStatus.TERMINATING)
        harness.account.terminate_error = TimeoutError()
        await harness.run(emptiable)
        assert await self._recorded(rows, emptiable) == "TimeoutError"
