# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Health checks: a worker probes each proxy through itself, keeps its soft health in Redis and writes its status to Postgres.

A proxy is degraded at its first failed check and unhealthy at the third in a row, and only healthy proxies route.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
import structlog

from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.redis import PROXY_HEALTH_KEY, PROXY_HEALTH_TTL_SECONDS
from api.db.repository import ProxyRepository
from api.feed.sections import resolve_credentials
from api.feed.state import HealthSoft, RuntimeStateReader
from api.geo.extraction import DEFAULT_HEALTHCHECK_URL
from api.geo.models import ObservationSource
from api.jobs.attribution import Sighting
from api.jobs.job import Job
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.sources import (
    default_proxied_client_factory,
    extract_ip_and_place,
    shared_ssl_context,
)
from api.providers.sdk.strategies import is_dynamic_gateway
from api.services.proxies import StatusMove

if TYPE_CHECKING:
    from collections.abc import Sequence

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.geo.extraction import EchoExtractionRules
    from api.jobs.attribution import Attribution
    from api.jobs.job import JobBatch
    from api.models.connector import Connector
    from api.models.credential import Credential
    from api.models.location import LocationTarget
    from api.services.proxies import ProxyService

logger = structlog.get_logger()

# How long a new cloud instance may fail its checks and stay initializing:
# booting and installing the proxy takes a few minutes, and it must not be
# reported unhealthy (or replaced) for that.
INITIALIZATION_GRACE_PERIOD = timedelta(minutes=5)
# Failed checks in a row that make a proxy unhealthy; one makes it degraded.
UNHEALTHY_FAILURES = 3
# Proxies one batch loads and checks together. Few, because a proxy that
# hangs for the whole check timeout holds its batch-mates with it; a batch
# still costs one load and one attribution pass, a tenth of one per key.
HEALTH_BATCH_SIZE = 10
# Keys a worker holds per probe slot, so the slots that fast checks free are
# refilled while some batches wait on a hung proxy: with the default 50
# probes, 20 batches of 10 at once. With two batches as large as the probe
# bound, a model of the runner gave about 3 checks a second per worker once
# 5% of proxies hung; this gives about 14, against 16 with no slot idle.
HEALTH_KEYS_PER_PROBE = 4
# How long one sign of whether the echo endpoint answers holds, in seconds. A
# check through a proxy that got the echo's answer within this long shows it
# up; without one, a batch whose checks of it failed requests the echo
# directly, and the batches of the next ten seconds go by that answer. So a
# worker requests the echo itself at most once per this long while it is
# down, and hardly ever while proxies reach it. It also bounds what an echo
# outage costs: only the checks judged within this long of its last answer
# count against their proxies.
ECHO_CHECK_SECONDS = 10.0
# Where a proxy whose soft health was lost resumes counting: as many failures
# as its stored status needs, so the next failure keeps it where it is.
_FAILURES_AT_LEAST = {ProxyStatus.DEGRADED: 1, ProxyStatus.UNHEALTHY: UNHEALTHY_FAILURES - 1}


