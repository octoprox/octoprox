# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project lines of Needs attention, each kind at the edges of its cut-offs, read off a project's data built in
memory: definitions compiled into the routing preview by the feed's rules, and the counts and runtime state the loader
would have read beside them; each cut-off a project's alert settings move, and the lines a wider line covers."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

import pytest

from api.attention.checks import (
    CHECKS,
    AttentionKind,
    ConnectorErrorCheck,
    CountriesCheck,
    Finding,
    RefusalCheck,
    build_report,
)
from api.attention.data import FailureCount, ProjectData, SiteReputation
from api.core import utc_now
from api.db.repository import ConnectorStatusCount, ProxyPlacement, RequestTally
from api.geo.models import META_LOCATION_VERDICT, ContradictedAction
from api.models.alerts import AlertSettings
from api.models.attention import Severity
from api.models.connector import (
    BYTES_PER_GB,
    Connector,
    TrafficLimitAction,
    TrafficPeriod,
    TrafficUsage,
)
from api.models.credential import Credential, CredentialType
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy, ProxyStatus
from api.reputation.record import ConnectorSiteKey, ConnectorSiteReputation
from api.reputation.split import ReputationSplit
from api.services.read_model import Pools
from tests.routing.builder import FeedBuilder

PROJECT = "p"
STATIC = CredentialType.STATIC_PROXY_PROVIDER.value
OXYLABS = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
SITE = "www.google.com"
FAR = timedelta(days=3650)


class Scene:
    """One project's definitions and stored state, and the data the checks read off them."""

    def __init__(self, **project: Any) -> None:
        self.now = utc_now()
        self.project = Project(id=PROJECT, name="Shop", username="client", password="pw", **project)
        self.feed = FeedBuilder().add(self.project)
        self.thresholds = AlertSettings()
        self.failures: list[FailureCount] = []
        self.project_requests = 0
        # None: every request succeeded.
        self.successes: int | None = None
        self.requests_at = self.now - timedelta(minutes=1)
        self.connector_requests: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.traffic: dict[str, TrafficUsage] = {}
        self.reputation: SiteReputation | None = None
        self._made = 0

    def connector(self, name: str, type: str = STATIC, *, credential: dict[str, Any] | None = None, **fields: Any) -> Connector:
        """A connector of the project on a credential of its own."""
        made = Credential(id=f"k-{name}", name=name, type=type, project_id=PROJECT, config=credential or {})
        connector = Connector(
            id=f"c-{name}", name=name, credential_id=made.id, credential_type=type, project_id=PROJECT, **fields
        )
        self.feed.add(made, connector)
        return connector

    def proxies(self, connector: Connector, n: int = 1, status: ProxyStatus = ProxyStatus.HEALTHY, **fields: Any) -> list[Proxy]:
        made = []
        for _ in range(n):
            self._made += 1
            proxy = Proxy(id=f"x{self._made}", host=f"h{self._made}", port=1, connector_id=connector.id, status=status, **fields)
            self.feed.add(proxy)
            made.append(proxy)
        return made

    def quarantine(self, *proxies: Proxy) -> None:
        for proxy in proxies:
            self.feed.quarantined[proxy.id] = self.now + FAR

    def block(self, connector: Connector) -> None:
        self.blocked.add(connector.id)
        self.feed.blocked_until[connector.id] = self.now + FAR

    def fail(
        self,
        connector: Connector | None,
        kind: str,
        n: int,
        status: int | None = None,
        *,
        ago: timedelta = timedelta(minutes=1),
    ) -> None:
        """Failures as the metrics flush counted them, the newest ``ago``."""
        self.failures.append(
            FailureCount(connector.id if connector else None, kind, status, n, self.now - ago)
        )

    def data(self) -> ProjectData:
        connectors = tuple(c for c in self.feed.connectors.values())
        statuses: dict[str, Counter[ProxyStatus]] = {c.id: Counter() for c in connectors}
        for proxy in self.feed.proxies.values():
            statuses[proxy.connector_id][proxy.status] += 1
        counts = [
            ConnectorStatusCount(PROJECT, connector_id, status, count)
            for connector_id, by_status in statuses.items()
            for status, count in by_status.items()
        ]
        enabled = {c.id for c in connectors if c.enabled}
        pools = Pools(
            counts=counts,
            quarantined={
                proxy_id: ProxyPlacement(self.feed.proxies[proxy_id].connector_id, self.feed.proxies[proxy_id].status)
                for proxy_id in self.feed.quarantined
            },
            counted=enabled - self.blocked,
        )
        successes = self.project_requests if self.successes is None else self.successes
        return ProjectData(
            project=self.project,
            thresholds=self.thresholds,
            connectors=connectors,
            statuses=statuses,
            pool=pools.pool([c.id for c in connectors]),
            blocked=frozenset(enabled & self.blocked),
            failures=tuple(self.failures),
            success=RequestTally(requests=self.project_requests, successes=successes, last_at=self.requests_at),
            connector_requests=self.connector_requests,
            preview=self.feed.router(),
            registry=self.feed.registry,
            now=self.now,
            traffic=self.traffic,
            reputation=self.reputation,
        )

    def lines(self, *kinds: AttentionKind) -> list[Finding]:
        """The project's lines in display order, those of ``kinds`` only when given."""
        found = build_report(self.data(), CHECKS).findings
        return [finding for finding in found if not kinds or finding.kind in kinds]

    def hidden(self) -> dict[str, str]:
        """Each line a shown one makes redundant, by key, with the key of the line covering it."""
        return {finding.key: by for finding, by in build_report(self.data(), CHECKS).hidden}


def _serving(scene: Scene, name: str = "Healthy", n: int = 4) -> Connector:
    """A connector whose healthy proxies keep the pool lines quiet."""
    connector = scene.connector(name)
    scene.proxies(connector, n)
    return connector


def _one(found: list[Finding]) -> Finding:
    assert len(found) == 1, found
    return found[0]


# --- Connectors off and the pool ----------------------------------------------------------------


class TestConnectorsOff:
    def test_no_connector_asks_for_one_and_every_one_off_says_all_requests_fail(self) -> None:
        scene = Scene()
        empty = _one(scene.lines())
        assert (empty.key, empty.severity, empty.text) == (
            "connectors_off", Severity.PROBLEM, "Add a connector to start serving requests."
        )
        assert (empty.link, empty.action) == ("connectors", "Connectors")

        scene.connector("Off", enabled=False, config={"countries": ["DE"]})
        scene.connector("Leaving", enabled=False, pending_deletion=True)
        off = _one(scene.lines())
        assert off.text == "Every connector is turned off, so all requests fail."

    def test_it_makes_the_pool_and_countries_lines_redundant(self) -> None:
        scene = Scene()
        off = scene.connector("Off", enabled=False, config={"countries": ["DE"]}, routing_config={"reserved_countries": ["DE"]})
        scene.proxies(off, 3, ProxyStatus.UNHEALTHY)
        scene.fail(None, "no_proxy_available", 500)

        assert [finding.key for finding in scene.lines()] == ["connectors_off"]
        # Still found, so its alert stays open, covered, and an until-clear mute on it holds while it is hidden.
        assert scene.hidden() == {
            "countries_unserved": "connectors_off",
            "refusals:no_proxy_available": "connectors_off",
        }


