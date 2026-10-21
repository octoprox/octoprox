# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the job base (api.jobs.job): what a batch of job entries holds, and what every job has unless it says otherwise."""

import dataclasses

import pytest

from api.core.roles import JobKind
from api.db.streams import StreamEntry
from api.jobs.job import Job, JobBatch


class TestJobBatch:
    def test_every_id_in_order_and_each_key_once_in_order_without_empty_ones(self) -> None:
        """Duplicates are expected on a job stream: a key read twice in one batch is run once, and every entry completed."""
        entries = [
            StreamEntry("1-0", {"key": "proxy-b"}),
            StreamEntry("2-0", {"key": "proxy-a"}),
            StreamEntry("3-0", {"key": "proxy-b"}),
            # Deleted while pending, or appended with an empty key: nothing to run, an ID to complete all the same.
            StreamEntry("4-0", {}),
            StreamEntry("5-0", {"key": ""}),
            StreamEntry("6-0", {"key": "all"}),
        ]

        batch = JobBatch.of(entries)

        assert batch.ids == ("1-0", "2-0", "3-0", "4-0", "5-0", "6-0")
        assert batch.keys == ("proxy-b", "proxy-a", "all")

    def test_a_batch_cannot_be_changed_once_made(self) -> None:
        batch = JobBatch.of([StreamEntry("1-0", {"key": "proxy-a"})])

        with pytest.raises(dataclasses.FrozenInstanceError):
            batch.keys = ()  # type: ignore[misc]


class Probe(Job):
    """A job that notes each batch it runs."""

    kind = JobKind.HEALTH

    def __init__(self) -> None:
        self.ran: list[JobBatch] = []

    async def run(self, batch: JobBatch) -> None:
        self.ran.append(batch)


class SizedProbe(Probe):
    """A job sized by its settings, as health checks are by their probe count."""

    def __init__(self, probes: int) -> None:
        super().__init__()
        self.batch_size = 10
        self.concurrency = max(2, 4 * probes // 10)


class TestJob:
    async def test_by_default_one_entry_a_run_one_run_at_a_time_and_nothing_to_settle_when_given_up(self) -> None:
        job = Probe()

        assert (job.batch_size, job.concurrency) == (1, 1)
        assert await job.give_up(JobBatch.of([StreamEntry("1-0", {"key": "proxy-a"})])) is None
        assert job.ran == []

    def test_a_job_sized_by_its_settings_sets_its_own_size_and_leaves_its_class_s(self) -> None:
        few, many = SizedProbe(probes=5), SizedProbe(probes=50)

        assert (few.batch_size, few.concurrency) == (10, 2)
        assert (many.batch_size, many.concurrency) == (10, 20)
        assert (SizedProbe.batch_size, SizedProbe.concurrency) == (1, 1)

    def test_a_job_must_say_how_it_runs(self) -> None:
        class Unfinished(Job):
            kind = JobKind.SYNC

        with pytest.raises(TypeError):
            Unfinished()  # type: ignore[abstract]
