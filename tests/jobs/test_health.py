# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the health job: the status machine, the batch's choices, real probes and the writes they make.

Probes go through :class:`~tests.jobs.conftest.FakeUpstream`, a local proxy that answers the check itself, and the echo
endpoint is another one, which a worker requests directly only when it must know whether the echo is up.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.core.roles import JobKind
from api.db.redis import PROXY_HEALTH_KEY, PROXY_HEALTH_TTL_SECONDS, RedisClient
from api.feed.state import RuntimeStateReader
from api.geo.extraction import DEFAULT_HEALTHCHECK_URL, EchoExtractionRules, ExtractionPaths
from api.geo.models import IpObservation, ObservationSource
from api.geo.observations import ObservationRecorder
from api.geo.runtime import GeoStack
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.jobs import health as health_module
from api.jobs.attribution import Attribution
from api.jobs.health import INITIALIZATION_GRACE_PERIOD, EchoWatch, HealthCheckJob
from api.jobs.queue import JobQueue
from api.jobs.runner import JobRunner
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.strategies import META_DISCOVERED_IP, META_DYNAMIC_SESSIONS
from api.services.proxies import ProxyService
from tests.jobs.conftest import (
    LONDON_IP,
    FakeUpstream,
    Rows,
    announced,
    batch_of,
    counted_round_trips,
    counted_selects,
    geo_settings,
    recorded,
)
from tests.waits import running, until

# What the worker logs when the echo endpoint goes down, once per outage.
ECHO_DOWN = "Echo endpoint does not answer; failed health checks against it are not counted"
# What it logs for each batch some of whose failed checks it did not count, with how many.
UNCOUNTED = "Failed health checks not counted: the echo endpoint does not answer"


def uncounted(logs: list[dict[str, Any]]) -> list[int]:
    """How many failed checks each batch did not count, as the worker logged them."""
    return [entry["count"] for entry in logs if entry["event"] == UNCOUNTED]


def _proxy(status: ProxyStatus, age: timedelta = timedelta(hours=1)) -> Proxy:
    return Proxy(host="1.2.3.4", port=3128, connector_id="c", status=status, created_at=utc_now() - age)


def _idle_job(*, rules: EchoExtractionRules | None = None, probes: int = 1) -> HealthCheckJob:
    """A health job whose stores and services are stand-ins of their own types, for a test that runs no check."""
    return HealthCheckJob(
        MagicMock(spec=async_sessionmaker),
        MagicMock(spec=RedisClient),
        MagicMock(spec=ProxyService),
        MagicMock(spec=Attribution),
        rules if rules is not None else MagicMock(spec=EchoExtractionRules),
        timeout=1.0,
        probes=probes,
    )


class TestStatusMachine:
    """What one check makes of a proxy: the status and the failures in a row."""

    def test_the_grace_period_is_five_minutes(self) -> None:
        assert timedelta(minutes=5) == INITIALIZATION_GRACE_PERIOD

    def test_only_a_new_initializing_instance_is_in_its_grace_period(self) -> None:
        now = utc_now()
        assert HealthCheckJob.in_grace_period(_proxy(ProxyStatus.INITIALIZING, timedelta(seconds=10)), now)
        assert not HealthCheckJob.in_grace_period(_proxy(ProxyStatus.INITIALIZING, timedelta(minutes=6)), now)
        assert not HealthCheckJob.in_grace_period(_proxy(ProxyStatus.HEALTHY, timedelta(seconds=10)), now)

    def test_a_failure_in_the_grace_period_keeps_it_initializing_and_counts(self) -> None:
        new = _proxy(ProxyStatus.INITIALIZING, timedelta(seconds=10))
        assert HealthCheckJob.judge_check(new, 0, False, utc_now()) == (ProxyStatus.INITIALIZING, 1)
        assert HealthCheckJob.judge_check(new, 5, False, utc_now()) == (ProxyStatus.INITIALIZING, 6)

    def test_after_the_grace_period_a_failure_degrades_and_the_third_makes_it_unhealthy(self) -> None:
        old = _proxy(ProxyStatus.INITIALIZING, timedelta(minutes=6))
        assert HealthCheckJob.judge_check(old, 0, False, utc_now()) == (ProxyStatus.DEGRADED, 1)
        assert HealthCheckJob.judge_check(old, 1, False, utc_now()) == (ProxyStatus.DEGRADED, 2)
        assert HealthCheckJob.judge_check(old, 2, False, utc_now()) == (ProxyStatus.UNHEALTHY, 3)

    def test_a_success_makes_any_proxy_healthy_and_clears_the_count(self) -> None:
        for status in (ProxyStatus.UNKNOWN, ProxyStatus.INITIALIZING, ProxyStatus.UNHEALTHY):
            assert HealthCheckJob.judge_check(_proxy(status), 7, True, utc_now()) == (ProxyStatus.HEALTHY, 0)


