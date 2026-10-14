# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ExitVerifier: what preflight does with the selection, per assurance level and strategy."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.core.signals import exit_location_mismatch
from api.geo.models import ContradictedAction, LocationRules, UnknownAction
from api.geo.observations import ObservationRecorder
from api.geo.preflight import PreflightChecker
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.geo.verifier import ExitVerifier
from api.models.location import LocationTarget
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
    """A pooled session slot: preflight applies to session-bound rows only."""
    return Proxy(
        id=host, host=host, port=8000, connector_id="conn", username="u", password="p",
        status=ProxyStatus.HEALTHY, metadata={"session_id": f"slot-{host}"},
    )


TRUSTING = LocationRules(ContradictedAction.VENDOR, UnknownAction.VENDOR)
VERIFYING = LocationRules(ContradictedAction.REFUSE, UnknownAction.VENDOR)
STRICT = LocationRules(ContradictedAction.REFUSE, UnknownAction.REFUSE)


def _project(rules: LocationRules = VERIFYING, preflight: bool = True) -> Project:
    return Project(
        id="proj", name="P", username="u", password="p", location_preflight=preflight,
        location_contradicted=rules.contradicted, location_unknown=rules.unknown,
    )


def _selector(strategy: str = "round_robin", picks: list[Proxy | None] | None = None) -> MagicMock:
    selector = MagicMock()
    selector.get_proxy = MagicMock(side_effect=lambda proxy_id: _proxy(proxy_id))  # every slot is still in the pool
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
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("gb"), session_id="s", location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        selector.select_proxy_for_project.assert_not_called()
        assert mismatches.events == []

    async def test_retry_moves_to_another_proxy(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert selector.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"us"})
        assert len(mismatches.events) == 1
        assert mismatches.events[0]["proxy_id"] == "us" and mismatches.events[0]["expected"] == "GB"
        assert mismatches.events[0]["observed"] == "US" and mismatches.events[0]["project_id"] == "proj"

    async def test_retry_gives_up_after_max_attempts(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("us2"), _proxy("us")])
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and "requested GB" in (decision.rejection or "")
        # max_attempts is 3: the first proxy plus two replacements were checked.
        assert len(mismatches.events) == 3

    async def test_retry_stops_when_nothing_else_is_eligible(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        decision = await ExitVerifier(checker, _selector(picks=[None])).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and len(mismatches.events) == 1

    async def test_sticky_session_is_rejected_not_moved(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("us"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected
        selector.select_proxy_for_project.assert_not_called()
        assert len(mismatches.events) == 1

    async def test_sticky_without_session_may_move(self, checker: PreflightChecker) -> None:
        selector = _selector("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"

    async def test_rotating_dynamic_request_retries_by_rerendering(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        """A session-less request on a dynamic gateway is retried on the same row: a re-selection mints a new vendor session."""
        gateway = _proxy("us")  # the echo sees this session in the US
        gateway.id = "gw"
        gateway.metadata.update({"dynamic_sessions": "true", "exit_sample_percent": 100})
        rerendered = _proxy("gb")
        rerendered.id = "gw"
        rerendered.metadata.update({"dynamic_sessions": "true", "exit_sample_percent": 100})
        selector = _selector("sticky", picks=[rerendered])
        decision = await ExitVerifier(checker, selector).verify(
            _project(), gateway, session_id=None, location=LocationTarget(country="GB"), target_host="x"
        )
        assert not decision.rejected and decision.proxy is not None and decision.proxy.host == "gb"
        assert selector.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset()
        assert selector.select_proxy_for_project.await_args.args[1] is None  # still no session on the retry
        # With an explicit client session the row is excluded like any other under a movable strategy.
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(
            _project(), gateway, session_id="order-1", location=LocationTarget(country="GB"), target_host="x"
        )
        assert not decision.rejected
        assert selector.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"gw"})

    async def test_trusting_rules_forward_without_a_mismatch_signal(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(TRUSTING), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"
        assert mismatches.events == [] and checker.checks == 1
        selector.select_proxy_for_project.assert_not_called()

    async def test_one_attempt_fails_the_first_mismatch(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        """Fail-fast on a movable strategy is the attempt count, not a mode."""
        checker._geo_service.settings.preflight_max_attempts = 1
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and len(mismatches.events) == 1
        selector.select_proxy_for_project.assert_not_called()

    async def test_off_skips_everything(self, checker: PreflightChecker) -> None:
        decision = await ExitVerifier(checker, _selector()).verify(_project(preflight=False), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"
        assert checker.checks == 0

    async def test_refusing_unknown_exits_refuses_an_open_country_without_rotating(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        """An exit no source can place is refused under confirmed, moved when the strategy allows, and never rotated."""
        use_echo_by_proxy(checker._geo_service, {"unknown": ("203.0.113.9", ""), "gb": (GB_IP, "GB")})
        selector = _selector(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(STRICT), _proxy("unknown"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert selector.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"unknown"})
        assert len(mismatches.events) == 1  # the refusal is announced; nothing rotates on missing evidence
        # Under sticky with a session there is nowhere to go: refused outright, naming the open country.
        selector = _selector("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, selector).verify(_project(STRICT), _proxy("unknown"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and (decision.rejection or "").startswith("Exit country not confirmed: requested GB, observed unknown")
        # The same exit passes where unknown exits are trusted.
        decision = await ExitVerifier(checker, _selector()).verify(_project(VERIFYING), _proxy("unknown"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected


class TestStrategyHook:
    def test_defaults_allow_reselection(self) -> None:
        assert RoundRobinStrategy().allows_exit_reselection("s") and RoundRobinStrategy().allows_exit_reselection(None)

    def test_sticky_refuses_for_sessions(self) -> None:
        assert StickySessionStrategy().allows_exit_reselection(None)
        assert not StickySessionStrategy().allows_exit_reselection("sess")


class TestRemovedSlot:
    async def test_slot_removed_by_its_own_sighting_does_not_serve_the_request(self, checker: PreflightChecker, mismatches: Mismatches) -> None:
        """The echo matched the client's place, but the sighting judged the slot out of the pool: moved or 502, never forwarded."""
        gone = _proxy("gb")  # the echo lands in GB, as the client asked
        gone.id = "gone"
        selector = _selector(picks=[_proxy("gb")])
        selector.get_proxy = MagicMock(side_effect=lambda proxy_id: None if proxy_id == "gone" else _proxy(proxy_id))
        dropped: list[str] = []

        async def invalidate(project_id: str, proxy_id: str) -> None:
            dropped.append(proxy_id)

        checker.invalidate = invalidate  # type: ignore[method-assign]
        decision = await ExitVerifier(checker, selector).verify(_project(), gone, session_id=None, location=LocationTarget(country="GB"), target_host="x")
        # Moved to the replacement the selector offered, and the stale verdict for the removed slot was dropped.
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert dropped == ["gone"] and selector.select_proxy_for_project.await_count == 1
        # Under sticky with a session there is nowhere to go.
        sticky = _selector("sticky")
        sticky.get_proxy = MagicMock(return_value=None)
        decision = await ExitVerifier(checker, sticky).verify(_project(), gone, session_id="s", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and (decision.rejection or "").startswith("Proxy replaced")
