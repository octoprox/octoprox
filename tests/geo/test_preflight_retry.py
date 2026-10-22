# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ExitVerifier: what preflight does with the selection, per assurance level and strategy."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core.config import Settings
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
from api.routing.router import Router
from api.strategies import get_strategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy
from tests.geo.doubles import Refusals, Retired, Sightings, logged_refusals
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
US_IP = "203.0.113.9"  # not in the test database; the echo endpoint's country decides


def use_echo_by_proxy(geo_service: GeoService, exits: dict[str, tuple[str, str] | None]) -> None:
    """Make the service's echo requests answer per proxy host: host -> (ip, country), or None for an echo that fails."""

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        exit_ = exits[proxy_url.rsplit("@", 1)[-1].split(":")[0]]

        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503) if exit_ is None else httpx.Response(200, json={"ip": exit_[0], "country": exit_[1]})

        return httpx.AsyncClient(transport=httpx.MockTransport(answer), timeout=timeout)

    geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=factory)  # type: ignore[method-assign]


@pytest.fixture
async def checker(tmp_path: Path) -> PreflightChecker:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db)}])
    await store.sync_all()
    settings = Settings(instance_id="t", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]
    geo_service = GeoService(settings, store, GeoSettingsStore(settings, None), ObservationRecorder(None))
    use_echo_by_proxy(geo_service, {"gb": (GB_IP, "GB"), "us": (US_IP, "US"), "us2": (US_IP, "US")})
    return PreflightChecker(geo_service, None, sightings=Sightings(), exclusions=Retired())


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


def _router(strategy: str = "round_robin", picks: list[Proxy | None] | None = None) -> MagicMock:
    router = MagicMock(spec=Router)
    router.get_proxy = MagicMock(side_effect=lambda proxy_id: _proxy(proxy_id))  # every slot is still in the pool
    router.strategy_for_project = MagicMock(return_value=get_strategy(strategy))
    router.select_proxy_for_project = AsyncMock(side_effect=picks if picks is not None else [None])
    return router


@pytest.fixture
def refusals() -> Iterator[Refusals]:
    with logged_refusals() as logged:
        yield logged