class EchoWatch:
    """Whether the echo endpoint answers, so a check that failed because it did not is not held against the proxy.

    A recent answer through any proxy shows it up; otherwise it is asked directly, at most once per ``ECHO_CHECK_SECONDS``.

    Args:
        timeout: seconds the direct request may take (``health_check_timeout``).
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        # One direct request at a time: the batches that need one meanwhile wait for its answer.
        self._lock = asyncio.Lock()
        # The echo URL and when (time.monotonic()) it last answered, through a proxy or directly.
        self._answered: tuple[str, float] | None = None
        # The echo URL and when a direct request of it last went unanswered.
        self._unanswered: tuple[str, float] | None = None
        # Whether the echo is taken to be down, so an outage is logged at its start and its end only.
        self._down = False

    def answered(self, url: str) -> None:
        """A check got the echo's answer at ``url``: it is up."""
        self._answered = (url, time.monotonic())
        if self._down:
            self._down = False
            logger.info("Echo endpoint answers again; failed health checks count again", host=_host(url))

    async def is_down(self, url: str) -> bool:
        """Whether the echo at ``url`` does not answer: from a sign younger than ``ECHO_CHECK_SECONDS``, else asked."""
        if self._recent(self._answered, url):
            return False
        async with self._lock:
            # Another batch may have asked while this one waited.
            if self._recent(self._answered, url):
                return False
            if self._recent(self._unanswered, url):
                return True
            reason = await self._request(url)
            if reason is None:
                self.answered(url)
                return False
            self._unanswered = (url, time.monotonic())
            if not self._down:
                self._down = True
                logger.warning(
                    "Echo endpoint does not answer; failed health checks against it are not counted",
                    host=_host(url),
                    reason=reason,
                )
            return True

    async def _request(self, url: str) -> str | None:
        """Request ``url`` directly; None when it answered as a check must (a 200), else why it did not."""
        try:
            async with httpx.AsyncClient(verify=shared_ssl_context(), timeout=self._timeout) as client:
                response = await client.get(url)
        except httpx.TimeoutException:
            return "Timeout"
        except Exception as exc:
            return f"Error: {error_text(exc)}"
        if response.status_code != 200:
            return f"HTTP {response.status_code}"
        return None

    @staticmethod
    def _recent(sign: tuple[str, float] | None, url: str) -> bool:
        """Whether ``sign`` is about ``url`` and younger than ``ECHO_CHECK_SECONDS``."""
        return sign is not None and sign[0] == url and time.monotonic() - sign[1] < ECHO_CHECK_SECONDS


def _host(url: str) -> str | None:
    """The host ``url`` names: all a log line shows of the echo URL, whose query may carry a token."""
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


@dataclass(frozen=True)
class _Target:
    """A proxy of the batch as stored, with what its check needs."""

    proxy: Proxy
    connector: Connector
    credential: Credential


@dataclass(frozen=True)
class _Probe:
    """What requesting the check URL through a proxy gave."""

    # The URL requested: the connector's own, or the echo endpoint.
    url: str
    ok: bool
    latency_ms: float = 0.0
    # Why it failed, for the log.
    reason: str | None = None
    # The exit the response named, and its place, when it named one.
    exit_ip: str | None = None
    place: LocationTarget | None = None
    # When the response arrived (naive UTC): the sighting's time, which orders
    # it against the sightings of the same exit gateways append.
    seen_at: datetime | None = None


