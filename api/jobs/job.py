# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Jobs: one class per kind of work a worker takes from its kind's job stream, a batch of entries at a time.

Entries come at least once, so every job is safe to run again on the same keys.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from api.core.roles import JobKind
    from api.db.streams import StreamEntry


@dataclass(frozen=True)
class JobBatch:
    """The entries of one job stream read together: every entry's ID, and the keys they name."""

    # Every entry's ID, in stream order.
    ids: tuple[str, ...]
    # The keys the entries name, each once, in stream order; an entry with no key adds none.
    keys: tuple[str, ...]

    @classmethod
    def of(cls, entries: Sequence[StreamEntry]) -> JobBatch:
        """The batch of ``entries``: every ID in order, and each key once, in order, empty ones skipped."""
        return cls(
            ids=tuple(entry.id for entry in entries),
            keys=tuple(dict.fromkeys(entry.key for entry in entries if entry.key)),
        )


class Job(ABC):
    """One kind of work: what a worker does with a batch of its kind's entries, and with a batch delivered too often.

    Raising from either leaves the batch's entries pending, delivered again after the claim timeout.
    """

    # The kind whose job stream the job takes.
    kind: ClassVar[JobKind]
    # The most entries one run takes, and how many runs one worker process
    # holds at once; their product is a worker's capacity for the kind. A job
    # may set either per instance, as one sized by its settings does.
    batch_size: int = 1
    concurrency: int = 1

    @abstractmethod
    async def run(self, batch: JobBatch) -> None:
        """Do the work the batch's keys ask for."""

    async def give_up(self, batch: JobBatch) -> None:
        """Settle a batch delivered too often to run again, before the runner completes it; by default nothing."""
        return None
