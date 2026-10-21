# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the job runner: a group consumer of one kind's job stream that hands each batch it reads to the kind's job.

Toy jobs record each batch and hold or fail it as the test says, on the session's Redis, with the claim timings shrunk
so a redelivery takes a fraction of a second.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

import api.core.consumers as consumers
import api.db.streams as streams
from api.core.job_stats import job_stats
from api.core.roles import JobKind
from api.core.workers import WorkerName
from api.db.redis import JOB_STREAM_KEY, WORKER_GROUP, RedisClient
from api.jobs.job import Job, JobBatch
from api.jobs.queue import JobQueue
from api.jobs.runner import JobRunner
from tests.waits import running, until


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Claims taken after 0.4 s, refreshes and reclaim checks every 0.1 s, and read back-offs of 0.05 s to 0.1 s."""
    monkeypatch.setattr(consumers, "CLAIM_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 0.1)
    monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(consumers, "STOP_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.1)
    job_stats.reset()
    yield
    job_stats.reset()


class Recorder(Job):
    """A sync job under test control: notes each batch and give-up, holds runs while ``released`` is clear, raises ``error``."""

    kind = JobKind.SYNC

    def __init__(self, *, batch_size: int = 1, concurrency: int = 1) -> None:
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.batches: list[JobBatch] = []
        self.given_up: list[JobBatch] = []
        self.released = asyncio.Event()
        self.released.set()
        self.error: Exception | None = None

    async def run(self, batch: JobBatch) -> None:
        self.batches.append(batch)
        await self.released.wait()
        if self.error is not None:
            raise self.error

    async def give_up(self, batch: JobBatch) -> None:
        self.given_up.append(batch)


class HealthRecorder(Recorder):
    """The same, as the health kind's job."""

    kind = JobKind.HEALTH


async def _length(redis_client: RedisClient, kind: JobKind) -> int:
    """Entries on the kind's stream: queued, running, or done and not completed."""
    return int(await redis_client.client.xlen(JOB_STREAM_KEY.format(kind=kind)))


async def _pending(redis_client: RedisClient, kind: JobKind) -> int:
    """Entries of the kind delivered to a worker and not acknowledged."""
    summary: dict[str, Any] = await redis_client.client.xpending(JOB_STREAM_KEY.format(kind=kind), WORKER_GROUP)
    return int(summary["pending"])