class HealthCheckJob(Job):
    """Checks a batch of proxies through themselves and writes what the checks found: the ``health`` kind's job.

    Args:
        timeout: seconds a check may take (``health_check_timeout``).
        probes: checks one worker runs at once (``health_check_concurrency``), which sizes its concurrency.
    """

    kind = JobKind.HEALTH
    batch_size = HEALTH_BATCH_SIZE

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        proxies: ProxyService,
        attribution: Attribution,
        rules: EchoExtractionRules,
        *,
        timeout: float,
        probes: int,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._state = RuntimeStateReader(redis_client)
        self._proxies = proxies
        self._attribution = attribution
        self._rules = rules
        self._timeout = timeout
        # Batches at once: enough keys in hand to keep every probe slot busy.
        self.concurrency = max(2, HEALTH_KEYS_PER_PROBE * probes // HEALTH_BATCH_SIZE)
        # Shared by every batch of this worker: the per-worker bound on checks in flight.
        self._slots = asyncio.Semaphore(probes)
        self._echo = EchoWatch(timeout)

    @staticmethod
    def wants_check(proxy: Proxy, connector: Connector) -> bool:
        """Whether a proxy is one to check: of an enabled connector, not on its way out, and not a dynamic-sessions gateway.

        Such a gateway mints a vendor session per request, so no single exit speaks for it and a check would only cost
        vendor traffic.
        """
        return connector.enabled and not proxy.status.retiring and not is_dynamic_gateway(proxy)

    @staticmethod
    def in_grace_period(proxy: Proxy, now: datetime) -> bool:
        """Whether ``proxy`` is a new instance still initializing, so its failed checks do not count yet."""
        return proxy.status == ProxyStatus.INITIALIZING and now - proxy.created_at < INITIALIZATION_GRACE_PERIOD

    @staticmethod
    def judge_check(proxy: Proxy, previous_failures: int, ok: bool, now: datetime) -> tuple[ProxyStatus, int]:
        """The status and the failures in a row after one check of ``proxy``.

        A success makes it healthy; a failure counts and degrades it, or makes it unhealthy from the third in a row,
        while an initializing instance in its grace period stays initializing.
        """
        if ok:
            return ProxyStatus.HEALTHY, 0
        failures = previous_failures + 1
        if HealthCheckJob.in_grace_period(proxy, now):
            return ProxyStatus.INITIALIZING, failures
        if failures >= UNHEALTHY_FAILURES:
            return ProxyStatus.UNHEALTHY, failures
        return ProxyStatus.DEGRADED, failures

    async def run(self, batch: JobBatch) -> None:
        """Check the batch's proxies that still want a check, then write their soft health, statuses and exits."""
        loaded = (await self._load(batch.keys)).values()
        targets = [target for target in loaded if self.wants_check(target.proxy, target.connector)]
        soft = await self._state.health(target.proxy.id for target in targets)
        probes = await asyncio.gather(*(self._probe(target) for target in targets))
        echo_url = self.echo_url
        echo_failed = any(not probe.ok and probe.url == echo_url for probe in probes)
        echo_down = echo_failed and await self._echo.is_down(echo_url)
        now = utc_now()
        results: dict[str, tuple[ProxyStatus, HealthSoft]] = {}
        sightings: list[Sighting] = []
        uncounted = 0
        for target, probe in zip(targets, probes, strict=True):
            proxy = target.proxy
            if echo_down and not probe.ok and probe.url == echo_url:
                # Says nothing about the proxy: its status and failures stay as loaded.
                uncounted += 1
                continue
            status, failures = self.judge_check(proxy, self._previous_failures(proxy, soft.get(proxy.id)), probe.ok, now)
            results[proxy.id] = (status, HealthSoft(failures=failures, latency_ms=probe.latency_ms, checked_at=now))
            if probe.exit_ip:
                sightings.append(Sighting(
                    proxy.id,
                    probe.exit_ip,
                    ObservationSource.HEALTH_CHECK,
                    endpoint=probe.place,
                    observed_at=probe.seen_at,
                ))
            if not probe.ok:
                logger.debug("Proxy health check failed", proxy_id=proxy.id, reason=probe.reason, failures=failures)
        if uncounted:
            logger.debug("Failed health checks not counted: the echo endpoint does not answer", count=uncounted)
        await self._write_soft_health({proxy_id: soft_health for proxy_id, (_, soft_health) in results.items()})
        await self._write_statuses([target.proxy for target in targets if target.proxy.id in results], results)
        await self._attribution.observe(sightings)

    # --- the batch ---------------------------------------------------------------------

    @staticmethod
    def _previous_failures(proxy: Proxy, soft: HealthSoft | None) -> int:
        """The failed checks in a row before this one: the soft health's count, else what the stored status implies."""
        if soft is not None:
            return soft.failures
        return _FAILURES_AT_LEAST.get(proxy.status, 0)

    async def _load(self, keys: Sequence[str]) -> dict[str, _Target]:
        """The batch's proxies that still exist, each with its connector and credential, in one query."""
        async with self._session_factory() as session:
            rows = await ProxyRepository(session).get_many_with_connectors(list(keys))
        return {
            proxy.id: _Target(proxy=proxy, connector=connector, credential=credential)
            for proxy, connector, credential in rows
        }

    async def _write_soft_health(self, results: dict[str, HealthSoft]) -> None:
        """Every checked proxy's soft health, with a day's expiry, in one round trip."""
        if not results:
            return
        pipe = self._redis.client.pipeline(transaction=False)
        for proxy_id, soft in results.items():
            key = PROXY_HEALTH_KEY.format(proxy_id=proxy_id)
            # redis-py types the mapping's keys as a union, and a mapping is invariant in its key type.
            pipe.hset(key, mapping=soft.model_dump(mode="json"))  # type: ignore[arg-type]
            pipe.expire(key, PROXY_HEALTH_TTL_SECONDS)
        await pipe.execute()

    async def _write_statuses(
        self, proxies: list[Proxy], results: dict[str, tuple[ProxyStatus, HealthSoft]]
    ) -> None:
        """Move each stored status the checks changed, from the one loaded, in one transaction announced by one change entry.

        A proxy something else moved meanwhile is left as it is.
        """
        moves = {
            proxy.id: StatusMove(new=status, expected=proxy.status)
            for proxy in proxies
            if (status := results[proxy.id][0]) != proxy.status
        }
        moved = await self._proxies.set_statuses_if(moves)
        for proxy in proxies:
            move = moves.get(proxy.id)
            if move is None:
                continue
            previous = proxy.status.value
            if proxy.id not in moved:
                logger.debug("Proxy status moved during its check", proxy_id=proxy.id, status=move.new.value)
            elif move.new == ProxyStatus.UNHEALTHY:
                failures = results[proxy.id][1].failures
                logger.warning("Proxy marked unhealthy", proxy_id=proxy.id, previous=previous, failures=failures)
            else:
                logger.info("Proxy status changed", proxy_id=proxy.id, previous=previous, status=move.new.value)

    # --- one check ---------------------------------------------------------------------

    @property
    def echo_url(self) -> str:
        """What a connector without a check URL of its own checks: the echo endpoint, else httpbin."""
        return self._rules.default_check_url or DEFAULT_HEALTHCHECK_URL

    def check_url(self, connector: Connector) -> str:
        """What a check requests: the connector's own ``healthcheck_url``, else the echo endpoint, else httpbin.

        A connector may name its own URL because its proxies are restricted
        to certain destinations.
        """
        custom = connector.config.get("healthcheck_url") if connector.config else None
        if isinstance(custom, str) and custom:
            return custom
        return self.echo_url

    def exit_of(
        self, proxy_id: str, connector: Connector, url: str, response: httpx.Response
    ) -> tuple[str | None, LocationTarget | None]:
        """The exit IP a check response names, and its place; ``(None, None)`` when the URL names none."""
        paths = self._rules.ip_paths(url, connector.config or {})
        if paths is None:
            return None, None
        try:
            ip, place = extract_ip_and_place(response, paths.ip, paths.country, paths.state, paths.city)
        except Exception as exc:
            logger.debug("Health check IP extraction failed", proxy_id=proxy_id, error=error_text(exc))
            return None, None
        if ip is None:
            logger.debug(
                "Health check response carried no IP at the configured path",
                proxy_id=proxy_id,
                url=url,
                ip_path=paths.ip,
            )
        return ip, place

    async def _probe(self, target: _Target) -> _Probe:
        """Request the check URL through the proxy, its credentials filled in, once a slot is free.

        A check of the echo endpoint that gets its answer tells the echo
        watch that the echo is up.
        """
        url = self.check_url(target.connector)
        proxy = resolve_credentials(target.proxy, target.credential, target.connector)
        async with self._slots:
            started = time.monotonic()
            try:
                async with default_proxied_client_factory(proxy.url, self._timeout) as client:
                    response = await client.get(url)
            except httpx.TimeoutException:
                return _Probe(url, ok=False, reason="Timeout")
            except httpx.ProxyError as exc:
                return _Probe(url, ok=False, reason=f"Proxy error: {exc}")
            except Exception as exc:
                return _Probe(url, ok=False, reason=f"Error: {exc}")
            latency_ms = (time.monotonic() - started) * 1000
        if response.status_code != 200:
            return _Probe(url, ok=False, reason=f"HTTP {response.status_code}")
        if url == self.echo_url:
            self._echo.answered(url)
        ip, place = self.exit_of(proxy.id, target.connector, url, response)
        return _Probe(url, ok=True, latency_ms=latency_ms, exit_ip=ip, place=place, seen_at=utc_now())