class TestCheckUrlAndExit:
    """Which URL a check requests and where its response names the exit (the 5.x exit-IP tests)."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> GeoService:
        settings = Settings(
            instance_id="t",
            geo_lookup_url="https://echo.example/ip",
            geo_lookup_ip_path="ip",
            geo_lookup_country_path="country",
        )
        return GeoService(
            settings, GeoDatabaseStore(None, tmp_path), GeoSettingsStore(settings, None), ObservationRecorder(None)
        )

    @pytest.fixture
    def rules(self, service: GeoService) -> EchoExtractionRules:
        return EchoExtractionRules(service)

    @staticmethod
    def _job(rules: EchoExtractionRules) -> HealthCheckJob:
        return _idle_job(rules=rules)

    @staticmethod
    def _connector(config: dict[str, Any] | None = None) -> Connector:
        return Connector(
            id="conn", name="c", credential_id="cred", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p", config=config or {},
        )

    def test_the_echo_endpoint_is_checked_unless_the_connector_names_a_url(self, rules: EchoExtractionRules) -> None:
        job = self._job(rules)
        assert job.check_url(self._connector()) == "https://echo.example/ip"
        assert job.check_url(self._connector({"healthcheck_url": "https://www.bing.com"})) == "https://www.bing.com"

    def test_httpbin_when_nothing_names_a_url(self) -> None:
        rules = MagicMock(default_check_url="")
        assert self._job(rules).check_url(self._connector()) == DEFAULT_HEALTHCHECK_URL

    def test_httpbin_names_the_first_origin(self, rules: EchoExtractionRules) -> None:
        response = httpx.Response(200, json={"origin": "203.0.113.5, 10.0.0.1"})
        exit_seen = self._job(rules).exit_of("p", self._connector(), DEFAULT_HEALTHCHECK_URL, response)
        assert exit_seen == ("203.0.113.5", None)

    def test_the_echo_endpoint_names_ip_and_country(self, rules: EchoExtractionRules) -> None:
        response = httpx.Response(200, json={"ip": "203.0.113.5", "country": "de"})
        exit_seen = self._job(rules).exit_of("p", self._connector(), "https://echo.example/ip", response)
        assert exit_seen == ("203.0.113.5", LocationTarget(country="DE"))

    def test_a_custom_url_without_a_path_names_nothing(self, rules: EchoExtractionRules) -> None:
        connector = self._connector({"healthcheck_url": "https://www.bing.com"})
        response = httpx.Response(200, json={"ip": "203.0.113.5"})
        assert self._job(rules).exit_of("p", connector, "https://www.bing.com", response) == (None, None)

    def test_the_connectors_paths_win(self, rules: EchoExtractionRules) -> None:
        connector = self._connector({
            "healthcheck_url": "https://x/ip",
            "healthcheck_ip_path": "data.addr",
            "healthcheck_country_path": "data.cc",
        })
        assert rules.ip_paths("https://x/ip", connector.config) == ExtractionPaths("data.addr", "data.cc")
        response = httpx.Response(200, json={"data": {"addr": "203.0.113.5", "cc": "fr"}})
        exit_seen = self._job(rules).exit_of("p", connector, "https://x/ip", response)
        assert exit_seen == ("203.0.113.5", LocationTarget(country="FR"))

    def test_the_settings_can_turn_echo_attribution_off(self, service: GeoService, rules: EchoExtractionRules) -> None:
        service.settings_store.adopt(service.settings.model_copy(update={"health_check_attribution": False}), stored=True)
        response = httpx.Response(200, json={"ip": "203.0.113.5"})
        assert self._job(rules).exit_of("p", self._connector(), "https://echo.example/ip", response) == (None, None)

    def test_a_body_that_is_not_json_names_nothing(self, rules: EchoExtractionRules) -> None:
        response = httpx.Response(200, text="<html>ok</html>")
        assert self._job(rules).exit_of("p", self._connector(), DEFAULT_HEALTHCHECK_URL, response) == (None, None)

    def test_a_text_path_reads_the_body(self, rules: EchoExtractionRules) -> None:
        connector = self._connector({"healthcheck_url": "https://x/raw", "healthcheck_ip_path": "@text"})
        response = httpx.Response(200, text="203.0.113.5\n")
        assert self._job(rules).exit_of("p", connector, "https://x/raw", response) == ("203.0.113.5", None)


@pytest.fixture
async def echo() -> AsyncIterator[FakeUpstream]:
    """The echo endpoint as the worker requests it directly; it answers 200 until a test says otherwise."""
    server = FakeUpstream()
    await server.start()
    yield server
    await server.stop()


def _echo_url(echo: FakeUpstream) -> str:
    return f"http://127.0.0.1:{echo.port}/ip"


@pytest.fixture
async def geo(
    tmp_path: Path, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, echo: FakeUpstream
) -> AsyncIterator[GeoStack]:
    """The worker's attribution stack as the shared fixture builds it, with ``echo`` as the echo endpoint."""
    settings = geo_settings(tmp_path, geo_lookup_url=_echo_url(echo))
    stack = GeoStack.build(settings, session_factory=sessions, redis_client=redis_client, blob_source=None)
    await stack.geo_service.resync()
    yield stack
    stack.database_store.close_all()


