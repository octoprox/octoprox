# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the traffic job against the real Postgres and Redis test containers.

A connector's usage is its ``connector_metrics`` history in the window plus its live Redis window; the harness writes
both where the flush and the gateways write them, and reads back the blocks hash and the runtime stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import pytest
from redis.asyncio.client import Pipeline
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from api.core import utc_now
from api.core.roles import JobKind
from api.core.stats import MetricDelta
from api.db.redis import (
    CONNECTOR_WINDOWS,
    RUNTIME_STREAM_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
    EntityWindows,
    RedisClient,
)
from api.db.repository import ConnectorRepository, MetricsRepository
from api.db.streams import RuntimeSignal, StreamEntry
from api.feed.state import RuntimeStateReader
from api.jobs.job import JobBatch
from api.jobs.traffic import TrafficJob
from api.models.connector import Connector
from tests.jobs.conftest import Rows, counted_selects

LIMIT = 1000
REACHED = "Connector traffic limit reached"
APPROACHING = "Connector approaching its traffic limit"
BLOCKED = "Connector blocked by its traffic limit"
RELEASED = "Connector released from its traffic limit"


def batch_of(*connector_ids: str) -> JobBatch:
    """The batch a runner hands the job for entries naming ``connector_ids``, in order."""
    return JobBatch.of([StreamEntry(f"{n}-0", {"key": connector_id}) for n, connector_id in enumerate(connector_ids, 1)])


def period_end(connector: Connector) -> datetime:
    """When the connector's current usage window ends: the end of every block decided now."""
    return connector.parsed_traffic_config.usage_window(utc_now(), connector.traffic_reset_at)[1]


def events(logs: Iterable[Mapping[str, Any]]) -> list[str]:
    return [log["event"] for log in logs]


class Harness:
    """Connectors in Postgres, their bytes where the flush and the gateways write them, and one worker's job."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, rows: Rows) -> None:
        self.sessions = sessions
        self.redis = redis_client
        self.rows = rows
        self.job = TrafficJob(sessions, redis_client)

    async def connector(self, **traffic: Any) -> Connector:
        """A connector of a project of its own, with ``traffic`` as its traffic config."""
        return await self.rows.connector(await self.rows.project(), traffic_config=traffic)

    async def edit(self, connector: Connector, **changes: Any) -> Connector:
        """The connector as an admin's edit leaves it in Postgres."""
        edited = connector.model_copy(update=changes)
        async with self.sessions() as session:
            await ConnectorRepository(session).update(edited)
            await session.commit()
        return edited

    async def delete(self, connector: Connector) -> None:
        async with self.sessions() as session:
            await ConnectorRepository(session).delete(connector.id)
            await session.commit()

    async def history(self, connector: Connector, received: int, sent: int = 0) -> None:
        """Bytes a metrics flush committed to ``connector_metrics`` now."""
        async with self.sessions() as session:
            delta = MetricDelta(bytes_sent=sent, bytes_received=received)
            await MetricsRepository(session).save_connector_metrics_snapshots({connector.id: delta})
            await session.commit()

    async def window(self, connector: Connector, received: int, sent: int = 0) -> None:
        """Bytes the gateways added to the connector's live window, as metering adds them."""
        pipe = self.redis.client.pipeline(transaction=True)
        delta = MetricDelta(bytes_sent=sent, bytes_received=received)
        CONNECTOR_WINDOWS.add(pipe, CONNECTOR_WINDOWS.key(connector.id), delta, utc_now().isoformat())
        await pipe.execute()

    async def decide(self, *connectors: Connector, job: TrafficJob | None = None) -> None:
        """One run of a job (this worker's by default) over a batch naming ``connectors``."""
        await (job or self.job).run(batch_of(*(connector.id for connector in connectors)))

    async def blocked(self) -> dict[str, datetime]:
        """The blocks hash as the feed builder reads it."""
        return await RuntimeStateReader(self.redis).blocked()

    async def ask(self, connector: Connector) -> str:
        """Mark the connector waiting for a decision, as the api marks it after a reset or a limit edit; the mark."""
        asked = utc_now().isoformat()
        await self.redis.client.hset(TRAFFIC_PENDING_KEY, connector.id, asked)
        return asked

    async def pending(self) -> dict[str, str]:
        """The connectors the api shows as waiting for a decision, with their marks."""
        return await RuntimeStateReader(self.redis).pending()

    async def announced(self) -> list[tuple[str, str]]:
        """Every entry of the runtime stream so far, as (kind, ref), oldest first."""
        entries: Any = await self.redis.client.xrange(RUNTIME_STREAM_KEY)
        return [(fields["kind"], fields["ref"]) for _entry_id, fields in entries]


