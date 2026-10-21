# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the job queue against the real Redis test container: entries per key on each kind's stream.

A Redis that goes away is staged by refusing a pipeline as a lost connection would, and one that stops answering by
pausing it.
"""

import time
from typing import Any

import pytest
import redis.exceptions
import structlog

from api.core.roles import JobKind
from api.db.redis import JOB_STREAM_KEY, RedisClient
from api.jobs import queue as queue_module
from api.jobs.queue import PUBLISH_BATCH, JobQueue
from tests.conftest import queued_job_keys

KIND = JobKind.SYNC


class Pipelines:
    """Counts the pipelines a client executes, and refuses the one numbered ``refuse`` (from 1) as a lost Redis would."""

    def __init__(self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch, refuse: int | None = None) -> None:
        self.executed = 0
        self._refuse = refuse
        self._pipeline = redis_client.client.pipeline
        monkeypatch.setattr(redis_client.client, "pipeline", self._counted)

    def _counted(self, *args: Any, **kwargs: Any) -> Any:
        pipe = self._pipeline(*args, **kwargs)
        execute = pipe.execute

        async def counted(*execute_args: Any, **execute_kwargs: Any) -> Any:
            self.executed += 1
            if self.executed == self._refuse:
                raise redis.exceptions.ConnectionError("Redis went away")
            return await execute(*execute_args, **execute_kwargs)

        pipe.execute = counted
        return pipe


def failed_requests(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The log lines of the job requests that failed after a write."""
    return [log for log in logs if log["event"] == "Redis step after a write failed" and log["step"] == "request jobs"]


@pytest.fixture
def jobs(redis_client: RedisClient) -> JobQueue:
    return JobQueue(redis_client)


