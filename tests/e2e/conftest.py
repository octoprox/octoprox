# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""End-to-end fixtures: the world outside Octoprox, the cluster of processes a test runs, and shortened claim timings.

The ``e2e`` marker selects these tests; once a test's cluster has stopped, ``db_session`` truncates the tables and
``redis_client`` flushes Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

import pytest

from api.core import consumers
from tests.e2e.fakes import FakeTarget, FakeUpstream
from tests.e2e.harness import SHORT_CLAIMS, ClaimTimings, Cluster, cluster_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient

# The vendor credentials the fake vendor demands, and every test's proxy row carries.
VENDOR_USERNAME = "vendor"
VENDOR_PASSWORD = "vendor-secret"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``e2e`` marker, which every module here carries."""
    config.addinivalue_line(
        "markers", "e2e: whole Octoprox role processes on one event loop against Postgres and Redis"
    )


@pytest.fixture
async def target() -> AsyncIterator[FakeTarget]:
    """The site clients fetch pages from, and the cluster's echo endpoint."""
    site = FakeTarget()
    await site.start()
    yield site
    await site.stop()


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    """The vendor's forwarding proxy endpoint a test's proxy row points at."""
    vendor = FakeUpstream(VENDOR_USERNAME, VENDOR_PASSWORD)
    await vendor.start()
    yield vendor
    await vendor.stop()


@pytest.fixture
async def cluster(
    build_app: Callable[..., FastAPI],
    auth_headers: dict[str, str],
    target: FakeTarget,
    upstream: FakeUpstream,
    db_session: AsyncSession,
    redis_client: RedisClient,
) -> AsyncIterator[Cluster]:
    """The test's processes, stopped when it ends, before anything they talk to.

    It asks for both fakes so they outlive every process (fixtures are torn down in reverse), and for ``db_session``
    and ``redis_client`` for the cleanup they do once the processes have stopped.
    """
    processes = Cluster(build_app, settings=cluster_settings(target), headers=auth_headers)
    yield processes
    # What the vendor still holds, from a test that ended before releasing it, is answered first, so that no worker's
    # stop waits out its grace for it.
    upstream.release()
    await processes.close()


@pytest.fixture
def short_claims(monkeypatch: pytest.MonkeyPatch) -> ClaimTimings:
    """The group consumers' claim timeout, reclaim check and refresh shortened for the test (``SHORT_CLAIMS``).

    They are constants every consumer reads as it uses them, so the patch reaches the processes the test starts.
    """
    monkeypatch.setattr(consumers, "CLAIM_TIMEOUT_SECONDS", SHORT_CLAIMS.timeout)
    monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", SHORT_CLAIMS.check)
    monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", SHORT_CLAIMS.refresh)
    return SHORT_CLAIMS
