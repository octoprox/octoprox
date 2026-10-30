# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ExitVerifier: what preflight does with the selection, per assurance level and strategy."""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from api.core.config import Settings
from api.core.http_tunnel import TunnelRefusedError
from api.geo.models import ContradictedAction, LocationRules, UnknownAction
from api.geo.observations import ObservationRecorder
from api.geo.preflight import PreflightChecker, PreflightVerdict
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.geo.verifier import ExitDecision, ExitIp, ExitSource, ExitVerifier
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.sources import IpDiscoverer
from api.routing.router import Router
from api.strategies import get_strategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy
from tests.core.test_http_tunnel import _vendor
from tests.geo.doubles import Refusals, Retired, Sightings, logged_refusals
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
US_IP = "203.0.113.9"  # not in the test database; the echo endpoint's country decides


def use_echo_by_proxy(geo_service: GeoService, exits: dict[str, tuple[str, str] | Exception | None]) -> None:
    """Make the service's echo requests answer per proxy host: host -> (ip, country), None for an echo that fails, or the
    error the upstream fails it with."""

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        exit_ = exits[proxy_url.rsplit("@", 1)[-1].split(":")[0]]

        def answer(request: httpx.Request) -> httpx.Response:
            if isinstance(exit_, Exception):
                raise exit_
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

    async def test_a_pinned_request_retries_only_on_its_pinned_proxy(self, checker: PreflightChecker, refusals: Refusals) -> None:
        """The retry asks for the pinned proxy with that proxy excluded, which the router answers with nothing."""
        router = _router(picks=[None])
        decision = await ExitVerifier(checker, router).verify(
            _project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x",
            pinned_proxy_id="us",
        )
        assert decision.rejected and len(refusals.events) == 1
        kwargs = router.select_proxy_for_project.await_args.kwargs
        assert kwargs["pinned_proxy_id"] == "us" and kwargs["exclude"] == frozenset({"us"})

    async def test_a_refusal_names_the_proxy_whose_echo_refused_it(self, checker: PreflightChecker) -> None:
        """The message is the last verdict's, so the refusal is counted against the proxy that verdict was on."""
        router = _router(picks=[_proxy("us2"), None])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.rejected and decision.proxy is not None and decision.proxy.id == "us2"

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