class TestJobQueue:
    """Job entries, one per key: published by the producers and the jobs, asked for after a write by the services."""

    async def test_publish_appends_an_entry_per_key_in_order_on_the_kind_s_stream(
        self, jobs: JobQueue, redis_client: RedisClient
    ) -> None:
        """Every key as given, repeats too: a worker's batch runs a repeated key once."""
        assert await jobs.publish(KIND, ["proxy-b", "proxy-a", "proxy-b"]) == 3

        entries = await redis_client.client.xrange(JOB_STREAM_KEY.format(kind=KIND))
        assert [fields for _id, fields in entries] == [{"key": "proxy-b"}, {"key": "proxy-a"}, {"key": "proxy-b"}]
        assert await queued_job_keys(redis_client, JobKind.HEALTH) == []

    async def test_publish_sends_a_pipeline_per_500_keys_and_none_for_no_keys(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slice of a large pool pays an exchange per batch, not per key, and no exchange runs long."""
        keys = [f"proxy-{n}" for n in range(2 * PUBLISH_BATCH + 1)]
        pipelines = Pipelines(redis_client, monkeypatch)

        assert await jobs.publish(KIND, keys) == len(keys)
        assert pipelines.executed == 3
        assert await queued_job_keys(redis_client, KIND) == keys

        assert await jobs.publish(KIND, []) == 0
        assert pipelines.executed == 3

    async def test_a_stream_no_worker_takes_from_stays_about_its_cap(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trimmed as it is written, oldest first and approximately: Redis drops whole nodes, so a little over the cap stays."""
        monkeypatch.setattr(queue_module, "JOB_STREAM_MAXLEN", 10)
        keys = [f"proxy-{n}" for n in range(250)]

        await jobs.publish(KIND, keys)

        kept = await queued_job_keys(redis_client, KIND)
        assert 10 < len(kept) < len(keys) and kept == keys[-len(kept) :]

    async def test_publish_raises_when_redis_does_not_take_a_pipeline(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A producer sends that second again: what landed before stands, and a key published twice is run twice at most."""
        keys = [f"proxy-{n}" for n in range(2 * PUBLISH_BATCH + 1)]
        pipelines = Pipelines(redis_client, monkeypatch, refuse=2)

        with pytest.raises(redis.exceptions.ConnectionError):
            await jobs.publish(KIND, keys)

        assert pipelines.executed == 2
        assert await queued_job_keys(redis_client, KIND) == keys[:PUBLISH_BATCH]

    async def test_publish_raises_when_redis_refuses_an_entry(self, jobs: JobQueue, redis_client: RedisClient) -> None:
        await redis_client.client.set(JOB_STREAM_KEY.format(kind=KIND), "not a stream")

        with pytest.raises(redis.exceptions.ResponseError):
            await jobs.publish(KIND, ["proxy-a"])

    async def test_request_now_asks_for_each_key_once_in_order(self, jobs: JobQueue, redis_client: RedisClient) -> None:
        assert await jobs.request_now(KIND, ["b", "a", "b"]) == 2

        assert await queued_job_keys(redis_client, KIND) == ["b", "a"]

    async def test_request_now_asks_for_a_batch_of_keys_per_pipeline(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that adds thousands of rows pays an exchange per batch, not per key."""
        keys = [f"proxy-{n}" for n in range(2 * PUBLISH_BATCH + 1)]
        pipelines = Pipelines(redis_client, monkeypatch)

        assert await jobs.request_now(KIND, keys) == len(keys)

        assert pipelines.executed == 3
        assert await queued_job_keys(redis_client, KIND) == keys

    async def test_the_first_batch_that_fails_ends_the_request_and_is_logged(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The batches after it are not tried: each would wait out its deadline on a Redis in trouble."""
        keys = [f"proxy-{n}" for n in range(2 * PUBLISH_BATCH + 1)]
        pipelines = Pipelines(redis_client, monkeypatch, refuse=2)

        with structlog.testing.capture_logs() as logs:
            assert await jobs.request_now(KIND, keys) == PUBLISH_BATCH

        assert pipelines.executed == 2
        assert [(log["kind"], log["key"], log["not_requested"], log["error"]) for log in failed_requests(logs)] == [
            ("sync", keys[PUBLISH_BATCH], PUBLISH_BATCH + 1, "Redis went away")
        ]
        assert await queued_job_keys(redis_client, KIND) == keys[:PUBLISH_BATCH]

    async def test_a_batch_redis_does_not_answer_is_given_up_at_the_deadline(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Not at the 30 s reply bound, so the write it follows returns within the deadline."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        try:
            await client.client.ping()
            await redis_client.client.client_pause(800, all=True)
            started = time.monotonic()
            with structlog.testing.capture_logs() as logs:
                asked = await JobQueue(client).request_now(KIND, ["a", "b"])
            assert time.monotonic() - started < 0.6
        finally:
            await client.close()

        assert asked == 0
        assert [(log["key"], log["not_requested"], log["error"]) for log in failed_requests(logs)] == [
            ("a", 2, "TimeoutError")
        ]

    async def test_nothing_to_ask_for_sends_nothing(
        self, jobs: JobQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipelines = Pipelines(redis_client, monkeypatch)

        assert await jobs.request_now(KIND, []) == 0

        assert pipelines.executed == 0

    async def test_an_entry_appended_to_a_transaction_lands_with_its_other_writes_and_is_capped(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that asks for a run in its own MULTI: the entry waits for the pipeline, and trims as a published one does."""
        monkeypatch.setattr(queue_module, "JOB_STREAM_MAXLEN", 10)
        for n in range(250):
            pipe = redis_client.client.pipeline(transaction=True)
            pipe.set("o6:test:written", str(n))
            JobQueue.append(pipe, KIND, f"connector-{n}")
            if n == 0:
                assert await queued_job_keys(redis_client, KIND) == []
            await pipe.execute()

        kept = await queued_job_keys(redis_client, KIND)
        assert 10 < len(kept) < 250 and kept == [f"connector-{n}" for n in range(250 - len(kept), 250)]
        assert await redis_client.client.get("o6:test:written") == "249"