@pytest.fixture
def job(
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    proxy_service: ProxyService,
    attribution: Attribution,
    geo: GeoStack,
) -> HealthCheckJob:
    return HealthCheckJob(sessions, redis_client, proxy_service, attribution, geo.extraction_rules, timeout=2.0, probes=10)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


@pytest.fixture
async def connector(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project)


async def _soft(redis_client: RedisClient, proxy_id: str) -> tuple[int, float] | None:
    soft = (await RuntimeStateReader(redis_client).health([proxy_id])).get(proxy_id)
    return (soft.failures, soft.latency_ms) if soft else None


class TestBatch:
    """Which proxies of a batch are checked: one that no longer qualifies is left alone."""

    async def test_proxies_that_no_longer_qualify_are_left_unchecked(
        self, job: HealthCheckJob, rows: Rows, project: Project, connector: Connector, upstream: FakeUpstream
    ) -> None:
        disabled = await rows.connector(project, enabled=False)
        skipped = [
            await rows.proxy(connector, port=upstream.port, status=ProxyStatus.DRAINING),
            await rows.proxy(connector, port=upstream.port, status=ProxyStatus.TERMINATING),
            await rows.proxy(connector, port=upstream.port, metadata={META_DYNAMIC_SESSIONS: "true"}),
            await rows.proxy(disabled, port=upstream.port),
        ]
        checked = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.INITIALIZING)

        await job.run(batch_of(*(p.id for p in skipped), "gone", checked.id))

        assert len(upstream.requests) == 1  # only the initializing proxy was probed
        stored = [await rows.get(p.id) for p in skipped]
        assert [p.status if p else None for p in stored] == [p.status for p in skipped]
        assert (await rows.get(checked.id)).status == ProxyStatus.HEALTHY  # type: ignore[union-attr]

    async def test_a_batch_of_proxies_that_are_gone_checks_and_writes_nothing(
        self, job: HealthCheckJob, upstream: FakeUpstream, redis_client: RedisClient
    ) -> None:
        await job.run(batch_of("gone"))

        assert upstream.requests == [] and await announced(redis_client) == []


