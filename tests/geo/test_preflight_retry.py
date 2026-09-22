# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ExitVerifier: what preflight does with the selection, per mode and strategy."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.core.signals import exit_location_mismatch
from api.geo.models import PreflightMode
from api.geo.observations import ObservationRecorder
from api.geo.preflight import PreflightChecker
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.geo.verifier import ExitVerifier
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.sources import IpDiscoverer
from api.strategies import get_strategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
US_IP = "203.0.113.9"  # not in the test database; the echo endpoint's country decides


def use_echo_by_proxy(geo_service: GeoService, exits: dict[str, tuple[str, str]]) -> None:
    """Make the service's echo requests answer per proxy host: host -> (ip, country)."""

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        host = proxy_url.rsplit("@", 1)[-1].split(":")[0]
        ip, country = exits[host]
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ip": ip, "country": country})),
            timeout=timeout,
        )

    geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=factory)  # type: ignore[method-assign]


@pytest.fixture
async def checker(tmp_path: Path) -> PreflightChecker:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db)}])
    await store.sync_all()
    settings = Settings(instance_id="t", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]
    geo_service = GeoService(settings, store, GeoSettingsStore(settings, None), ObservationRecorder(None))
    use_echo_by_proxy(geo_service, {"gb": (GB_IP, "GB"), "us": (US_IP, "US"), "us2": (US_IP, "US")})
    return PreflightChecker(geo_service, None)


def _proxy(host: str) -> Proxy:
    return Proxy(id=host, host=host, port=8000, connector_id="conn", username="u", password="p", status=ProxyStatus.HEALTHY)


def _project(mode: PreflightMode) -> Project:
    return Project(id="proj", name="P", username="u", password="p", location_preflight=mode)


def _selector(strategy: str = "round_robin", picks: list[Proxy | None] | None = None) -> MagicMock:
    selector = MagicMock()
    selector.strategy_for_project = MagicMock(return_value=get_strategy(strategy))
    selector.select_proxy_for_project = AsyncMock(side_effect=picks if picks is not None else [None])
    return selector


class Mismatches:
    """Collects exit_location_mismatch signals for the duration of a test."""

    def __init__(self) -> None:
        self.events: list[dict] = []


@pytest.fixture
def mismatches() -> Iterator[Mismatches]:
    sink = Mismatches()

    # blinker only awaits plain coroutine functions, not objects with an async __call__.
    async def receive(sender: object, **kwargs: object) -> None:
        sink.events.append(kwargs)

    exit_location_mismatch.connect(receive)
    yield sink
    exit_location_mismatch.disconnect(receive)


class TestVerify:
    async def test_match_keeps_the_selection(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector()
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.RETRY), _proxy("gb"), session_id="s", country="gb", target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        selector.select_proxy_for_project.assert_not_called()
        assert mismatches.events == []

    async def test_retry_moves_to_another_proxy(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.RETRY), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert selector.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"us"})
        assert len(mismatches.events) == 1
        assert mismatches.events[0]["proxy_id"] == "us" and mismatches.events[0]["expected"] == "GB"
        assert mismatches.events[0]["observed"] == "US" and mismatches.events[0]["project_id"] == "proj"

    async def test_retry_gives_up_after_max_attempts(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("us2"), _proxy("us")])
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.RETRY), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert decision.rejected and "requested GB" in (decision.rejection or "")
        # max_attempts is 3: the first proxy plus two replacements were checked.
        assert len(mismatches.events) == 3

    async def test_retry_stops_when_nothing_else_is_eligible(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        decision = await ExitVerifier(checker, _selector(picks=[None])).verify(_project(PreflightMode.RETRY), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert decision.rejected and len(mismatches.events) == 1

    async def test_sticky_session_is_rejected_not_moved(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.RETRY), _proxy("us"), session_id="sess", country="GB", target_host="x")
        assert decision.rejected
        selector.select_proxy_for_project.assert_not_called()
        assert len(mismatches.events) == 1

    async def test_sticky_without_session_may_move(self, checker: PreflightChecker) -> None:
        selector = _selector("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.RETRY), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"

    async def test_report_forwards_without_a_mismatch_signal(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        decision = await ExitVerifier(checker, _selector()).verify(_project(PreflightMode.REPORT), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"
        assert mismatches.events == []

    async def test_reject_fails_first_mismatch(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(PreflightMode.REJECT), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert decision.rejected and len(mismatches.events) == 1
        selector.select_proxy_for_project.assert_not_called()

    async def test_off_skips_everything(self, checker: PreflightChecker) -> None:
        decision = await ExitVerifier(checker, _selector()).verify(_project(PreflightMode.OFF), _proxy("us"), session_id=None, country="GB", target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"


class TestStrategyHook:
    def test_defaults_allow_reselection(self) -> None:
        assert RoundRobinStrategy().allows_exit_reselection("s") and RoundRobinStrategy().allows_exit_reselection(None)

    def test_sticky_refuses_for_sessions(self) -> None:
        assert StickySessionStrategy().allows_exit_reselection(None)
        assert not StickySessionStrategy().allows_exit_reselection("sess")