class TestPool:
    def test_no_routable_proxy_counts_as_the_hero_does(self) -> None:
        scene = Scene()
        connector = scene.connector("Static")
        scene.proxies(connector, 3, ProxyStatus.UNHEALTHY)
        scene.proxies(connector, 1, ProxyStatus.DEGRADED)
        scene.quarantine(*scene.proxies(connector, 2, ProxyStatus.HEALTHY))
        scene.proxies(connector, 1, ProxyStatus.DRAINING)
        scene.fail(None, "no_proxy_available", 500)

        line = _one(scene.lines())
        assert (line.key, line.severity, line.subject) == ("pool_no_routable", Severity.PROBLEM, "No proxy can take requests")
        assert line.text == ": 4 unhealthy, 2 quarantined by rate limits. Requests fail until one recovers."
        assert (line.link, line.action) == ("proxies", "Proxies")

    def test_none_quarantined_leaves_the_quarantine_out(self) -> None:
        scene = Scene()
        scene.proxies(scene.connector("Static"), 2, ProxyStatus.UNHEALTHY)

        assert _one(scene.lines()).text == ": 2 unhealthy. Requests fail until one recovers."

    def test_every_proxy_quarantined_says_clients_are_told_to_retry(self) -> None:
        scene = Scene()
        scene.quarantine(*scene.proxies(scene.connector("Static"), 3))
        scene.fail(None, "all_proxies_quarantined", 400)

        line = _one(scene.lines())
        assert (line.subject, line.text) == (
            "Every proxy", " is quarantined by its rate limit, so clients are told to retry later."
        )

    def test_every_connector_over_its_traffic_limit(self) -> None:
        scene = Scene()
        for name in ("A", "B"):
            connector = scene.connector(name)
            scene.proxies(connector, 2)
            scene.block(connector)
        scene.connector("Off", enabled=False)
        scene.fail(None, "traffic_limit_exceeded", 400)

        line = _one(scene.lines())
        assert (line.subject, line.link, line.action) == ("Every connector", "connectors", "Connectors")
        assert line.text == " is over its traffic limit, so requests are turned away until a limit resets or is raised."

    def test_rows_awaiting_their_first_check_are_no_line(self) -> None:
        """New static rows wait in unknown, new vendor slots in initializing: their first check is seconds away."""
        scene = Scene()
        connector = scene.connector("New")
        scene.proxies(connector, 2, ProxyStatus.UNKNOWN)
        scene.proxies(connector, 2, ProxyStatus.INITIALIZING)
        scene.proxies(connector, 1, ProxyStatus.DRAINING)

        assert scene.lines() == []

    def test_an_empty_pool_is_no_line(self) -> None:
        """An on-demand country pool or a cloud connector at no instance provisions on the first request."""
        scene = Scene()
        scene.connector("Empty")

        assert scene.lines() == []

    @pytest.mark.parametrize(
        ("unhealthy", "healthy", "shown"),
        [
            pytest.param(3, 9, True, id="3 of 12 is a quarter"),
            pytest.param(3, 10, False, id="3 of 13 is under"),
            pytest.param(2, 2, False, id="2 rows are too few"),
            pytest.param(3, 3, True, id="3 rows"),
        ],
    )
    def test_the_unhealthy_share(self, unhealthy: int, healthy: int, shown: bool) -> None:
        scene = Scene()
        connector = scene.connector("Mixed")
        scene.proxies(connector, healthy)
        scene.proxies(connector, unhealthy, ProxyStatus.UNHEALTHY)
        # Rows retiring or awaiting their first check count for nothing.
        scene.proxies(connector, 5, ProxyStatus.DRAINING)
        scene.proxies(connector, 5, ProxyStatus.UNKNOWN)

        found = scene.lines(AttentionKind.POOL_UNHEALTHY_SHARE)

        assert bool(found) is shown
        if shown:
            line = found[0]
            assert (line.subject, line.severity) == (f"{unhealthy} of {unhealthy + healthy} proxies", Severity.WARNING)
            assert line.text == " keep failing health checks and get no requests, most on Mixed."

    def test_the_share_leaves_out_a_connector_with_no_healthy_proxy_its_own_line_names(self) -> None:
        scene = Scene()
        _serving(scene, n=6)
        scene.proxies(scene.connector("Dead"), 6, ProxyStatus.UNHEALTHY)

        assert [line.kind for line in scene.lines()] == [AttentionKind.CONNECTOR_NO_HEALTHY_PROXY]


# --- Countries ----------------------------------------------------------------------------------


class TestCountries:
    def test_a_country_listed_by_a_connector_that_takes_requests_and_nothing_serves(self) -> None:
        scene = Scene()
        _serving(scene)
        scene.connector("Germany", config={"countries": ["DE"]})

        line = _one(scene.lines())
        assert (line.key, line.severity) == ("countries_unserved", Severity.PROBLEM)
        assert line.lead + line.subject + line.text == "Requests for Germany fail: no connector can serve it right now."
        assert (line.subject, line.link, line.action) == ("Germany", "countries?country=DE", "Countries")

    def test_a_country_reserved_by_a_connector_turned_off_fails_and_one_only_excluded_does_not(self) -> None:
        scene = Scene()
        # Exits everywhere, untargeted: it would serve France but for the reservation.
        everywhere = scene.connector("Everywhere", routing_config={"excluded_countries": ["IT"]})
        scene.proxies(everywhere, 2, metadata={"country": "FR"})
        scene.proxies(everywhere, 2)
        scene.connector("French", enabled=False, config={"countries": ["FR"]}, routing_config={"reserved_countries": ["FR"]})
        # Listed by a connector that takes no requests: not a promise.
        scene.connector("Spanish", enabled=False, config={"countries": ["ES"]})

        line = _one(scene.lines())
        assert (line.subject, line.link) == ("France", "countries?country=FR")

    def test_a_connector_being_deleted_reserves_nothing(self) -> None:
        scene = Scene()
        everywhere = scene.connector("Everywhere")
        scene.proxies(everywhere, 2, metadata={"country": "FR"})
        scene.connector(
            "Leaving", enabled=False, pending_deletion=True, config={"countries": ["FR"]},
            routing_config={"reserved_countries": ["FR"]},
        )

        assert scene.lines() == []

    @pytest.mark.parametrize(
        ("codes", "subject", "pronoun"),
        [
            (["DE"], "Germany", "it"),
            (["FR", "DE"], "France and Germany", "them"),
            (["IT", "DE", "FR"], "France, Germany and Italy", "them"),
            (["IT", "DE", "FR", "ES"], "France, Germany and 2 more countries", "them"),
        ],
    )
    def test_several_countries_fold_into_one_line_by_name(self, codes: list[str], subject: str, pronoun: str) -> None:
        scene = Scene()
        _serving(scene)
        scene.connector("Listing", config={"countries": codes})

        line = _one(CountriesCheck().findings(scene.data()))
        assert (line.subject, line.text) == (subject, f" fail: no connector can serve {pronoun} right now.")
        assert line.link == ("countries?country=DE" if len(codes) == 1 else "countries")


