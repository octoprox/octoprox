# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Job runners: a worker process runs one per job kind it takes, reading the kind's job stream through the workers' group.

Each batch the group hands it goes to the kind's job; the base consumer completes the batch once the job returns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.core.consumers import GroupConsumer
from api.core.workers import job_runner_name
from api.db.redis import JOB_STREAM_KEY
from api.jobs.job import JobBatch

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.streams import StreamEntry
    from api.jobs.job import Job

logger = structlog.get_logger()

# Keys a log line names at most: a health batch names a sample, a one-key kind its key.
_LOGGED_KEYS = 5


class JobRunner(GroupConsumer):
    """Runs one kind's job on the batches its job stream hands this worker: a batch a read, the job's concurrency at once.

    Its room is the job's free batches times its batch size, and each batch is one run of the kind in the job stats.
    """

    def __init__(self, redis_client: RedisClient, job: Job) -> None:
        super().__init__(
            redis_client,
            JOB_STREAM_KEY.format(kind=job.kind),
            name=job_runner_name(job.kind),
            count=job.batch_size,
            concurrency=job.concurrency,
        )
        self._job = job

    async def _handle(self, entries: list[StreamEntry]) -> None:
        """Run the job on the batch's keys, each once; an entry without a key is logged and completed with the rest."""
        keyless = [entry.id for entry in entries if not entry.key]
        if keyless:
            logger.warning(
                "Job entries without a key dropped", kind=self._job.kind.value, ids=keyless[:_LOGGED_KEYS]
            )
        batch = JobBatch.of(entries)
        if batch.keys:
            await self._job.run(batch)

    async def _give_up(self, entries: list[StreamEntry]) -> None:
        """Log the keys of a batch delivered too often to run again, then hand it to the job's give-up."""
        batch = JobBatch.of(entries)
        logger.warning(
            "Job given up after repeated deliveries", kind=self._job.kind.value, keys=list(batch.keys[:_LOGGED_KEYS])
        )
        await self._job.give_up(batch)