class TestChecks:
    async def test_a_healthy_check_writes_the_soft_health_and_the_new_status(
        self,
        job: HealthCheckJob,
        rows: Rows,
        connector: Connector,
        upstream: FakeUpstream,
        echo: FakeUpstream,
        redis_client: RedisClient,
    ) -> None:
        upstream.answer({"ip": LONDON_IP})
        proxy = await rows.proxy(connector, port=upstream.port)

        await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.HEALTHY and stored.status_changed_at is not None
        soft = await _soft(redis_client, proxy.id)
        assert soft is not None and soft[0] == 0 and soft[1] > 0
        ttl = await redis_client.client.ttl(PROXY_HEALTH_KEY.format(proxy_id=proxy.id))
        assert PROXY_HEALTH_TTL_SECONDS - 10 < ttl <= PROXY_HEALTH_TTL_SECONDS
        assert upstream.requests[0][0] == f"GET {_echo_url(echo)} HTTP/1.1"
        # A check that passed needs no word from the echo itself.
        assert echo.requests == []

    async def test_the_check_goes_through_the_proxy_with_its_placeholders_filled(
        self, job: HealthCheckJob, rows: Rows, project: Project, upstream: FakeUpstream
    ) -> None:
        vendor = await rows.connector(project, "oxylabs", credential={"username": "alice", "password": "s3cret"})
        proxy = await rows.proxy(vendor, port=upstream.port, username="customer-{username}", password="{password}")

        await job.run(batch_of(proxy.id))

        expected = base64.b64encode(b"customer-alice:s3cret").decode()
        assert upstream.requests[0][1] == f"Basic {expected}"

    async def test_failures_degrade_then_make_it_unhealthy_writing_the_status_only_when_it_changes(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, redis_client: RedisClient
    ) -> None:
        upstream.answer({}, status=502)
        proxy = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)

        statuses = []
        entries = []
        for _ in range(4):
            await job.run(batch_of(proxy.id))
            stored = await rows.get(proxy.id)
            assert stored is not None
            statuses.append(stored.status)
            entries.append(len(await announced(redis_client)))

        assert statuses == [ProxyStatus.DEGRADED, ProxyStatus.DEGRADED, ProxyStatus.UNHEALTHY, ProxyStatus.UNHEALTHY]
        # Written on the two changes only: each write is announced, and the controller recompiles the feed.
        assert entries == [1, 1, 2, 2]
        assert await announced(redis_client) == [("proxy", proxy.id)] * 2
        assert await _soft(redis_client, proxy.id) == (4, 0.0)

    async def test_the_statuses_a_batch_changed_are_written_in_one_transaction(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, redis_client: RedisClient
    ) -> None:
        """A vendor outage flips a whole pool: one commit per batch, so one change entry, not one a proxy."""
        upstream.answer({}, status=502)
        proxies = [await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY) for _ in range(3)]

        await job.run(batch_of(*(p.id for p in proxies)))

        stored = [await rows.get(p.id) for p in proxies]
        assert [p.status if p else None for p in stored] == [ProxyStatus.DEGRADED] * 3
        assert await announced(redis_client) == [("proxy", "")]

    async def test_a_dead_or_slow_proxy_fails_its_check(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, proxy_service: ProxyService,
        attribution: Attribution, geo: GeoStack, rows: Rows, connector: Connector, upstream: FakeUpstream,
    ) -> None:
        job = HealthCheckJob(
            sessions, redis_client, proxy_service, attribution, geo.extraction_rules, timeout=0.3, probes=10
        )
        upstream.delay = 1.0
        slow = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)
        dead = await rows.proxy(connector, port=1, status=ProxyStatus.HEALTHY)

        await job.run(batch_of(slow.id, dead.id))

        stored = [await rows.get(p.id) for p in (slow, dead)]
        assert [p.status if p else None for p in stored] == [ProxyStatus.DEGRADED] * 2

    async def test_a_check_never_undoes_a_drain_that_landed_meanwhile(
        self,
        job: HealthCheckJob,
        rows: Rows,
        connector: Connector,
        upstream: FakeUpstream,
        proxy_service: ProxyService,
    ) -> None:
        upstream.delay = 0.3
        proxy = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.UNKNOWN)

        async def drain_during_the_check() -> None:
            await asyncio.sleep(0.1)
            await proxy_service.drain(proxy.id)

        await asyncio.gather(job.run(batch_of(proxy.id)), drain_during_the_check())

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.DRAINING

    async def test_a_lost_soft_health_resumes_from_the_status(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, redis_client: RedisClient
    ) -> None:
        """After a Redis wipe an unhealthy proxy that fails again stays unhealthy rather than looking degraded."""
        upstream.answer({}, status=500)
        proxy = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.UNHEALTHY)

        await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.UNHEALTHY
        assert await _soft(redis_client, proxy.id) == (3, 0.0)

    async def test_the_exit_a_check_saw_is_attributed(
        self,
        job: HealthCheckJob,
        rows: Rows,
        connector: Connector,
        upstream: FakeUpstream,
        geo: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        upstream.answer({"ip": LONDON_IP, "country": "GB"})
        proxy = await rows.proxy(connector, port=upstream.port)

        await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == LONDON_IP and stored.country == "GB"
        observations = [o for o in await recorded(geo, redis_client) if isinstance(o, IpObservation)]
        seen = [(o.proxy_id, o.ip, o.source) for o in observations]
        assert seen == [(proxy.id, LONDON_IP, ObservationSource.HEALTH_CHECK)]

        # The same exit again is a repeat: nothing is written or recorded.
        await job.run(batch_of(proxy.id))
        assert await recorded(geo, redis_client) == []

    async def test_a_check_url_that_names_no_exit_attributes_nothing(
        self,
        job: HealthCheckJob,
        rows: Rows,
        project: Project,
        upstream: FakeUpstream,
        geo: GeoStack,
        redis_client: RedisClient,
    ) -> None:
        upstream.answer({"ip": LONDON_IP})
        restricted = await rows.connector(project, config={"healthcheck_url": "http://www.example.test/"})
        proxy = await rows.proxy(restricted, port=upstream.port)

        await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.HEALTHY and META_DISCOVERED_IP not in stored.metadata
        assert upstream.requests[0][0] == "GET http://www.example.test/ HTTP/1.1"
        assert await recorded(geo, redis_client) == []


class TestEchoOutage:
    """A failed check of the echo endpoint counts against the proxy only while the echo itself answers."""

    async def test_an_echo_request_failing_with_no_text_is_logged_by_its_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def reset(_client: httpx.AsyncClient, _url: str, **_: Any) -> httpx.Response:
            raise ConnectionResetError()

        monkeypatch.setattr(httpx.AsyncClient, "get", reset)
        watch = EchoWatch(timeout=1.0)

        with structlog.testing.capture_logs() as logs:
            assert await watch.is_down("http://echo.test/ip") is True

        assert [entry["reason"] for entry in logs if entry["event"] == ECHO_DOWN] == ["Error: ConnectionResetError"]

    async def test_while_the_echo_is_down_failing_checks_move_no_proxy_and_count_again_once_it_answers(
        self,
        job: HealthCheckJob,
        rows: Rows,
        connector: Connector,
        upstream: FakeUpstream,
        echo: FakeUpstream,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Each batch asks the echo itself, as one does once every sign of it has aged.
        monkeypatch.setattr(health_module, "ECHO_CHECK_SECONDS", 0.0)
        upstream.answer({"ip": LONDON_IP})
        proxy = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)
        await job.run(batch_of(proxy.id))
        loaded = (await RuntimeStateReader(redis_client).health([proxy.id]))[proxy.id]
        entries = await announced(redis_client)

        echo.answer({}, status=503)
        upstream.answer({}, status=502)
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.HEALTHY
        assert (await RuntimeStateReader(redis_client).health([proxy.id]))[proxy.id] == loaded
        # Nothing written, so nothing announced.
        assert await announced(redis_client) == entries
        assert uncounted(logs) == [1, 1, 1] and len(echo.requests) == 3
        # One warning for the outage, not one per batch.
        assert [entry["event"] for entry in logs if entry["log_level"] == "warning"] == [ECHO_DOWN]

        echo.answer({})
        with structlog.testing.capture_logs() as logs:
            await job.run(batch_of(proxy.id))

        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.DEGRADED
        assert await _soft(redis_client, proxy.id) == (1, 0.0)
        assert "Echo endpoint answers again; failed health checks count again" in [entry["event"] for entry in logs]

    async def test_while_the_echo_answers_failed_checks_count_and_one_answer_serves_ten_seconds(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, echo: FakeUpstream
    ) -> None:
        upstream.answer({}, status=502)
        proxies = [await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY) for _ in range(2)]

        with structlog.testing.capture_logs() as logs:
            for proxy in proxies:
                await job.run(batch_of(proxy.id))

        stored = [await rows.get(proxy.id) for proxy in proxies]
        assert [proxy.status if proxy else None for proxy in stored] == [ProxyStatus.DEGRADED] * 2
        assert uncounted(logs) == []
        # Requested directly, once: the second batch went by the first one's answer.
        assert [request for request, _ in echo.requests] == ["GET /ip HTTP/1.1"]

    async def test_a_check_through_another_proxy_that_reached_the_echo_shows_it_up_without_asking_it(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, echo: FakeUpstream
    ) -> None:
        """So a worker that cannot reach the echo directly (behind a NAT that does not hairpin) still judges proxies."""
        echo.answer({}, status=503)
        upstream.answer({"ip": LONDON_IP})
        failing = FakeUpstream()
        await failing.start()
        failing.answer({}, status=502)
        try:
            good = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)
            bad = await rows.proxy(connector, port=failing.port, status=ProxyStatus.HEALTHY)
            with structlog.testing.capture_logs() as logs:
                await job.run(batch_of(good.id, bad.id))
        finally:
            await failing.stop()

        stored = await rows.get(bad.id)
        assert stored is not None and stored.status == ProxyStatus.DEGRADED
        assert echo.requests == [] and uncounted(logs) == []

    async def test_a_connectors_own_check_url_is_judged_as_always_while_the_echo_is_down(
        self,
        job: HealthCheckJob,
        rows: Rows,
        project: Project,
        connector: Connector,
        upstream: FakeUpstream,
        echo: FakeUpstream,
    ) -> None:
        echo.answer({}, status=503)
        upstream.answer({}, status=502)
        own = await rows.connector(project, config={"healthcheck_url": "http://www.example.test/"})
        on_echo = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)
        on_own = await rows.proxy(own, port=upstream.port, status=ProxyStatus.HEALTHY)

        with structlog.testing.capture_logs() as logs:
            await job.run(batch_of(on_echo.id, on_own.id))

        stored = [await rows.get(proxy.id) for proxy in (on_echo, on_own)]
        assert [proxy.status if proxy else None for proxy in stored] == [ProxyStatus.HEALTHY, ProxyStatus.DEGRADED]
        assert uncounted(logs) == [1]