# --- Request windows ----------------------------------------------------------------------------


class TestVendorRefusals:
    @pytest.mark.parametrize(
        ("refused", "requests", "shown"),
        [
            pytest.param(9, 9, False, id="9 refusals are too few"),
            pytest.param(10, 10, True, id="10 refusals"),
            pytest.param(19, 100, False, id="19 percent"),
            pytest.param(20, 100, True, id="20 percent"),
        ],
    )
    def test_a_rejected_login(self, refused: int, requests: int, shown: bool) -> None:
        scene = Scene()
        connector = _serving(scene, "Webshare UK")
        scene.connector_requests[connector.id] = requests
        scene.fail(connector, "upstream_rejected", refused, 407)

        found = scene.lines()

        assert bool(found) is shown
        if shown:
            line = found[0]
            pct = round(refused / requests * 100)
            assert (line.key, line.kind, line.severity) == (
                f"vendor_login:{connector.id}", AttentionKind.VENDOR_LOGIN, Severity.PROBLEM
            )
            assert (line.lead, line.subject) == ("", "Webshare UK")
            assert line.text == f": the vendor is rejecting its login ({pct}% of requests in the last 15 minutes)."
            assert line.detail == f"The vendor answered 407 to {refused} of {requests} requests."
            assert line.when is not None and (line.when.kind, line.when.at) == ("last_at", scene.now - timedelta(minutes=1))
            assert (line.link, line.action) == (f"connectors?open={connector.id}", "Open connector")

    @pytest.mark.parametrize("status", [429, 403])
    @pytest.mark.parametrize(
        ("refused", "severity"),
        [(49, None), (50, Severity.WARNING), (499, Severity.WARNING), (500, Severity.PROBLEM)],
    )
    def test_too_many_requests_and_forbidden(self, status: int, refused: int, severity: Severity | None) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.connector_requests[connector.id] = 1000
        scene.fail(connector, "upstream_rejected", refused, status)

        found = scene.lines()

        assert [line.severity for line in found] == ([severity] if severity else [])
        if severity:
            kind = AttentionKind.VENDOR_RATE_LIMITED if status == 429 else AttentionKind.VENDOR_FORBIDDEN
            assert found[0].kind == kind
            assert found[0].detail == f"The vendor answered {status} to {refused} of 1000 requests."

    @pytest.mark.parametrize(("refused", "shown"), [(299, False), (300, True)])
    def test_a_place_the_vendor_cannot_serve(self, refused: int, shown: bool) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.connector_requests[connector.id] = 1000
        scene.fail(connector, "upstream_rejected", 100, 400)
        scene.fail(connector, "upstream_rejected", 100, 502)
        scene.fail(connector, "upstream_rejected", refused - 200, 503)

        found = scene.lines()

        assert bool(found) is shown
        if shown:
            line = found[0]
            assert (line.kind, line.severity) == (AttentionKind.VENDOR_CANNOT_SERVE, Severity.WARNING)
            assert line.text == (
                ": the vendor could not serve 30% of its requests in the last 15 minutes, for example a place it has "
                "no exit in."
            )
            assert line.detail == "The vendor answered 400, 502 or 503 to 300 of 1000 requests."

    @pytest.mark.parametrize(("refused", "shown"), [(299, False), (300, True)])
    def test_any_other_refusal_names_the_most_frequent_status(self, refused: int, shown: bool) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.connector_requests[connector.id] = 1000
        scene.fail(connector, "upstream_rejected", refused - 100, None)
        scene.fail(connector, "upstream_rejected", 100, 418)

        found = scene.lines()

        assert bool(found) is shown
        if shown:
            line = found[0]
            assert line.kind == AttentionKind.VENDOR_REFUSED_OTHER
            assert line.text == ": the vendor refused 30% of its requests in the last 15 minutes."
            assert line.detail == "The vendor refused 300 of 1000 requests, most often with no status."

    def test_a_window_not_flushed_yet_never_divides_by_zero(self) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.fail(connector, "upstream_rejected", 12, 407)

        line = _one(scene.lines())
        assert "(100% of requests" in line.text and line.detail == "The vendor answered 407 to 12 of 12 requests."

    def test_a_connector_that_takes_no_requests_gets_no_vendor_line(self) -> None:
        scene = Scene()
        _serving(scene)
        off = scene.connector("Off", enabled=False)
        scene.fail(off, "upstream_rejected", 100, 407)

        assert scene.lines() == []

    @pytest.mark.parametrize(("ago", "shown"), [(timedelta(minutes=4, seconds=59), True), (timedelta(minutes=5, seconds=1), False)])
    def test_a_line_goes_five_minutes_after_its_last_failure(self, ago: timedelta, shown: bool) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.fail(connector, "upstream_rejected", 50, 407, ago=timedelta(minutes=12))
        scene.fail(connector, "upstream_rejected", 50, 407, ago=ago)

        assert bool(scene.lines()) is shown