class TestBatches:
    async def test_a_runner_takes_only_its_own_kind(self, redis_client: RedisClient, jobs: JobQueue) -> None:
        sync, health = Recorder(), HealthRecorder()
        sync_runner, health_runner = JobRunner(redis_client, sync), JobRunner(redis_client, health)
        assert (sync_runner.name, health_runner.name) == (WorkerName.JOB_RUNNER_SYNC, WorkerName.JOB_RUNNER_HEALTH)

        async with running(sync_runner), running(health_runner):
            await jobs.publish(JobKind.SYNC, ["connector-1"])
            await jobs.publish(JobKind.HEALTH, ["proxy-1"])
            await until(lambda: len(sync.batches) == 1 and len(health.batches) == 1)

        assert [batch.keys for batch in sync.batches] == [("connector-1",)]
        assert [batch.keys for batch in health.batches] == [("proxy-1",)]

    async def test_it_runs_up_to_its_concurrency_of_batches_of_its_batch_size(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder(batch_size=2, concurrency=2)
        job.released.clear()
        runner = JobRunner(redis_client, job)
        await jobs.publish(JobKind.SYNC, ["a", "b", "c", "d", "e"])

        async with running(runner):
            await until(lambda: len(job.batches) == 2)
            await asyncio.sleep(0.3)
            # Both batches running: the fifth key waits for one of them.
            assert [batch.keys for batch in job.batches] == [("a", "b"), ("c", "d")]
            assert runner.in_flight == 4
            job.released.set()
            await until(lambda: len(job.batches) == 3)

        assert job.batches[2].keys == ("e",)

    async def test_a_key_read_twice_in_a_batch_runs_once_and_every_entry_is_completed(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder(batch_size=10)
        runner = JobRunner(redis_client, job)
        await jobs.publish(JobKind.SYNC, ["a", "a", "b"])

        async with running(runner):
            await until(lambda: runner.consumed == 3)

        (batch,) = job.batches
        assert batch.keys == ("a", "b") and len(batch.ids) == 3
        assert await _length(redis_client, JobKind.SYNC) == 0

    async def test_the_entries_stay_pending_while_the_run_lasts_and_are_completed_after_it(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder()
        job.released.clear()
        runner = JobRunner(redis_client, job)
        await jobs.publish(JobKind.SYNC, ["a"])

        async with running(runner):
            await until(lambda: len(job.batches) == 1)
            # Held past the claim timeout: refreshed, so no reclaim runs it again.
            await asyncio.sleep(0.6)
            assert (await _length(redis_client, JobKind.SYNC), await _pending(redis_client, JobKind.SYNC)) == (1, 1)
            job.released.set()
            await until(lambda: runner.consumed == 1)

        assert len(job.batches) == 1
        assert (await _length(redis_client, JobKind.SYNC), await _pending(redis_client, JobKind.SYNC)) == (0, 0)

    async def test_each_batch_is_one_run_of_the_kind_in_the_job_stats(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder()
        runner = JobRunner(redis_client, job)
        await jobs.publish(JobKind.SYNC, ["a", "b"])

        async with running(runner):
            await until(lambda: runner.consumed == 2)

        stats = job_stats.get(WorkerName.JOB_RUNNER_SYNC)
        assert stats is not None and (stats.runs, stats.failures) == (2, 0)


class TestFailures:
    async def test_a_run_that_raises_is_delivered_again_then_given_up_after_five_deliveries(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder()
        job.error = RuntimeError("vendor down")
        runner = JobRunner(redis_client, job)
        await jobs.publish(JobKind.SYNC, ["connector-1"])

        with structlog.testing.capture_logs() as logs:
            async with running(runner):
                await until(lambda: runner.dropped == 1, timeout=10)

        assert [batch.keys for batch in job.batches] == [("connector-1",)] * 5
        assert [batch.keys for batch in job.given_up] == [("connector-1",)]
        assert await _length(redis_client, JobKind.SYNC) == 0
        given_up = [log for log in logs if log["event"] == "Job given up after repeated deliveries"]
        assert [(log["log_level"], log["kind"], log["keys"]) for log in given_up] == [("warning", "sync", ["connector-1"])]
        stats = job_stats.get(WorkerName.JOB_RUNNER_SYNC)
        assert stats is not None and (stats.runs, stats.failures) == (5, 5)

    async def test_an_entry_without_a_key_is_logged_and_completed_without_a_run(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        job = Recorder(batch_size=10)
        runner = JobRunner(redis_client, job)
        stream = JOB_STREAM_KEY.format(kind=JobKind.SYNC)
        keyless = await redis_client.client.xadd(stream, {"item": "not a job"})

        with structlog.testing.capture_logs() as logs:
            async with running(runner):
                await until(lambda: runner.consumed == 1)
                await jobs.publish(JobKind.SYNC, ["a"])
                await until(lambda: runner.consumed == 2)

        assert [batch.keys for batch in job.batches] == [("a",)]
        assert await _length(redis_client, JobKind.SYNC) == 0
        dropped = [log for log in logs if log["event"] == "Job entries without a key dropped"]
        assert [(log["log_level"], log["kind"], log["ids"]) for log in dropped] == [("warning", "sync", [keyless])]

    async def test_a_wipe_is_logged_under_the_stream_s_name_in_the_system_view(
        self, redis_client: RedisClient, jobs: JobQueue
    ) -> None:
        """The warning names the kind's stream, as the System page does, not the runner's loop; the runner goes on."""
        job = Recorder()
        runner = JobRunner(redis_client, job)
        missing = "Stream consumer group was missing; created again"

        with structlog.testing.capture_logs() as logs:
            async with running(runner):
                await jobs.publish(JobKind.SYNC, ["a"])
                await until(lambda: runner.consumed == 1)
                await redis_client.client.flushdb()
                await until(lambda: any(log["event"] == missing for log in logs))
                await jobs.publish(JobKind.SYNC, ["b"])
                await until(lambda: runner.consumed == 2)

        warned = [(log["log_level"], log["stream"], log["group"]) for log in logs if log["event"] == missing]
        assert warned == [("warning", "jobs.sync", WORKER_GROUP)]
        assert [batch.keys for batch in job.batches] == [("a",), ("b",)]