@pytest.fixture
def harness(sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, rows: Rows) -> Harness:
    return Harness(sessions, redis_client, rows)


class TestBlocking:
    @pytest.mark.parametrize("action", ["block", "interrupt"])
    async def test_a_connector_at_its_limit_from_history_and_live_window_is_blocked_until_its_period_ends(
        self, harness: Harness, action: str
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action=action)
        await harness.history(connector, 600)
        await harness.window(connector, 399)
        await harness.decide(connector)
        # Each half alone, and both together, are under the limit.
        assert await harness.blocked() == {}

        await harness.window(connector, 1)
        with capture_logs() as logs:
            await harness.decide(connector)

        assert await harness.blocked() == {connector.id: period_end(connector)}
        # Stored as ISO, naive UTC.
        assert await harness.redis.client.hget(TRAFFIC_BLOCKED_KEY, connector.id) == period_end(connector).isoformat()
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, connector.id)]
        assert events(logs) == [REACHED, BLOCKED]
        assert logs[1]["until"] == period_end(connector).isoformat()
        assert logs[1]["action"] == action

    async def test_bytes_in_both_directions_count_against_the_limit(self, harness: Harness) -> None:
        """An upload-heavy connector is held to its cap like any other: sent and received add up, in both halves."""
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.history(connector, received=300, sent=300)
        await harness.window(connector, received=250, sent=150)

        await harness.decide(connector)

        assert connector.id in await harness.blocked()

    async def test_a_limit_set_on_a_connector_already_over_it_blocks_at_the_next_decision(
        self, harness: Harness
    ) -> None:
        connector = await harness.connector()
        await harness.history(connector, 1500)
        await harness.decide(connector)
        assert await harness.blocked() == {}

        connector = await harness.edit(connector, traffic_config={"limit_bytes": LIMIT, "action": "block"})
        await harness.decide(connector)

        assert await harness.blocked() == {connector.id: period_end(connector)}

    async def test_alert_only_flags_the_limit_and_never_blocks(self, harness: Harness) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="alert")
        await harness.window(connector, 2 * LIMIT)

        with capture_logs() as logs:
            await harness.decide(connector)
            await harness.decide(connector)

        assert await harness.blocked() == {}
        assert await harness.announced() == []
        assert events(logs) == [REACHED]

    async def test_a_reset_starts_the_window_at_the_reset(self, harness: Harness) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.history(connector, 1500)
        await harness.decide(connector)
        assert connector.id in await harness.blocked()

        # The admin resets the usage: what was flushed before it no longer counts.
        connector = await harness.edit(connector, traffic_reset_at=utc_now())
        await harness.decide(connector)
        assert await harness.blocked() == {}

        # What is carried after it does, up to the period's end, which the reset does not move.
        await harness.history(connector, 600)
        await harness.window(connector, 400)
        await harness.decide(connector)
        assert await harness.blocked() == {connector.id: period_end(connector)}