class TestConnectorFailing:
    @pytest.mark.parametrize(
        ("failed", "requests", "severity"),
        [
            pytest.param(49, 49, None, id="49 requests are too few"),
            pytest.param(50, 50, Severity.PROBLEM, id="50 requests"),
            pytest.param(299, 1000, None, id="29.9 percent"),
            pytest.param(300, 1000, Severity.WARNING, id="30 percent"),
            pytest.param(799, 1000, Severity.WARNING, id="79.9 percent"),
            pytest.param(800, 1000, Severity.PROBLEM, id="80 percent"),
        ],
    )
    def test_the_cut_offs(self, failed: int, requests: int, severity: Severity | None) -> None:
        scene = Scene()
        connector = _serving(scene, "Slow")
        scene.connector_requests[connector.id] = requests
        scene.fail(connector, "upstream_timeout", failed)

        found = scene.lines()

        assert [line.severity for line in found] == ([severity] if severity else [])
        if severity:
            line = found[0]
            assert line.kind == AttentionKind.CONNECTOR_FAILING
            assert line.text == (
                f": {round(failed / requests * 100)}% of its requests failed in the last 15 minutes, mostly because "
                "requests timed out."
            )

    @pytest.mark.parametrize(
        ("kind", "reason"),
        [
            ("upstream_unreachable", "its proxies could not be reached"),
            ("upstream_error", "connections through it dropped"),
            ("preflight_mismatch", "exits were not in the place asked"),
        ],
    )
    def test_the_main_reason_by_the_largest_kind(self, kind: str, reason: str) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.connector_requests[connector.id] = 100
        scene.fail(connector, "upstream_timeout", 20)
        scene.fail(connector, kind, 21)

        assert _one(scene.lines()).text.endswith(f"mostly because {reason}.")

    def test_refusals_hang_ups_and_cuts_are_not_the_connector_failing(self) -> None:
        scene = Scene()
        connector = _serving(scene)
        scene.connector_requests[connector.id] = 100
        scene.fail(connector, "upstream_rejected", 9, 418)
        for kind in ("client_closed", "interrupted"):
            scene.fail(connector, kind, 90)

        assert scene.lines() == []


class TestRefusals:
    @pytest.mark.parametrize(
        ("refused", "severity"),
        [(49, None), (50, Severity.WARNING), (249, Severity.WARNING), (250, Severity.PROBLEM)],
    )
    def test_a_share_of_the_attempts(self, refused: int, severity: Severity | None) -> None:
        """Attempts are the requests the metrics counted and those refused before a proxy."""
        scene = Scene()
        _serving(scene)
        scene.project_requests = 1000 - refused
        scene.fail(None, "no_proxy_available", refused)

        found = scene.lines()

        assert [line.severity for line in found] == ([severity] if severity else [])
        if severity:
            line = found[0]
            assert (line.key, line.kind) == ("refusals:no_proxy_available", AttentionKind.REFUSALS)
            assert (line.subject, line.link, line.action) == (
                f"{refused} requests ({round(refused / 10)}%)", "failures", "Failures"
            )
            assert line.text == (
                " in the last 15 minutes failed: no proxy matched the country, city or proxy they asked for."
            )

    def test_a_location_that_could_not_be_read_at_any_share(self) -> None:
        scene = Scene()
        _serving(scene)
        scene.project_requests = 100_000
        scene.fail(None, "invalid_location", 9)
        assert scene.lines() == []

        scene.fail(None, "invalid_location", 1)
        line = _one(scene.lines())
        assert (line.subject, line.severity) == ("10 requests (0%)", Severity.WARNING)
        assert line.text.endswith("such as an unknown country, state or city. Check the client's username.")

    def test_each_kind_its_own_line_in_a_fixed_order(self) -> None:
        scene = Scene()
        _serving(scene)
        scene.project_requests = 100
        kinds = ["exits_avoided", "traffic_limit_exceeded", "all_proxies_quarantined", "no_proxy_available"]
        for n, kind in enumerate(kinds):
            scene.fail(None, kind, 10 + n)

        expected = [f"refusals:{kind}" for kind in reversed(kinds)]
        assert [line.key for line in RefusalCheck().findings(scene.data())] == expected
        assert [line.key for line in scene.lines()] == expected


# --- Connectors ---------------------------------------------------------------------------------


class TestConnectorErrors:
    def test_a_vendor_list_that_fails_and_cloud_instances_that_cannot_be_managed(self) -> None:
        scene = Scene()
        _serving(scene)
        failed_at = scene.now - timedelta(minutes=6)
        synced = scene.connector(
            "Oxylabs", "oxylabs", credential=OXYLABS,
            last_error="407 Proxy Authentication Required", last_error_at=failed_at, consecutive_errors=3,
        )
        cloud = scene.connector("AWS", "aws", last_error="UnauthorizedOperation", last_error_at=failed_at, consecutive_errors=1)
        # A static connector holds no vendor list or cloud: nothing.
        scene.connector("Static", last_error="?", consecutive_errors=2)
        # Written on a failed delivery and not counted yet: no line.
        scene.connector("Retrying", "oxylabs", credential=OXYLABS, last_error="timeout", consecutive_errors=0)

        lines = {line.kind: line for line in ConnectorErrorCheck().findings(scene.data())}

        sync = lines[AttentionKind.CONNECTOR_SYNC_FAILING]
        assert (sync.key, sync.subject) == (f"connector_sync_failing:{synced.id}", "Oxylabs")
        assert sync.text == " could not get its proxy list from the vendor the last 3 times. It keeps the proxies it had."
        assert sync.detail == "407 Proxy Authentication Required"
        assert sync.when is not None and (sync.when.kind, sync.when.at) == ("last_failed", failed_at)
        error = lines[AttentionKind.CONNECTOR_CLOUD_ERROR]
        assert (error.key, error.severity) == (f"connector_cloud_error:{cloud.id}", Severity.WARNING)
        assert error.text == " could not start, stop or check its cloud instances the last time. Running instances keep serving."
        assert len(lines) == 2

    def test_a_connector_that_takes_no_requests_gets_no_error_line(self) -> None:
        scene = Scene()
        _serving(scene)
        scene.connector("Off", "oxylabs", credential=OXYLABS, enabled=False, last_error="x", consecutive_errors=4)

        assert scene.lines() == []