class TestExitIp:
    """What the decision tells the exit headers: the echo it forwards on, else the row's last recorded exit."""

    async def test_an_accepted_echo_is_the_exit(self, checker: PreflightChecker) -> None:
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("us"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        # The replacement's echo, not the refused one's.
        assert decision.exit_ip is not None and decision.exit_ip.checked_at is not None
        assert (decision.exit_ip.ip, decision.exit_ip.source) == (GB_IP, ExitSource.PREFLIGHT)
        assert decision.known_exit() == decision.exit_ip

    def test_a_cached_verdict_says_so(self) -> None:
        stamp = datetime(2026, 10, 27, 9, 30)
        verdict = PreflightVerdict(ok=True, expected=None, observed=None, ip=GB_IP, reason="observed", checked_at=stamp, cached=True)
        assert ExitIp.of_verdict(verdict) == ExitIp(GB_IP, ExitSource.CACHED, stamp)
        assert ExitIp.of_verdict(verdict._replace(ip=None)) is None

    async def test_without_an_echo_the_rows_last_exit_is_the_answer(self, checker: PreflightChecker) -> None:
        slot = _proxy("us")
        slot.metadata.update({"discovered_ip": US_IP, "location_checked_at": "2026-10-27T09:30:00"})
        decision = await ExitVerifier(checker, _router()).verify(_project(preflight=False), slot, session_id=None, location=None, target_host="x")
        assert decision.exit_ip is None
        assert decision.known_exit() == ExitIp(US_IP, ExitSource.LAST_SEEN, datetime(2026, 10, 27, 9, 30))
        # A row never seen has no exit to name.
        assert ExitDecision(_proxy("us")).known_exit() is None

    def test_an_answer_that_is_not_an_ip_names_no_exit(self) -> None:
        # The IP goes into a header line, and the echo endpoint or a vendor's list supplies it unchecked.
        injected = "203.0.113.9\r\nX-Octoprox-Exit-Source: preflight"
        verdict = PreflightVerdict(ok=True, expected=None, observed=None, ip=injected, reason="observed")
        assert ExitIp.of_verdict(verdict) is None
        slot = _proxy("us")
        slot.metadata["discovered_ip"] = injected
        assert ExitDecision(slot).known_exit() is None

    def test_a_dynamic_gateways_exit_is_known_only_from_its_echo(self) -> None:
        gateway = _proxy("us")
        gateway.metadata.update({"dynamic_sessions": "true", "discovered_ip": US_IP})
        assert ExitDecision(gateway).known_exit() is None


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


NO_UPSTREAM = TunnelRefusedError(400, "Bad Request", "no upstream available requested City xyzzy")


class TestUpstreamFailsTheEcho:
    """The upstream failing the echo is the request's own failure, retried only where another try could get past it."""

    @staticmethod
    def _echoes(checker: PreflightChecker, **failing: Exception) -> None:
        use_echo_by_proxy(checker._geo_service, {"gb": (GB_IP, "GB"), "us": (US_IP, "US"), **failing})

    @pytest.mark.parametrize(
        "error",
        [NO_UPSTREAM, TunnelRefusedError(407, "Proxy Authentication Required"), TunnelRefusedError(429, "Too Many Requests")],
        ids=["400", "407", "429"],
    )
    async def test_a_4xx_moves_to_another_connector(self, checker: PreflightChecker, error: Exception) -> None:
        """The vendor answered for its connector (a place, its credentials, its rate limit): another connector may serve."""
        self._echoes(checker, bad=error)
        elsewhere = _proxy("gb")
        elsewhere.connector_id = "conn2"
        router = _router(picks=[elsewhere])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is None and decision.proxy is not None and decision.proxy.id == "gb"
        kwargs = router.select_proxy_for_project.await_args.kwargs
        assert (kwargs["exclude"], kwargs["exclude_connectors"]) == (frozenset({"bad"}), frozenset({"conn"}))

    async def test_a_4xx_fails_the_request_when_no_other_connector_serves(self, checker: PreflightChecker, refusals: Refusals) -> None:
        self._echoes(checker, bad=NO_UPSTREAM)
        router = _router(picks=[None])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is NO_UPSTREAM and decision.proxy is not None and decision.proxy.id == "bad"
        assert not decision.rejected and refusals.events == []

    async def test_a_4xx_never_goes_back_to_its_connector(self, checker: PreflightChecker) -> None:
        """A 5xx on the second connector moves on again, still without the first connector the vendor refused for."""
        self._echoes(checker, bad=NO_UPSTREAM, bad2=TunnelRefusedError(502, "Bad Gateway"))
        second = _proxy("bad2")
        second.connector_id = "conn2"
        router = _router(picks=[second, None])
        await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        kwargs = router.select_proxy_for_project.await_args.kwargs
        assert (kwargs["exclude"], kwargs["exclude_connectors"]) == (frozenset({"bad", "bad2"}), frozenset({"conn"}))

    async def test_a_socks_refusal_fails_the_request_at_once(self, checker: PreflightChecker, refusals: Refusals) -> None:
        error = ProxyError("General SOCKS server failure")
        self._echoes(checker, bad=error)
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is error and decision.proxy is not None and decision.proxy.id == "bad"
        router.select_proxy_for_project.assert_not_called()
        assert refusals.events == []

    @pytest.mark.parametrize(
        "error",
        [TunnelRefusedError(502, "Bad Gateway"), ProxyConnectionError(None, "Couldn't connect to proxy"), ProxyTimeoutError("timed out")],
        ids=["5xx", "unreachable", "timeout"],
    )
    async def test_a_failure_another_try_could_pass_moves_on(self, checker: PreflightChecker, error: Exception) -> None:
        self._echoes(checker, bad=error)
        router = _router(picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is None and decision.proxy is not None and decision.proxy.id == "gb"
        assert router.select_proxy_for_project.await_args.kwargs["exclude"] == frozenset({"bad"})

    async def test_the_last_failure_is_the_requests(self, checker: PreflightChecker) -> None:
        last = TunnelRefusedError(503, "Service Unavailable")
        self._echoes(checker, bad=TunnelRefusedError(502, "Bad Gateway"), bad2=ProxyTimeoutError("timed out"), bad3=last)
        router = _router(picks=[_proxy("bad2"), _proxy("bad3")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is last and decision.proxy is not None and decision.proxy.id == "bad3"
        assert checker.checks == 3

    @pytest.mark.parametrize("error", [TunnelRefusedError(502, "Bad Gateway"), NO_UPSTREAM], ids=["5xx", "4xx"])
    async def test_a_sticky_session_is_not_moved(self, checker: PreflightChecker, error: Exception) -> None:
        self._echoes(checker, bad=error)
        router = _router("sticky", picks=[_proxy("gb")])
        decision = await ExitVerifier(checker, router).verify(_project(), _proxy("bad"), session_id="sess", location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is not None
        router.select_proxy_for_project.assert_not_called()

    @pytest.mark.parametrize("error", [NO_UPSTREAM, ProxyConnectionError(None, "Couldn't connect to proxy")], ids=["4xx", "unreachable"])
    async def test_the_upstreams_own_failure_whatever_the_rules(self, checker: PreflightChecker, error: Exception) -> None:
        """Rules that never refuse a location still fail the request: forwarding it would only meet the same upstream."""
        self._echoes(checker, bad=error)
        decision = await ExitVerifier(checker, _router()).verify(_project(TRUSTING), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert decision.upstream_error is error

    @pytest.mark.parametrize(
        "error",
        [TunnelRefusedError(502, "Bad Gateway", "Could not resolve host"), ProxyTimeoutError("timed out"), ProxyError("Host unreachable")],
        ids=["5xx", "timeout", "socks-refusal"],
    )
    async def test_a_failure_that_may_be_our_echo_follows_the_rules(self, checker: PreflightChecker, error: Exception) -> None:
        """A 5xx, silence or a SOCKS refusal may be our echo endpoint out of the upstream's reach: where nothing refuses, the
        request goes through as after an echo that failed past the upstream; where the rules could refuse, it fails with
        the upstream's answer."""
        self._echoes(checker, bad=error)
        trusting = await ExitVerifier(checker, _router()).verify(_project(TRUSTING), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert trusting.upstream_error is None and not trusting.rejected
        assert trusting.proxy is not None and trusting.proxy.id == "bad"
        verifying = await ExitVerifier(checker, _router()).verify(_project(), _proxy("bad"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert verifying.upstream_error is error

    async def test_an_echo_failing_past_the_upstream_is_still_judged_by_the_rules(self, checker: PreflightChecker) -> None:
        use_echo_by_proxy(checker._geo_service, {"down": None})
        trusting = await ExitVerifier(checker, _router()).verify(_project(TRUSTING), _proxy("down"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert not trusting.rejected and trusting.upstream_error is None
        strict = await ExitVerifier(checker, _router()).verify(_project(STRICT), _proxy("down"), session_id=None, location=LocationTarget(country="GB"), target_host="x")
        assert strict.rejected and (strict.rejection or "").startswith("Exit location not checked")


async def test_the_vendors_answer_comes_back_through_the_real_echo(checker: PreflightChecker) -> None:
    """End to end on the shipped echo client: the vendor refusing the echo's CONNECT reaches the decision with its words."""
    answer = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 42\r\n\r\nno upstream available requested City xyzzy"
    heard: list[bytes] = []
    async with _vendor(answer, heard) as port:
        geo_service = checker._geo_service
        geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec())  # type: ignore[method-assign]
        upstream = Proxy(
            id="gw", host="127.0.0.1", port=port, connector_id="conn", username="u", password="p",
            status=ProxyStatus.HEALTHY, metadata={"session_id": "slot-gw"},
        )
        decision = await ExitVerifier(checker, _router()).verify(_project(), upstream, session_id=None, location=LocationTarget(country="GB"), target_host="x")

    assert isinstance(decision.upstream_error, TunnelRefusedError)
    assert decision.upstream_error.vendor_message == "no upstream available requested City xyzzy"
    assert heard[0].startswith(b"CONNECT httpbin.org:443 HTTP/1.1\r\n")