class TestBatchCost:
    async def test_ten_proxies_cost_the_statements_and_round_trips_two_do(
        self, job: HealthCheckJob, rows: Rows, connector: Connector, upstream: FakeUpstream, db_engine: AsyncEngine
    ) -> None:
        """One query loads a batch (SPEC 6.2), and nothing else is read or sent once per proxy."""
        upstream.answer({"ip": LONDON_IP, "country": "GB"})

        async def cost(count: int) -> tuple[int, int]:
            batch = [await rows.proxy(connector, port=upstream.port) for _ in range(count)]
            with counted_selects(db_engine) as selects, counted_round_trips() as trips:
                await job.run(batch_of(*(proxy.id for proxy in batch)))
            return len(selects), len(trips)

        # The first batch opens the clients' connections.
        await cost(1)
        assert await cost(2) == await cost(10)


class TestConcurrency:
    def test_a_worker_holds_four_keys_per_probe_slot_in_batches_of_ten_and_at_least_two_batches(self) -> None:
        def job(probes: int) -> HealthCheckJob:
            return _idle_job(probes=probes)

        assert (job(50).kind, job(50).batch_size, job(50).concurrency) == (JobKind.HEALTH, 10, 20)
        assert (job(1).concurrency, job(5).concurrency, job(7).concurrency) == (2, 2, 2)
        assert job(8).concurrency == 3

    async def test_probes_are_bounded_per_worker(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, proxy_service: ProxyService,
        attribution: Attribution, geo: GeoStack, rows: Rows, connector: Connector, upstream: FakeUpstream,
    ) -> None:
        job = HealthCheckJob(
            sessions, redis_client, proxy_service, attribution, geo.extraction_rules, timeout=5.0, probes=2
        )
        upstream.delay = 0.2
        proxies = [await rows.proxy(connector, port=upstream.port) for _ in range(4)]

        started = time.monotonic()
        await asyncio.gather(job.run(batch_of(*(p.id for p in proxies[:2]))), job.run(batch_of(*(p.id for p in proxies[2:]))))

        # Two batches, two probe slots between them: two rounds of 0.2 s.
        assert time.monotonic() - started >= 0.4

    async def test_while_hung_proxies_hold_their_batch_the_next_batch_checks_every_other_proxy(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, proxy_service: ProxyService,
        attribution: Attribution, geo: GeoStack, rows: Rows, connector: Connector, upstream: FakeUpstream,
        jobs: JobQueue,
    ) -> None:
        """A hung proxy holds its own batch, not the worker: the batch read after it keeps the free slots busy."""
        job = HealthCheckJob(
            sessions, redis_client, proxy_service, attribution, geo.extraction_rules, timeout=10.0, probes=4
        )
        hung = FakeUpstream()
        await hung.start()
        hung.delay = 2.0
        stuck = [await rows.proxy(connector, port=hung.port) for _ in range(2)]
        fast = [await rows.proxy(connector, port=upstream.port) for _ in range(18)]
        # Both hung proxies in the first batch of ten, the second batch all fast ones.
        await jobs.publish(JobKind.HEALTH, [proxy.id for proxy in [*stuck, *fast]])
        runner = JobRunner(redis_client, job)
        try:
            async with running(runner):
                await until(lambda: len(upstream.requests) == len(fast), timeout=1.0)
                probed = len(upstream.requests)
        finally:
            await hung.stop()

        # Within a second, while the hung proxies held two of the four probe slots and their batch for two.
        assert probed == len(fast)
        assert len(hung.requests) == len(stuck)


class TestDuplicates:
    async def test_a_proxy_published_twice_changes_its_status_once(
        self,
        job: HealthCheckJob,
        rows: Rows,
        connector: Connector,
        upstream: FakeUpstream,
        redis_client: RedisClient,
        jobs: JobQueue,
    ) -> None:
        """Duplicates are the norm (a producer and a run-now, a redelivery): a check judges from the status it loads."""
        upstream.answer({}, status=502)
        proxy = await rows.proxy(connector, port=upstream.port, status=ProxyStatus.HEALTHY)
        runner = JobRunner(redis_client, job)

        # Read together, the two entries are one key: one probe.
        await jobs.publish(JobKind.HEALTH, [proxy.id, proxy.id])
        async with running(runner):
            await until(lambda: runner.consumed == 2)
            assert len(upstream.requests) == 1
            # Read apart, the second probes again and finds the status it would set already there.
            await jobs.publish(JobKind.HEALTH, [proxy.id])
            await until(lambda: runner.consumed == 3)

        assert len(upstream.requests) == 2
        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.DEGRADED
        assert await announced(redis_client) == [("proxy", proxy.id)]