class TestVerify:
    async def test_match_keeps_the_selection(self, checker: PreflightChecker, refusals: Refusals) -> None:
        router = _router()
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("gb"), session_id="s", location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        router.select_proxy_for_project.assert_not_called()
        assert refusals.events == []

    async def test_retry_moves_to_another_proxy(self, checker: PreflightChecker, refusals: Refusals) -> None:
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"us"})
        assert len(refusals.events) == 1
        assert refusals.events[0]["proxy_id"] == "us" and refusals.events[0]["expected"] == "GB"
        assert refusals.events[0]["observed"] == "US" and refusals.events[0]["project_id"] == "proj"

    async def test_retry_gives_up_after_max_attempts(self, checker: PreflightChecker, refusals: Refusals) -> None:
        router = _router(picks=[_proxy("us2"), _proxy("us")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and "requested GB" in (decision.rejection or "")
        # max_attempts is 3: the first proxy plus two replacements were checked.
        assert len(refusals.events) == 3

    async def test_retry_stops_when_nothing_else_is_eligible(self, checker: PreflightChecker, refusals: Refusals) -> None:
        decision = await ExitVerifier(checker, _router(picks=[None])).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and len(refusals.events) == 1

    async def test_sticky_session_is_rejected_not_moved(self, checker: PreflightChecker, refusals: Refusals) -> None:
        router = _router("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected
        router.select_proxy_for_project.assert_not_called()
        assert len(refusals.events) == 1

    async def test_sticky_without_session_may_move(self, checker: PreflightChecker) -> None:
        router = _router("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"

    async def test_rotating_dynamic_request_retries_by_rerendering(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """A session-less request on a dynamic gateway is retried on the same row: a re-selection mints a new vendor session."""
        gateway = _proxy("us")  # the echo sees this session in the US
        gateway.id = "gw"
        gateway.metadata.update({"dynamic_sessions": "true", "exit_sample_percent": 100})
        rerendered = _proxy("gb")
        rerendered.id = "gw"
        rerendered.metadata.update({"dynamic_sessions": "true", "exit_sample_percent": 100})
        router = _router("sticky", picks=[rerendered])
        decision = await ExitVerifier(checker, router).verify(
            _project(), gateway, session_id=None, location=LocationTarget(country="GB"), target_host="x"
        )
        assert not decision.rejected and decision.proxy is not None and decision.proxy.host == "gb"
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset()
        assert router.select_proxy_for_project.await_args.args[1] is None  # still no session on the retry
        # With an explicit client session the row is excluded like any other under a movable strategy.
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(
            _project(), gateway, session_id="order-1", location=LocationTarget(country="GB"), target_host="x"
        )
        assert not decision.rejected
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"gw"})

    async def test_trusting_rules_forward_without_a_refusal(self, checker: PreflightChecker, refusals: Refusals) -> None:
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(TRUSTING), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"
        assert refusals.events == [] and checker.checks == 1
        router.select_proxy_for_project.assert_not_called()

    async def test_one_attempt_fails_the_first_mismatch(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """Fail-fast on a movable strategy is the attempt count, not a mode."""
        checker._geo_service.settings.preflight_max_attempts = 1
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and len(refusals.events) == 1
        router.select_proxy_for_project.assert_not_called()

    async def test_off_skips_everything(self, checker: PreflightChecker) -> None:
        decision = await ExitVerifier(checker, _router()).verify(_project(preflight=False), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "us"
        assert checker.checks == 0

    async def test_refusing_unknown_exits_refuses_an_open_country_without_rotating(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """An exit no source can place is refused under confirmed, moved when the strategy allows, and never rotated."""
        use_echo_by_proxy(checker._geo_service, {"unknown": ("203.0.113.9", ""), "gb": (GB_IP, "GB")})
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(STRICT), _proxy("unknown"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"unknown"})
        assert len(refusals.events) == 1  # the refusal is logged; nothing rotates on missing evidence
        # Under sticky with a session there is nowhere to go: refused outright, naming the open country.
        router = _router("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(STRICT), _proxy("unknown"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and (decision.rejection or "").startswith("Exit country not confirmed: requested GB, observed unknown")
        # The same exit passes where unknown exits are trusted.
        decision = await ExitVerifier(checker, _router()).verify(_project(VERIFYING), _proxy("unknown"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected

    async def test_a_failed_echo_moves_like_any_refusal(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """A failed echo checked nothing: refused where an answer could have been, then moved when the strategy allows."""
        use_echo_by_proxy(checker._geo_service, {"down": None, "gb": (GB_IP, "GB")})
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("down"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"down"})
        assert len(refusals.events) == 1 and refusals.events[0]["proxy_id"] == "down"


class TestStrategyHook:
    def test_defaults_allow_reselection(self) -> None:
        assert RoundRobinStrategy().allows_exit_reselection("s") and RoundRobinStrategy().allows_exit_reselection(None)

    def test_sticky_refuses_for_sessions(self) -> None:
        assert StickySessionStrategy().allows_exit_reselection(None)
        assert not StickySessionStrategy().allows_exit_reselection("sess")


class TestRemovedSlot:
    async def test_slot_removed_by_its_own_sighting_does_not_serve_the_request(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """The echo matched the client's place, but the sighting judged the slot out of the pool: moved or 502, never forwarded."""
        gone = _proxy("gb")  # the echo lands in GB, as the client asked
        gone.id = "gone"
        router = _router(picks=[_proxy("gb")])
        router.get_proxy = MagicMock(side_effect=lambda proxy_id: None if proxy_id == "gone" else _proxy(proxy_id))
        dropped: list[str] = []

        async def invalidate(project_id: str, proxy_id: str) -> None:
            dropped.append(proxy_id)

        checker.invalidate = invalidate  # type: ignore[method-assign]
        decision = await ExitVerifier(checker, router).verify(_project(), gone, session_id=None, location=LocationTarget(country="GB"), target_host="x")
        # Moved to the replacement the router offered, and the stale verdict for the removed slot was dropped.
        assert not decision.rejected and decision.proxy is not None and decision.proxy.id == "gb"
        assert dropped == ["gone"] and router.select_proxy_for_project.await_count == 1
        # Under sticky with a session there is nowhere to go.
        sticky = _router("sticky")
        sticky.get_proxy = MagicMock(return_value=None)
        decision = await ExitVerifier(checker, sticky).verify(_project(), gone, session_id="s", location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and (decision.rejection or "").startswith("Proxy replaced")