class TestNoHealthyProxy:
    def test_every_proxy_checked_and_failing(self) -> None:
        scene = Scene()
        _serving(scene)
        dead = scene.connector("Dead")
        scene.proxies(dead, 2, ProxyStatus.UNHEALTHY)
        scene.proxies(dead, 1, ProxyStatus.DEGRADED)

        line = _one(scene.lines())
        assert (line.key, line.subject, line.severity) == (
            f"connector_no_healthy_proxy:{dead.id}", "Dead", Severity.WARNING
        )
        assert line.text == " has no healthy proxy, so it takes no requests."

    @pytest.mark.parametrize("status", [ProxyStatus.UNKNOWN, ProxyStatus.INITIALIZING])
    def test_a_proxy_awaiting_its_first_check_is_no_line(self, status: ProxyStatus) -> None:
        scene = Scene()
        _serving(scene)
        dead = scene.connector("Dead")
        scene.proxies(dead, 2, ProxyStatus.UNHEALTHY)
        scene.proxies(dead, 1, status)

        assert scene.lines() == []

    def test_healthy_proxies_in_quarantine_are_no_line(self) -> None:
        scene = Scene()
        _serving(scene)
        scene.quarantine(*scene.proxies(scene.connector("Resting"), 2))

        assert scene.lines() == []

    def test_a_blocked_connector_is_its_traffic_line_s(self) -> None:
        scene = Scene()
        _serving(scene)
        dead = scene.connector("Dead")
        scene.proxies(dead, 2, ProxyStatus.UNHEALTHY)
        scene.block(dead)

        assert scene.lines() == []

    def test_more_than_three_fold_into_one_line(self) -> None:
        scene = Scene()
        _serving(scene, n=40)
        for name in ("delta", "Alpha", "charlie", "Bravo"):
            scene.proxies(scene.connector(name), 1, ProxyStatus.UNHEALTHY)

        line = _one(scene.lines())
        assert (line.key, line.subject, line.link, line.action) == (
            "connector_no_healthy_proxy", "4 connectors", "connectors", "Connectors"
        )
        assert line.text == " have no healthy proxy, so they take no requests: Alpha, Bravo and 2 more."
        # Each folded connector's own line stays found, covered by the combined one, for its alert and its mute.
        hidden = scene.hidden()
        assert len(hidden) == 4
        assert all(
            key.startswith("connector_no_healthy_proxy:") and by == "connector_no_healthy_proxy"
            for key, by in hidden.items()
        )

        scene = Scene()
        _serving(scene, n=40)
        for name in ("Alpha", "Bravo", "Charlie"):
            scene.proxies(scene.connector(name), 1, ProxyStatus.UNHEALTHY)
        assert [line.subject for line in scene.lines()] == ["Alpha", "Bravo", "Charlie"]
        assert scene.hidden() == {}


def _usage(
    status: str, action: TrafficLimitAction, *, blocked: bool = False, pending: bool = False, used: float = 0.85
) -> TrafficUsage:
    now = utc_now()
    limit = 50 * BYTES_PER_GB
    return TrafficUsage(
        period=TrafficPeriod.MONTH,
        period_start=now - timedelta(days=10),
        period_end=now + timedelta(days=20),
        bytes_sent=0,
        bytes_received=int(limit * used),
        total_bytes=int(limit * used),
        limit_bytes=limit,
        percent=used * 100,
        action=action,
        status=status,  # type: ignore[arg-type]
        blocked=blocked,
        pending=pending,
        currency="USD",
    )


class TestTraffic:
    @pytest.mark.parametrize(
        ("usage", "kind", "text"),
        [
            pytest.param(
                _usage("warning", TrafficLimitAction.BLOCK), AttentionKind.CONNECTOR_TRAFFIC_NEAR,
                " has used 85% of its 50 GB limit this month. New requests stop at the limit.", id="near a block",
            ),
            pytest.param(
                _usage("warning", TrafficLimitAction.INTERRUPT), AttentionKind.CONNECTOR_TRAFFIC_NEAR,
                " has used 85% of its 50 GB limit this month. New requests stop at the limit.", id="near a cut",
            ),
            pytest.param(_usage("warning", TrafficLimitAction.ALERT), None, None, id="near an alert"),
            pytest.param(
                _usage("exceeded", TrafficLimitAction.ALERT, used=1.2), AttentionKind.CONNECTOR_TRAFFIC_OVER_ALERT,
                " is at 120% of its 50 GB limit this month. The limit is set to alert only, so it keeps taking requests.",
                id="over an alert",
            ),
            pytest.param(_usage("exceeded", TrafficLimitAction.BLOCK, used=1.0), None, None, id="over, block not stored yet"),
            pytest.param(
                _usage("exceeded", TrafficLimitAction.BLOCK, blocked=True, used=1.0),
                AttentionKind.CONNECTOR_TRAFFIC_BLOCKED,
                " has used its 50 GB limit this month and takes no new requests until it resets or the limit is raised.",
                id="blocked",
            ),
            pytest.param(
                _usage("exceeded", TrafficLimitAction.BLOCK, blocked=True, pending=True, used=1.0), None, None,
                id="a decision pending",
            ),
        ],
    )
    def test_each_state(self, usage: TrafficUsage, kind: AttentionKind | None, text: str | None) -> None:
        scene = Scene()
        connector = _serving(scene, "Metered")
        scene.traffic[connector.id] = usage

        found = scene.lines()

        assert [line.kind for line in found] == ([kind] if kind else [])
        if kind:
            line = found[0]
            assert (line.text, line.severity) == (text, Severity.WARNING)
            assert line.when is not None and (line.when.kind, line.when.at) == ("resets", usage.period_end)

    def test_a_terabyte_limit_reads_in_terabytes(self) -> None:
        scene = Scene()
        connector = _serving(scene)
        usage = _usage("warning", TrafficLimitAction.BLOCK)
        scene.traffic[connector.id] = usage.model_copy(
            update={"limit_bytes": 1500 * BYTES_PER_GB, "total_bytes": 1350 * BYTES_PER_GB}
        )

        assert _one(scene.lines()).text == " has used 90% of its 1.5 TB limit this month. New requests stop at the limit."


# --- Exits --------------------------------------------------------------------------------------


class TestExitLocations:
    @pytest.mark.parametrize(("inside", "shown"), [pytest.param(87, True, id="10 percent"), pytest.param(88, False, id="9.9 percent")])
    def test_proxies_outside_the_listed_countries(self, inside: int, shown: bool) -> None:
        scene = Scene()
        connector = scene.connector("German", config={"countries": ["DE"]})
        scene.proxies(connector, inside, metadata={"country": "DE", "vendor_country": "DE"})
        scene.proxies(connector, 10, metadata={"country": "FR", "vendor_country": "DE"})
        # Pinned by hand: never outside.
        scene.proxies(connector, 3, metadata={"country": "FR", "vendor_country": "DE", "country_source": "manual"})
        # Retiring: counted nowhere.
        scene.proxies(connector, 50, ProxyStatus.DRAINING, metadata={"country": "FR", "vendor_country": "DE"})

        found = scene.lines(AttentionKind.EXITS_OUTSIDE_COUNTRIES)

        assert bool(found) is shown
        if shown:
            assert found[0].text == (
                f": 10 of {inside + 13} proxies are in countries the connector does not list, so they take no requests."
            )
        # Three at least, whatever the share.
        scene = Scene()
        connector = scene.connector("German", config={"countries": ["DE"]})
        scene.proxies(connector, 2, metadata={"country": "FR", "vendor_country": "DE"})
        assert scene.lines(AttentionKind.EXITS_OUTSIDE_COUNTRIES) == []

    @pytest.mark.parametrize(
        ("rule", "text"),
        [
            (ContradictedAction.EVIDENCE, None),
            (ContradictedAction.REFUSE, ": 3 of 12 checked proxies are not in the country the vendor says, so they are skipped."),
            (
                ContradictedAction.VENDOR,
                ": 3 of 12 checked proxies are not in the country the vendor says, so some requests for that country "
                "are served from elsewhere.",
            ),
        ],
    )
    def test_checked_proxies_not_where_the_vendor_says(self, rule: ContradictedAction, text: str | None) -> None:
        scene = Scene(location_contradicted=rule)
        connector = _serving(scene, "Vendor")
        scene.proxies(connector, 9, metadata={META_LOCATION_VERDICT: "confirmed"})
        scene.proxies(connector, 3, metadata={META_LOCATION_VERDICT: "contradicted"})
        # Session slots under refuse are replaced within seconds: left out there, counted elsewhere.
        if rule == ContradictedAction.REFUSE:
            scene.proxies(connector, 5, metadata={META_LOCATION_VERDICT: "contradicted", "session_id": "s"})

        found = scene.lines(AttentionKind.EXITS_CONTRADICTED)

        assert [line.text for line in found] == ([text] if text else [])

    def test_a_quarter_of_the_checked_proxies_at_least(self) -> None:
        scene = Scene(location_contradicted=ContradictedAction.REFUSE)
        connector = _serving(scene)
        scene.proxies(connector, 10, metadata={META_LOCATION_VERDICT: "confirmed"})
        scene.proxies(connector, 3, metadata={META_LOCATION_VERDICT: "contradicted"})

        assert scene.lines() == []