class TestChanges:
    async def test_the_hash_and_the_runtime_stream_are_written_only_when_the_decision_changes(
        self, harness: Harness
    ) -> None:
        under = await harness.connector(limit_bytes=LIMIT, action="block")
        over = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(under, 10)
        await harness.window(over, 2 * LIMIT)

        for _ in range(3):
            await harness.decide(under, over)

        assert await harness.blocked() == {over.id: period_end(over)}
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, over.id)]

    async def test_two_workers_deciding_alike_at_once_append_one_entry_and_log_one_block(
        self, harness: Harness, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.history(connector, 2 * LIMIT)
        other = TrafficJob(sessions, redis_client)

        with capture_logs() as logs:
            await asyncio.gather(harness.decide(connector), harness.decide(connector, job=other))

        assert await harness.blocked() == {connector.id: period_end(connector)}
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, connector.id)]
        # Each process logs reaching the limit; the block is logged by the one whose write took.
        assert sorted(events(logs)) == sorted([REACHED, REACHED, BLOCKED])

    @pytest.mark.parametrize(
        ("traffic_config", "reason"),
        [
            ({"limit_bytes": 10 * LIMIT, "action": "block"}, "under_limit"),
            ({}, "no_limit"),
            ({"limit_bytes": LIMIT, "action": "alert"}, "alert_only"),
        ],
        ids=["raised", "taken_away", "alert_only"],
    )
    async def test_an_edit_that_lifts_the_block_releases_the_connector(
        self, harness: Harness, traffic_config: dict[str, Any], reason: str
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(connector, 1500)
        await harness.decide(connector)
        assert connector.id in await harness.blocked()

        await harness.edit(connector, traffic_config=traffic_config)
        with capture_logs() as logs:
            await harness.decide(connector)

        assert await harness.blocked() == {}
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, connector.id)] * 2
        released = [log for log in logs if log["event"] == RELEASED]
        assert [(log["connector_id"], log["connector"], log["reason"]) for log in released] == [
            (connector.id, connector.name, reason)
        ]

    async def test_a_deleted_connector_is_released(self, harness: Harness) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(connector, 1500)
        await harness.decide(connector)

        await harness.delete(connector)
        with capture_logs() as logs:
            await harness.decide(connector)

        assert await harness.blocked() == {}
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, connector.id)] * 2
        assert [(log["event"], log["connector"], log["reason"]) for log in logs] == [(RELEASED, None, "deleted")]

    async def test_an_entry_past_its_period_is_replaced_by_the_next_decision(self, harness: Harness) -> None:
        under = await harness.connector(limit_bytes=LIMIT, action="block")
        over = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(over, 2 * LIMIT)
        # Blocks of a period that ended: the feed builder skips them, and a decision clears or renews them.
        await harness.redis.client.hset(
            TRAFFIC_BLOCKED_KEY, mapping={under.id: "2026-01-01T00:00:00", over.id: "2026-01-01T00:00:00"}
        )

        await harness.decide(under, over)

        assert await harness.blocked() == {over.id: period_end(over)}
        assert await harness.announced() == [
            (RuntimeSignal.TRAFFIC.value, under.id),
            (RuntimeSignal.TRAFFIC.value, over.id),
        ]

    async def test_an_unknown_connector_without_an_entry_writes_nothing(self, harness: Harness) -> None:
        await harness.job.run(batch_of("no-such-connector"))

        assert await harness.blocked() == {}
        assert await harness.announced() == []

    async def test_the_runtime_stream_is_capped_as_a_change_is_announced(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = harness.redis.client.pipeline(transaction=False)
        for n in range(250):
            pipe.xadd(RUNTIME_STREAM_KEY, {"kind": RuntimeSignal.QUARANTINE.value, "ref": f"proxy-{n}"})
        await pipe.execute()
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(connector, 2 * LIMIT)
        monkeypatch.setattr("api.jobs.traffic.RUNTIME_STREAM_MAXLEN", 1)

        await harness.decide(connector)

        # Trimmed a whole stream node at a time, the newest entry kept.
        assert await harness.redis.client.xlen(RUNTIME_STREAM_KEY) < 251
        assert (await harness.announced())[-1] == (RuntimeSignal.TRAFFIC.value, connector.id)


class TestPendingRequests:
    """A reset or a limit edit marks its connector pending; a decision whose reads started after the mark settles it."""

    async def test_a_decision_settles_the_request_it_answers_whether_or_not_the_block_changes(
        self, harness: Harness
    ) -> None:
        raised = await harness.connector(limit_bytes=LIMIT, action="block")
        steady = await harness.connector(limit_bytes=LIMIT, action="block")
        gone = await harness.connector(limit_bytes=LIMIT, action="block")
        elsewhere = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(raised, 1500)
        await harness.decide(raised)
        raised = await harness.edit(raised, traffic_config={"limit_bytes": 10 * LIMIT, "action": "block"})
        await harness.delete(gone)
        for connector in (raised, steady, gone):
            await harness.ask(connector)
        # A connector this batch does not decide keeps its mark.
        theirs = await harness.ask(elsewhere)

        await harness.decide(raised, steady, gone)

        assert await harness.pending() == {elsewhere.id: theirs}
        assert await harness.blocked() == {}
        # The release is announced; the decisions that changed nothing are not.
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, raised.id)] * 2

    async def test_a_request_marked_after_the_reads_started_waits_for_a_decision_of_its_own(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decision read the connector before the edit committed, so it cannot answer for it."""
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(connector, 1500)
        get_many = ConnectorRepository.get_many
        marks: list[str] = []

        async def read_then_edited(self: ConnectorRepository, connector_ids: Any) -> list[Connector]:
            found = await get_many(self, connector_ids)
            # The limit raised and committed, then the api's mark, while this decision reads.
            await harness.edit(connector, traffic_config={"limit_bytes": 10 * LIMIT, "action": "block"})
            marks.append(await harness.ask(connector))
            return found

        monkeypatch.setattr(ConnectorRepository, "get_many", read_then_edited)
        await harness.decide(connector)

        # Decided on the limit it read, and still waiting for the decision the edit asked for.
        assert connector.id in await harness.blocked()
        assert await harness.pending() == {connector.id: marks[0]}
        monkeypatch.undo()
        await harness.decide(connector)
        assert (await harness.blocked(), await harness.pending()) == ({}, {})

    async def test_a_newer_request_made_during_the_reads_replaces_the_one_read_and_waits(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.ask(connector)
        get_many = ConnectorRepository.get_many
        marks: list[str] = []

        async def read_then_asked_again(self: ConnectorRepository, connector_ids: Any) -> list[Connector]:
            found = await get_many(self, connector_ids)
            await asyncio.sleep(0.001)
            marks.append(await harness.ask(connector))
            return found

        monkeypatch.setattr(ConnectorRepository, "get_many", read_then_asked_again)
        await harness.decide(connector)

        assert await harness.pending() == {connector.id: marks[0]}


class TestFailures:
    async def test_a_decision_postgres_cannot_give_raises_and_leaves_the_block(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.history(connector, 1500)
        await harness.decide(connector)
        await harness.edit(connector, traffic_config={"limit_bytes": 10 * LIMIT, "action": "block"})

        async def unreachable(self: MetricsRepository, since: dict[str, datetime]) -> dict[str, dict[str, int]]:
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(MetricsRepository, "get_connector_totals_since", unreachable)
        # Raising leaves the batch pending: it is decided again on its next delivery.
        with pytest.raises(ConnectionError):
            await harness.decide(connector)

        assert await harness.blocked() == {connector.id: period_end(connector)}
        assert await harness.announced() == [(RuntimeSignal.TRAFFIC.value, connector.id)]

    async def test_a_write_redis_refuses_raises(self, harness: Harness) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.window(connector, 1500)
        await harness.redis.client.set(TRAFFIC_BLOCKED_KEY, "not a hash")

        with capture_logs() as logs, pytest.raises(ResponseError):
            await harness.decide(connector)

        assert BLOCKED not in events(logs)


class TestLogs:
    async def test_approaching_and_reaching_the_limit_are_logged_once_per_window_in_each_process(
        self, harness: Harness, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block", warn_percent=80)
        await harness.history(connector, 850)
        with capture_logs() as logs:
            await harness.decide(connector)
            await harness.decide(connector)
        assert events(logs) == [APPROACHING]
        assert (logs[0]["total_bytes"], logs[0]["limit_bytes"], logs[0]["percent"]) == (850, LIMIT, 85.0)

        await harness.window(connector, 200)
        with capture_logs() as logs:
            await harness.decide(connector)
            await harness.decide(connector)
        assert events(logs) == [REACHED, BLOCKED]
        assert (logs[0]["total_bytes"], logs[0]["period_end"]) == (1050, period_end(connector).isoformat())

        # Another worker process logs the limit once of its own; the block is no change to it.
        other = TrafficJob(sessions, redis_client)
        with capture_logs() as logs:
            await harness.decide(connector, job=other)
            await harness.decide(connector, job=other)
        assert events(logs) == [REACHED]

        # A reset starts a new window, whose approach is logged again.
        await harness.redis.client.delete(CONNECTOR_WINDOWS.key(connector.id))
        connector = await harness.edit(connector, traffic_reset_at=utc_now())
        await harness.history(connector, 900)
        with capture_logs() as logs:
            await harness.decide(connector)
        assert events(logs) == [APPROACHING, RELEASED]

    async def test_a_limit_taken_away_and_set_again_is_logged_again(self, harness: Harness) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="alert")
        await harness.window(connector, 1500)
        await harness.decide(connector)
        limit = connector.traffic_config

        await harness.edit(connector, traffic_config={})
        await harness.decide(connector)
        await harness.edit(connector, traffic_config=limit)
        with capture_logs() as logs:
            await harness.decide(connector)

        assert events(logs) == [REACHED]


class TestBatches:
    def test_a_worker_takes_the_traffic_stream_a_hundred_connectors_a_run_one_run_at_a_time(self) -> None:
        assert (TrafficJob.kind, TrafficJob.batch_size, TrafficJob.concurrency) == (JobKind.TRAFFIC, 100, 1)

    async def test_a_batch_reads_postgres_twice_and_the_live_windows_once_then_writes_each_connector_once_pipelined(
        self, harness: Harness, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = await harness.connector(limit_bytes=LIMIT, action="block")
        second = await harness.connector(limit_bytes=LIMIT, action="block", period="day")
        unlimited = await harness.connector()
        totals = MetricsRepository.get_connector_totals_since
        reads: list[dict[str, datetime]] = []

        async def counted(self: MetricsRepository, since: dict[str, datetime]) -> dict[str, dict[str, int]]:
            reads.append(since)
            return await totals(self, since)

        execute = Pipeline.execute
        pipelines: list[list[str]] = []

        async def recorded(self: Pipeline, raise_on_error: bool = True) -> list[Any]:
            pipelines.append([str(args[0]) for args, _options in self.command_stack])
            return await execute(self, raise_on_error)

        monkeypatch.setattr(MetricsRepository, "get_connector_totals_since", counted)
        monkeypatch.setattr(Pipeline, "execute", recorded)
        with counted_selects(db_engine) as selects:
            await harness.job.run(batch_of(first.id, second.id, first.id, unlimited.id, second.id))

        # The connectors, then the totals of those with a limit, each from its own window's start.
        assert len(selects) == 2
        assert reads == [
            {
                first.id: first.parsed_traffic_config.usage_window(utc_now(), None)[0],
                second.id: second.parsed_traffic_config.usage_window(utc_now(), None)[0],
            }
        ]
        # The live windows of those with a limit, then one decision per connector, in one pipeline each.
        assert pipelines == [["HGETALL", "HGETALL"], ["EVALSHA", "EVALSHA", "EVALSHA"]]

    async def test_the_history_is_read_before_the_live_window_so_a_flush_between_them_is_never_counted_twice(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = await harness.connector(limit_bytes=LIMIT, action="block")
        await harness.history(connector, 300)
        await harness.window(connector, 400)
        totals, windows = MetricsRepository.get_connector_totals_since, RuntimeStateReader.windows
        order: list[str] = []

        async def between_the_reads(read: str) -> None:
            """A metrics flush after the first of the two reads: the take, the commit, the clear."""
            order.append(read)
            if len(order) == 1:
                taken = await harness.redis.take_metric_windows("1-0")
                async with harness.sessions() as session:
                    await MetricsRepository(session).save_connector_metrics_snapshots(taken.connectors)
                    await session.commit()
                await harness.redis.clear_metric_windows("1-0")

        async def history(self: MetricsRepository, since: dict[str, datetime]) -> dict[str, dict[str, int]]:
            found = await totals(self, since)
            await between_the_reads("history")
            return found

        async def live(self: RuntimeStateReader, kind: EntityWindows, ids: Any) -> dict[str, MetricDelta]:
            found = await windows(self, kind, ids)
            await between_the_reads("live")
            return found

        monkeypatch.setattr(MetricsRepository, "get_connector_totals_since", history)
        monkeypatch.setattr(RuntimeStateReader, "windows", live)
        await harness.decide(connector)

        assert order == ["history", "live"]
        # 700 bytes carried: the flush's 400 are missed for this decision, never counted twice into a block.
        assert await harness.blocked() == {}
        monkeypatch.undo()
        await harness.window(connector, 300)
        await harness.decide(connector)
        assert connector.id in await harness.blocked()