class TestReputation:
    def _scene(self, *, dyn_factors: dict[str, tuple[float, float]], avoided: set[str], dyn_on: bool = True) -> Scene:
        """A static pool exiting at two IPs and an Oxylabs dynamic connector, the site's records as given."""
        scene = Scene(exit_reputation=ExitReputationMode.RECORD)
        pool = scene.connector("Pool")
        for n in range(2):
            scene.proxies(pool, metadata={"discovered_ip": f"192.0.2.{n + 1}"})
        dyn = scene.connector(
            "Dyn", "oxylabs", credential=OXYLABS, enabled=dyn_on,
            config={"session_mode": "dynamic", "session_duration_minutes": 10},
        )
        scene.feed.add(Proxy(
            id="gw", host="pr.oxylabs.io", port=7777, username="customer-alice-sessid-probe0probe0-sesstime-10",
            password="{password}", connector_id=dyn.id, status=ProxyStatus.HEALTHY,
            metadata={"provider": "oxylabs", "proxy_type": "residential", "dynamic_sessions": "true"},
        ))
        records = []
        for place, (factor, requests) in dyn_factors.items():
            record = ConnectorSiteReputation.new(ConnectorSiteKey(PROJECT, dyn.id, SITE, place), scene.now)
            record.factor = factor
            record.weighted_requests = requests
            records.append(record)
        preview = scene.feed.router()
        split = ReputationSplit(
            scene.project, preview, place="", avoided={SITE: frozenset(avoided)}, records=[], now=scene.now
        )
        scene.reputation = SiteReputation(sites=(SITE,), split=split, coarse=tuple(records))
        return scene

    def test_a_site_blocking_half_on_every_connector(self) -> None:
        # Request-weighted across a country and no place: (0.2 * 30 + 0.6 * 10) / 40.
        scene = self._scene(dyn_factors={"": (0.2, 30.0), "US": (0.6, 10.0)}, avoided={"192.0.2.1"})

        line = _one(scene.lines())
        assert (line.key, line.subject, line.severity) == (
            f"reputation_site_blocked:{SITE}", SITE, Severity.WARNING
        )
        assert line.text == " is blocking at least half of the requests on every connector."
        assert (line.link, line.action) == ("locations", "Exit locations")

    def test_one_connector_above_half_is_no_line(self) -> None:
        scene = self._scene(dyn_factors={"": (0.2, 10.0), "US": (0.9, 30.0)}, avoided={"192.0.2.1"})

        assert scene.lines() == []

    def test_one_serving_connector_is_named(self) -> None:
        scene = self._scene(dyn_factors={}, avoided={"192.0.2.1", "192.0.2.2"}, dyn_on=False)

        assert _one(scene.lines()).text == " is blocking at least half of the requests through Pool."

    def test_a_dynamic_connector_without_a_factor_counts_as_fine(self) -> None:
        scene = self._scene(dyn_factors={}, avoided={"192.0.2.1", "192.0.2.2"})

        assert scene.lines() == []


class TestStuckDeleting:
    @pytest.mark.parametrize(
        ("minutes", "age"), [(29, None), (30, "30 minutes"), (61, "1 hour"), (3 * 60 + 5, "3 hours"), (2 * 24 * 60, "2 days")]
    )
    def test_still_being_deleted_after_half_an_hour(self, minutes: int, age: str | None) -> None:
        scene = Scene()
        _serving(scene)
        leaving = scene.connector(
            "AWS", "aws", enabled=False, pending_deletion=True, updated_at=scene.now - timedelta(minutes=minutes)
        )

        found = scene.lines()

        assert [line.text for line in found] == (
            [f" is still being deleted after {age}: its cloud instances have not all shut down."] if age else []
        )
        if age:
            assert (found[0].key, found[0].link) == (f"connector_stuck_deleting:{leaving.id}", f"connectors?open={leaving.id}")


# --- The report ---------------------------------------------------------------------------------


class TestReport:
    def test_problems_first_then_warnings_each_by_kind_then_name(self) -> None:
        scene = Scene()
        bravo = _serving(scene, "bravo")
        alpha = _serving(scene, "Alpha")
        for connector in (bravo, alpha):
            scene.connector_requests[connector.id] = 100
            scene.fail(connector, "upstream_rejected", 10, 429)
            scene.fail(connector, "upstream_rejected", 30, 407)
        scene.fail(None, "invalid_location", 10)

        keys = [line.key for line in scene.lines()]

        assert keys == [
            f"vendor_login:{alpha.id}",
            f"vendor_login:{bravo.id}",
            f"vendor_rate_limited:{alpha.id}",
            f"vendor_rate_limited:{bravo.id}",
            "refusals:invalid_location",
        ]

    def test_a_refresh_with_other_counts_changes_words_in_place(self) -> None:
        """Rows are ordered by kind and name, never by count: the second refresh's numbers move nothing."""
        scene = Scene()
        connector = _serving(scene)
        scene.project_requests = 100
        scene.connector_requests[connector.id] = 100
        scene.fail(connector, "upstream_rejected", 10, 429)
        scene.fail(None, "no_proxy_available", 10)
        scene.fail(None, "exits_avoided", 12)
        before = scene.lines()

        scene.failures = []
        scene.fail(connector, "upstream_rejected", 30, 429)
        scene.fail(None, "no_proxy_available", 11)
        scene.fail(None, "exits_avoided", 20)
        after = scene.lines()

        assert [line.key for line in after] == [line.key for line in before] == [
            f"vendor_rate_limited:{connector.id}", "refusals:no_proxy_available", "refusals:exits_avoided"
        ]
        assert [line.subject for line in after] != [line.subject for line in before]

    def test_every_kind_has_a_family_and_every_family_runs(self) -> None:
        assert sorted(kind for check in CHECKS for kind in check.kinds) == sorted(AttentionKind)


# --- Alert settings -----------------------------------------------------------------------------


def _vendor(scene: Scene, status: int, refused: int, requests: int = 1000) -> list[Finding]:
    """The lines of one connector whose vendor refused ``refused`` of ``requests`` with ``status``."""
    connector = _serving(scene)
    scene.connector_requests[connector.id] = requests
    scene.fail(connector, "upstream_rejected", refused, status)
    return scene.lines()


class TestThresholds:
    """Each cut-off the project's alert settings set moves its line's edge, the share compared exactly."""

    @pytest.mark.parametrize(
        ("status", "warning", "problem"),
        [
            (429, "vendor_rate_limited_warning_percent", "vendor_rate_limited_problem_percent"),
            (403, "vendor_forbidden_warning_percent", "vendor_forbidden_problem_percent"),
        ],
    )
    @pytest.mark.parametrize(
        ("refused", "severity"),
        [(99, None), (100, Severity.WARNING), (599, Severity.WARNING), (600, Severity.PROBLEM)],
    )
    def test_the_429_and_403_pairs(
        self, status: int, warning: str, problem: str, refused: int, severity: Severity | None
    ) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(**{warning: 10, problem: 60})

        assert [line.severity for line in _vendor(scene, status, refused)] == ([severity] if severity else [])

    @pytest.mark.parametrize(("refused", "shown"), [(39, False), (40, True)])
    def test_the_login_problem(self, refused: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(vendor_login_problem_percent=40)

        assert bool(_vendor(scene, 407, refused, 100)) is shown

    @pytest.mark.parametrize(
        ("status", "field"), [(502, "vendor_cannot_serve_warning_percent"), (418, "vendor_other_warning_percent")]
    )
    @pytest.mark.parametrize(("refused", "shown"), [(99, False), (100, True)])
    def test_cannot_serve_and_other_refusals(self, status: int, field: str, refused: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(**{field: 10})

        assert bool(_vendor(scene, status, refused)) is shown

    @pytest.mark.parametrize(("refused", "shown"), [(19, False), (20, True)])
    def test_the_vendor_minimum_is_its_own(self, refused: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(vendor_min=20, refusals_min=1)

        assert bool(_vendor(scene, 407, refused, refused)) is shown

    @pytest.mark.parametrize(
        ("refused", "severity"),
        [(99, None), (100, Severity.WARNING), (399, Severity.WARNING), (400, Severity.PROBLEM)],
    )
    def test_the_refusal_pair(self, refused: int, severity: Severity | None) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(refusals_warning_percent=10, refusals_problem_percent=40)
        _serving(scene)
        scene.project_requests = 1000 - refused
        scene.fail(None, "no_proxy_available", refused)

        assert [line.severity for line in scene.lines()] == ([severity] if severity else [])

    @pytest.mark.parametrize(
        ("kind", "field"), [("no_proxy_available", "refusals_min"), ("invalid_location", "invalid_location_min")]
    )
    @pytest.mark.parametrize(("refused", "shown"), [(29, False), (30, True)])
    def test_the_refusal_minimums(self, kind: str, field: str, refused: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(**{field: 30})
        _serving(scene)
        scene.fail(None, kind, refused)

        assert bool(scene.lines()) is shown

    @pytest.mark.parametrize(
        ("failed", "requests", "severity"),
        [
            pytest.param(99, 99, None, id="99 requests are too few"),
            pytest.param(100, 100, Severity.PROBLEM, id="100 requests"),
            pytest.param(499, 1000, None, id="49.9 percent"),
            pytest.param(500, 1000, Severity.WARNING, id="50 percent"),
            pytest.param(899, 1000, Severity.WARNING, id="89.9 percent"),
            pytest.param(900, 1000, Severity.PROBLEM, id="90 percent"),
        ],
    )
    def test_the_connector_failing_cut_offs(self, failed: int, requests: int, severity: Severity | None) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(
            connector_failing_min_requests=100,
            connector_failing_warning_percent=50,
            connector_failing_problem_percent=90,
        )
        connector = _serving(scene)
        scene.connector_requests[connector.id] = requests
        scene.fail(connector, "upstream_timeout", failed)

        assert [line.severity for line in scene.lines()] == ([severity] if severity else [])

    @pytest.mark.parametrize(("errors", "shown"), [(2, False), (3, True)])
    def test_the_errors_in_a_row(self, errors: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(connector_errors_min=3)
        _serving(scene)
        scene.connector("Oxylabs", "oxylabs", credential=OXYLABS, last_error="timeout", consecutive_errors=errors)

        assert bool(scene.lines()) is shown

    @pytest.mark.parametrize(("minutes", "shown"), [(59, False), (60, True)])
    def test_the_minutes_a_delete_may_take(self, minutes: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(stuck_deleting_minutes=60)
        _serving(scene)
        scene.connector(
            "AWS", "aws", enabled=False, pending_deletion=True, updated_at=scene.now - timedelta(minutes=minutes)
        )

        assert bool(scene.lines()) is shown

    @pytest.mark.parametrize(
        ("unhealthy", "healthy", "shown"),
        [
            pytest.param(4, 4, False, id="4 rows are too few"),
            pytest.param(5, 5, True, id="half"),
            pytest.param(5, 6, False, id="under half"),
        ],
    )
    def test_the_unhealthy_share(self, unhealthy: int, healthy: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(unhealthy_share_percent=50, unhealthy_min=5)
        connector = scene.connector("Mixed")
        scene.proxies(connector, healthy)
        scene.proxies(connector, unhealthy, ProxyStatus.UNHEALTHY)

        assert bool(scene.lines(AttentionKind.POOL_UNHEALTHY_SHARE)) is shown

    @pytest.mark.parametrize(("outside", "shown"), [(4, False), (5, True)])
    def test_the_exits_outside_cut_offs(self, outside: int, shown: bool) -> None:
        scene = Scene()
        scene.thresholds = AlertSettings(exits_outside_percent=50, exits_outside_min=5)
        connector = scene.connector("German", config={"countries": ["DE"]})
        scene.proxies(connector, 5, metadata={"country": "DE", "vendor_country": "DE"})
        scene.proxies(connector, outside, metadata={"country": "FR", "vendor_country": "DE"})

        assert bool(scene.lines(AttentionKind.EXITS_OUTSIDE_COUNTRIES)) is shown

    @pytest.mark.parametrize(("contradicted", "shown"), [(3, False), (4, True)])
    def test_the_contradicted_cut_offs(self, contradicted: int, shown: bool) -> None:
        scene = Scene(location_contradicted=ContradictedAction.REFUSE)
        scene.thresholds = AlertSettings(exits_contradicted_percent=50, exits_contradicted_min=4)
        connector = _serving(scene, "Vendor", n=0)
        scene.proxies(connector, 4, metadata={META_LOCATION_VERDICT: "confirmed"})
        scene.proxies(connector, contradicted, metadata={META_LOCATION_VERDICT: "contradicted"})

        assert bool(scene.lines(AttentionKind.EXITS_CONTRADICTED)) is shown


class TestSiteBlockedThreshold:
    @pytest.mark.parametrize(("factor", "shown"), [(0.25, False), (0.2, True)])
    def test_a_share_other_than_half_is_named(self, factor: float, shown: bool) -> None:
        scene = TestReputation()._scene(dyn_factors={"": (factor, 30.0)}, avoided={"192.0.2.1", "192.0.2.2"})
        scene.thresholds = AlertSettings(site_blocked_percent=80)

        found = scene.lines()

        assert [line.text for line in found] == (
            [" is blocking at least 80% of the requests on every connector."] if shown else []
        )


# --- Covered lines ------------------------------------------------------------------------------


class TestCovered:
    def test_no_routable_proxy_covers_the_lines_it_explains(self) -> None:
        scene = Scene()
        connector = scene.connector("Static")
        scene.proxies(connector, 3, ProxyStatus.UNHEALTHY)
        scene.fail(None, "no_proxy_available", 500)

        assert [line.key for line in scene.lines()] == ["pool_no_routable"]
        assert scene.hidden() == {
            f"connector_no_healthy_proxy:{connector.id}": "pool_no_routable",
            "refusals:no_proxy_available": "pool_no_routable",
        }

    def test_the_fold_covers_past_three_connectors_and_uncovers_back_at_three(self) -> None:
        scene = Scene()
        _serving(scene, n=40)
        dead = [scene.connector(name) for name in ("Alpha", "Bravo", "Charlie", "Delta")]
        for connector in dead:
            scene.proxies(connector, 1, ProxyStatus.UNHEALTHY)
        assert set(scene.hidden()) == {f"connector_no_healthy_proxy:{connector.id}" for connector in dead}

        scene.feed.proxies = {key: proxy for key, proxy in scene.feed.proxies.items() if proxy.connector_id != dead[-1].id}
        assert scene.hidden() == {}
        assert [line.subject for line in scene.lines()] == ["Alpha", "Bravo", "Charlie"]


# --- Success rate -------------------------------------------------------------------------------


class TestSuccessRate:
    def _scene(self, requests: int, successes: int, **thresholds: Any) -> Scene:
        scene = Scene()
        scene.thresholds = AlertSettings(**thresholds)
        _serving(scene)
        scene.project_requests = requests
        scene.successes = successes
        return scene

    @pytest.mark.parametrize(
        ("successes", "severity"),
        [
            pytest.param(800, None, id="80 percent"),
            pytest.param(799, Severity.WARNING, id="79.9 percent"),
            pytest.param(500, Severity.WARNING, id="50 percent"),
            pytest.param(499, Severity.PROBLEM, id="49.9 percent"),
        ],
    )
    def test_the_warning_and_the_problem(self, successes: int, severity: Severity | None) -> None:
        found = self._scene(1000, successes).lines()

        assert [line.severity for line in found] == ([severity] if severity else [])
        if severity:
            line = found[0]
            assert (line.key, line.kind) == ("success_rate", AttentionKind.SUCCESS_RATE)
            assert (line.lead, line.text) == ("Only ", " succeeded in the last 15 minutes.")
            assert line.detail == (
                f"{successes} of 1000 requests succeeded. Below 80% is a warning, below 50% a problem."
            )
            assert (line.when, line.link, line.action) == (None, "failures", "Failures")

    def test_the_rate_is_rounded_down_so_it_never_reads_as_the_cut_off(self) -> None:
        line = _one(self._scene(1000, 799).lines())

        assert line.subject == "79% of requests"

    @pytest.mark.parametrize(("requests", "shown"), [(99, False), (100, True)])
    def test_it_needs_the_minimum_of_requests(self, requests: int, shown: bool) -> None:
        assert bool(self._scene(requests, 0).lines()) is shown
        assert bool(self._scene(requests, 0, success_min_requests=requests + 1).lines()) is False

    def test_problem_at_zero_is_never_a_problem(self) -> None:
        line = _one(self._scene(1000, 0, success_problem_below=0).lines())

        assert line.severity == Severity.WARNING
        assert line.detail == "0 of 1000 requests succeeded. Below 80% is a warning."

    def test_it_can_be_turned_off(self) -> None:
        assert self._scene(1000, 0, success_rate=False).lines() == []

    def test_a_window_whose_newest_row_is_stale_is_no_line(self) -> None:
        scene = self._scene(1000, 0)
        scene.requests_at = scene.now - timedelta(minutes=5, seconds=1)

        assert scene.lines() == []

    def test_connectors_off_and_no_routable_cover_it(self) -> None:
        scene = Scene()
        scene.connector("Off", enabled=False)
        scene.project_requests, scene.successes = 1000, 0
        assert [line.key for line in scene.lines()] == ["connectors_off"]
        assert scene.hidden() == {"success_rate": "connectors_off"}

        scene = Scene()
        scene.proxies(scene.connector("Static"), 3, ProxyStatus.UNHEALTHY)
        scene.project_requests, scene.successes = 1000, 0
        assert scene.hidden()["success_rate"] == "pool_no_routable"

        scene = Scene()
        blocked = scene.connector("Static")
        scene.proxies(blocked, 3)
        scene.block(blocked)
        scene.project_requests, scene.successes = 1000, 0
        assert [line.subject for line in scene.lines()] == ["Every connector"]
        assert scene.hidden()["success_rate"] == "pool_no_routable"

    def test_it_shows_beside_the_lines_that_name_a_cause_after_the_refusals(self) -> None:
        scene = self._scene(1000, 100)
        rejected = _serving(scene, "Rejected")
        scene.connector_requests[rejected.id] = 100
        scene.fail(rejected, "upstream_rejected", 30, 407)
        failing = _serving(scene, "Failing")
        scene.connector_requests[failing.id] = 100
        scene.fail(failing, "upstream_timeout", 90)
        scene.fail(None, "no_proxy_available", 400)

        assert [line.kind for line in scene.lines()] == [
            AttentionKind.VENDOR_LOGIN,
            AttentionKind.CONNECTOR_FAILING,
            AttentionKind.REFUSALS,
            AttentionKind.SUCCESS_RATE,
        ]
        assert scene.hidden() == {}
