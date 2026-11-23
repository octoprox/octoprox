# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The split the api shows against routing: for a request to a site from a place, the effective weights of
ReputationSplit are the weights the router hands the project's strategy, whatever domain filters, reservations and
countries keep connectors or rows off the request; one split serves every place; where each connector stands at a
place; and one site across every place (SitePlaces).

Feeds are built in memory. The split reads the api's preview of the same compile, which carries no exit reputation, and
is handed the records the feed's reputation section was made from.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from api.core import utc_now
from api.feed.models import REPUTATION_SECTION
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy, ProxyProtocol
from api.reputation.record import ConnectorSiteKey, ConnectorSiteReputation
from api.reputation.split import PlaceLevel, ReputationSplit, SitePlaces, place_label, place_target
from api.reputation.weight import PlaceState
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.strategies import ProxyGroup
from tests.routing.builder import FeedBuilder, healthy

SITE = "www.google.com"
# The dynamic connector's stored factors for the site, by level of the requested place.
FACTORS = {"": 0.8, "US": 0.5, "US/NY/new_york": 0.3, "GB": 0.9}


def _feed(pool_routing: dict[str, Any], rows: dict[str, dict[str, str]], mode: ExitReputationMode) -> FeedBuilder:
    """Project ``p`` with a static ``pool`` (weight 1, ``pool_routing``) holding ``rows`` (exit IP -> location
    metadata), and an Oxylabs dynamic connector ``dyn`` (weight 3) with ``FACTORS`` for the site."""
    feed = FeedBuilder().add(
        Project(id="p", name="P", username="client", password="pw", routing_strategy="random", exit_reputation=mode),
        Credential(id="k", name="K", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p"),
        Connector(
            id="pool", name="Pool", credential_id="k", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p", routing_config={"weight": 1, **pool_routing},
        ),
        Credential(
            id="oxy", name="Oxylabs", type="oxylabs", project_id="p",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ),
        Connector(
            id="dyn", name="Dyn", credential_id="oxy", credential_type="oxylabs", project_id="p",
            config={"session_mode": "dynamic", "session_duration_minutes": 10}, routing_config={"weight": 3},
        ),
        healthy(Proxy(
            id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
            username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}", connector_id="dyn",
            metadata={"provider": "oxylabs", "proxy_type": "residential", "dynamic_sessions": "true"},
        )),
    )
    for n, (ip, location) in enumerate(rows.items()):
        feed.add(healthy(Proxy(id=f"r{n}", host=f"r{n}", port=1, connector_id="pool", metadata={"discovered_ip": ip, **location})))
    for place, factor in FACTORS.items():
        feed.factor("p", "dyn", SITE, place, factor)
    return feed


def _record(
    connector_id: str, place: str, factor: float | None, reports: int = 0, block_rate: float | None = None
) -> ConnectorSiteReputation:
    """A record of the connector for the site at ``place``, storing ``factor``, with ``reports`` blocked reports, and
    weights of 100 requests blocked at ``block_rate`` (one less the factor when not given)."""
    record = ConnectorSiteReputation.new(ConnectorSiteKey("p", connector_id, SITE, place), utc_now())
    record.factor = factor
    record.blocked_reports = reports
    if factor is not None:
        record.weighted_requests = 100.0
        record.weighted_blocked = 100.0 * (1.0 - factor if block_rate is None else block_rate)
    return record


def _records() -> list[ConnectorSiteReputation]:
    """The dynamic connector's records the feed's factors were read from."""
    return [_record("dyn", place, factor) for place, factor in FACTORS.items()]


async def _routed(router: Router, place: str) -> dict[str, float]:
    """The weights the project's strategy is handed for one request to the site from ``place``, by connector."""
    strategy = router.strategy_for_project("p")
    handed: list[list[ProxyGroup]] = []
    select = strategy.select_weighted

    async def spy(groups: list[ProxyGroup], *args: Any, **kwargs: Any) -> Proxy | None:
        handed.append(groups)
        return await select(groups, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strategy, "select_weighted", spy)
        await router.select_proxy_for_project("p", None, SITE, place_target(place))
    return {group.key: group.weight for groups in handed for group in groups}


def _split(
    feed: FeedBuilder, avoided: set[str], records: list[ConnectorSiteReputation] | None = None
) -> ReputationSplit:
    """The api's split for the site: over the preview, the compile without the reputation section."""
    sections = {name: section for name, section in feed.sections().items() if name != REPUTATION_SECTION}
    preview = Router(RoutingSnapshot.from_sections(sections, registry=feed.registry))
    project = preview.get_project("p")
    assert project is not None
    return ReputationSplit(
        project,
        preview,
        avoided={SITE: frozenset(avoided)},
        records=_records() if records is None else records,
        now=utc_now(),
    )


# One row in New York City, one elsewhere in New York state, two in California.
US = {
    f"198.51.100.{n}": {"country": "US", "state_code": "NY" if n <= 2 else "CA", **({"city": "new_york"} if n == 1 else {})}
    for n in range(1, 5)
}
GB = {f"203.0.113.{n}": {"country": "GB"} for n in range(1, 5)}
# Rows with no known country: they serve requests naming none.
UNPLACED = {f"192.0.2.{n}": {} for n in range(1, 5)}


@pytest.mark.parametrize(
    ("pool_routing", "rows", "avoided", "place"),
    [
        pytest.param({}, UNPLACED, {"192.0.2.1"}, "", id="no place"),
        pytest.param({"domain_blacklist": ["google.com"]}, UNPLACED, set(), "", id="the site's domain refused"),
        pytest.param({"domain_whitelist": ["bing.com"]}, US, set(), "US", id="only another domain allowed"),
        pytest.param({"reserved_countries": ["US"]}, US | GB, {"198.51.100.1"}, "US", id="a reserved country"),
        pytest.param({"reserved_countries": ["US"]}, US | GB, {"203.0.113.1"}, "", id="a reserved country, no place"),
        pytest.param({}, US | GB, {"198.51.100.1", "198.51.100.3"}, "US", id="mixed countries, from one"),
        pytest.param({}, US | GB, {"198.51.100.1", "198.51.100.2", "203.0.113.2"}, "GB", id="mixed countries, from the other"),
        pytest.param({}, US | GB, {"198.51.100.1", "203.0.113.2"}, "DE", id="mixed countries, from neither"),
        pytest.param({}, US | GB, {"198.51.100.1"}, "US/NY", id="a state"),
        pytest.param({}, US | GB, set(US) - {"198.51.100.4"}, "US/CA", id="a state with one row usable"),
        pytest.param({}, US | GB, {"198.51.100.2"}, "US/NY/new_york", id="a city"),
        pytest.param({}, US | GB, {"198.51.100.1"}, "US//new_york", id="a city with no state named"),
        pytest.param({}, US, set(US), "US", id="every row avoided"),
    ],
)
async def test_the_effective_weights_are_those_routing_hands_its_strategy(
    pool_routing: dict[str, Any], rows: dict[str, dict[str, str]], avoided: set[str], place: str
) -> None:
    feed = _feed(pool_routing, rows, ExitReputationMode.AVOID)
    until = utc_now() + timedelta(hours=1)
    for ip in avoided:
        feed.avoid("p", ip, SITE, until)

    split = _split(feed, avoided)
    shares = split.shares(SITE, place, ["pool", "dyn"])

    taking = {connector_id: share.effective_weight for connector_id, share in shares.items() if share.effective_weight}
    assert taking == pytest.approx(await _routed(feed.router(), place))


async def test_where_the_project_records_the_weights_are_the_configured_ones_of_the_connectors_serving_the_request() -> None:
    feed = _feed({"reserved_countries": ["US"]}, US | GB, ExitReputationMode.RECORD)

    shares = _split(feed, {"198.51.100.1"}).shares(SITE, "US", ["pool", "dyn"])

    # The reservation keeps the dynamic connector off; the pool's reputation is shown for what Avoid would make of it.
    assert {connector_id: share.effective_weight for connector_id, share in shares.items()} == {"pool": 1.0, "dyn": 0.0}
    assert (shares["pool"].reputation.usable_ips, shares["pool"].reputation.eligible_ips) == (3, 4)
    assert shares["dyn"].reputation.value == 0.5


async def test_one_split_serves_every_place_as_a_split_per_place_would() -> None:
    feed = _feed({"reserved_countries": ["GB"]}, US | GB | UNPLACED, ExitReputationMode.AVOID)
    avoided = {"198.51.100.1", "203.0.113.2", "192.0.2.3"}
    places = ["", "US", "US/NY", "US/NY/new_york", "US//new_york", "US/CA", "GB", "DE"]

    records = _records()
    one = _split(feed, avoided, records)

    for place in places:
        alone = _split(feed, avoided, records).shares(SITE, place, ["pool", "dyn"])
        assert one.shares(SITE, place, ["pool", "dyn"]) == alone, place


# The dynamic connector's records besides FACTORS: a factor for California, and reports from France too few to judge.
JUDGED = [*_records(), _record("dyn", "US/CA", 0.7), _record("dyn", "FR", None)]


@pytest.mark.parametrize(
    ("pool_routing", "connector_id", "place", "state", "value", "level"),
    [
        pytest.param({}, "dyn", "US", PlaceState.OWN, 0.5, "US", id="dynamic, own"),
        pytest.param({}, "dyn", "US/NY/new_york", PlaceState.OWN, 0.3, "US/NY/new_york", id="dynamic, own city"),
        pytest.param({}, "dyn", "US/NY", PlaceState.INHERITED, 0.5, "US", id="dynamic, a state from its country"),
        pytest.param({}, "dyn", "US/CA/los_angeles", PlaceState.INHERITED, 0.7, "US/CA", id="dynamic, a city from its state"),
        pytest.param({}, "dyn", "FR", PlaceState.NOT_JUDGED, 1.0, None, id="dynamic, not judged"),
        pytest.param({}, "dyn", "FR//paris", PlaceState.NOT_JUDGED, 1.0, None, id="dynamic, a city not judged"),
        pytest.param({}, "dyn", "DE", PlaceState.NO_REPORTS, 1.0, None, id="dynamic, no reports"),
        pytest.param({"reserved_countries": ["US"]}, "dyn", "US", PlaceState.TAKES_NONE, 0.5, "US", id="dynamic, reserved off"),
        pytest.param({}, "pool", "US", PlaceState.OWN, 0.75, None, id="IPs, a row avoided"),
        pytest.param({}, "pool", "US/NY/new_york", PlaceState.OWN, 0.0, None, id="IPs, the city's one row avoided"),
        pytest.param({}, "pool", "GB", PlaceState.NO_REPORTS, 1.0, None, id="IPs, nothing reported"),
        pytest.param({}, "pool", "DE", PlaceState.TAKES_NONE, 1.0, None, id="IPs, no row there"),
    ],
)
def test_where_a_connector_stands_at_a_place(
    pool_routing: dict[str, Any], connector_id: str, place: str, state: PlaceState, value: float, level: str | None
) -> None:
    split = _split(_feed(pool_routing, US | GB, ExitReputationMode.AVOID), {"198.51.100.1"}, JUDGED)

    share = split.shares(SITE, place, ["pool", "dyn"])[connector_id]

    assert (share.state, share.reputation.value, share.reputation.place) == (state, value, level)


def test_a_connector_with_ips_is_measured_wherever_the_site_has_a_report_through_it() -> None:
    split = _split(_feed({}, US | GB, ExitReputationMode.AVOID), set(), [*JUDGED, _record("pool", "", None, reports=3)])

    share = split.shares(SITE, "GB", ["pool", "dyn"])["pool"]

    assert (share.state, share.reputation.value, share.own_factor) == (PlaceState.OWN, 1.0, None)
    assert share.own is not None and share.own.blocked_reports == 3


def test_a_dynamic_connector_counts_the_place_s_own_record() -> None:
    thin = _record("dyn", "US/NY", None, reports=2)
    split = _split(_feed({}, US | GB, ExitReputationMode.AVOID), set(), [*JUDGED, thin])

    shares = split.shares(SITE, "US/NY", ["dyn"])

    # The place's own thin record, while the value comes from the country.
    assert (shares["dyn"].state, shares["dyn"].own, shares["dyn"].own_factor) == (PlaceState.INHERITED, thin, None)
    assert split.shares(SITE, "US", ["dyn"])["dyn"].own_factor == 0.5


@pytest.mark.parametrize(
    ("mode", "effective"),
    [
        (ExitReputationMode.OFF, {"pool": 25.0, "dyn": 75.0}),
        (ExitReputationMode.RECORD, {"pool": 25.0, "dyn": 75.0}),
        (ExitReputationMode.AVOID, {"pool": 21.74, "dyn": 78.26}),
    ],
)
def test_configured_effective_and_avoid_shares(mode: ExitReputationMode, effective: dict[str, float]) -> None:
    # One of the pool's four British rows avoided: x0.75 at weight 1, beside the dynamic connector's x0.9 at weight 3.
    shares = _split(_feed({}, US | GB, mode), {"203.0.113.1"}).shares(SITE, "GB", ["pool", "dyn"])

    def rounded(field: str) -> dict[str, float]:
        return {connector_id: round(getattr(share, field), 2) for connector_id, share in shares.items()}

    assert rounded("configured_share") == {"pool": 25.0, "dyn": 75.0}
    assert rounded("avoid_share") == {"pool": 21.74, "dyn": 78.26}
    assert rounded("effective_share") == effective


# A second dynamic connector's factors for the site: better than the first's in the United States and New York City.
STRONGER = {"US": 0.95, "US/NY/new_york": 0.9, "GB": 0.6}


def _two_dynamic(strength: int, pool_routing: dict[str, Any] | None = None) -> tuple[FeedBuilder, list[ConnectorSiteReputation]]:
    """``_feed``'s project at ``strength`` with a second Oxylabs dynamic connector ``dyn2`` (weight 3, ``STRONGER``),
    and every dynamic connector's records."""
    feed = _feed(pool_routing or {}, US | GB, ExitReputationMode.AVOID)
    feed.projects["p"] = feed.projects["p"].model_copy(update={"exit_reputation_strength": strength})
    feed.add(
        Connector(
            id="dyn2", name="Dyn2", credential_id="oxy", credential_type="oxylabs", project_id="p",
            config={"session_mode": "dynamic", "session_duration_minutes": 10}, routing_config={"weight": 3},
        ),
        healthy(Proxy(
            id="gw2", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
            username="customer-alice-sessid-probe1probe1-sesstime-10", password="{password}", connector_id="dyn2",
            metadata={"provider": "oxylabs", "proxy_type": "residential", "dynamic_sessions": "true"},
        )),
    )
    for place, factor in STRONGER.items():
        feed.factor("p", "dyn2", SITE, place, factor)
    return feed, [*_records(), *(_record("dyn2", place, factor) for place, factor in STRONGER.items())]


@pytest.mark.parametrize("strength", [1, 10])
@pytest.mark.parametrize("place", ["", "US", "US/NY", "US/NY/new_york", "GB", "DE"])
async def test_at_any_strength_the_effective_weights_are_those_routing_hands_its_strategy(strength: int, place: str) -> None:
    feed, records = _two_dynamic(strength)
    feed.avoid("p", "198.51.100.1", SITE, utc_now() + timedelta(hours=1))

    shares = _split(feed, {"198.51.100.1"}, records).shares(SITE, place, ["pool", "dyn", "dyn2"])

    taking = {connector_id: share.effective_weight for connector_id, share in shares.items() if share.effective_weight}
    assert taking == pytest.approx(await _routed(feed.router(), place))


def test_the_best_is_found_among_every_connector_taking_the_requests_not_only_those_asked_about() -> None:
    feed, records = _two_dynamic(10)
    split = _split(feed, set(), records)

    alone = split.shares(SITE, "US", ["dyn"])["dyn"].reputation

    assert (alone.success, alone.best, alone.best_connector_id) == (0.5, 0.95, "dyn2")
    assert alone.value == pytest.approx(max(0.1, 0.5 * (0.5 / 0.95) ** 9))
    assert alone == split.shares(SITE, "US", ["pool", "dyn", "dyn2"])["dyn"].reputation


def test_a_connector_routing_keeps_off_is_shown_beside_those_taking_the_requests() -> None:
    # The pool reserves the United States: neither dynamic connector takes its requests.
    feed, records = _two_dynamic(10, {"reserved_countries": ["US"]})

    shares = _split(feed, set(), records).shares(SITE, "US", ["pool", "dyn", "dyn2"])

    assert (shares["dyn"].state, shares["dyn2"].state) == (PlaceState.TAKES_NONE, PlaceState.TAKES_NONE)
    # Each stands as it would beside the pool alone: its own best.
    assert (shares["dyn"].reputation.value, shares["dyn"].reputation.best_connector_id) == (0.5, "dyn")
    assert (shares["dyn2"].reputation.value, shares["dyn2"].reputation.best_connector_id) == (0.95, "dyn2")


def test_a_split_under_a_draft_of_the_settings_shares_the_records() -> None:
    feed, records = _two_dynamic(1)
    split = _split(feed, set(), records)
    project = split._project

    draft = split.under(project.model_copy(update={"exit_reputation_strength": 10, "exit_reputation_floor": 20}))

    assert split.shares(SITE, "US", ["dyn"])["dyn"].reputation.value == 0.5
    assert draft.shares(SITE, "US", ["dyn"])["dyn"].reputation.value == 0.2
    assert (draft.rule.strength, draft.rule.floor, split.rule.strength) == (10, 0.2, 1)


def test_a_draft_at_another_strength_decides_each_factor_again_on_its_grid() -> None:
    """Blocked 2% and 1.3%, both connectors store 1 at strength 1; at 20 they tell apart, as routing's next flushes
    would tell them."""
    feed, _ = _two_dynamic(1)
    records = [_record("dyn", "US", 1.0, block_rate=0.02), _record("dyn2", "US", 1.0, block_rate=0.013)]
    split = _split(feed, set(), records)

    draft = split.under(split._project.model_copy(update={"exit_reputation_strength": 20}))

    assert split.shares(SITE, "US", ["dyn"])["dyn"].reputation.value == 1.0
    compared = draft.shares(SITE, "US", ["dyn"])["dyn"].reputation
    # Steps of 0.0025 at strength 20: 0.987 is stored as 0.9875.
    assert (compared.success, compared.best, compared.best_connector_id) == (0.98, 0.9875, "dyn2")
    assert compared.value == pytest.approx(0.98 * (0.98 / 0.9875) ** 19)


@pytest.mark.parametrize(
    ("place", "label"),
    [
        ("", "No place named"),
        ("US", "United States"),
        ("VN", "Vietnam"),
        ("ZZ", "ZZ"),
        ("US/NY", "New York, United States"),
        ("GB/ENG", "ENG, United Kingdom"),
        ("US/NY/new_york", "New York, NY"),
        ("GB//london", "London, United Kingdom"),
        ("US//salt_lake_city", "Salt Lake City, United States"),
        ("KR//seoul", "Seoul, South Korea"),
    ],
)
def test_how_a_place_reads(place: str, label: str) -> None:
    assert place_label(place) == label


class TestSitePlaces:
    """One site across every place: the rows, their order, the tiles and the hardest hit."""

    def _places(self, records: list[ConnectorSiteReputation], avoided: set[str], rows: dict[str, dict[str, str]] | None = None) -> SitePlaces:
        feed = _feed({}, US | GB if rows is None else rows, ExitReputationMode.RECORD)
        return SitePlaces(_split(feed, avoided, records), SITE, ["dyn", "pool"])

    def test_the_rows_in_tree_order_the_most_reported_first(self) -> None:
        records = [
            _record("dyn", "GB", 0.9, reports=40),
            _record("dyn", "US", 0.5, reports=30),
            _record("dyn", "US//austin", None, reports=4),
            _record("dyn", "US/NY", 0.6, reports=26),
            _record("dyn", "US/NY/new_york", 0.3, reports=20),
            _record("dyn", "US/NY/buffalo", None, reports=6),
            _record("dyn", "DE", None, reports=30),
            # Every report through a connector with IPs is kept at no place: never a row of its own.
            _record("pool", "", None, reports=99),
        ]

        places = self._places(records, set())

        assert [(row.place, row.level, row.parent, row.reports) for row in places.rows] == [
            ("GB", PlaceLevel.COUNTRY, None, 40),
            # Equal reports: by key.
            ("DE", PlaceLevel.COUNTRY, None, 30),
            ("US", PlaceLevel.COUNTRY, None, 30),
            ("US/NY", PlaceLevel.STATE, "US", 26),
            ("US/NY/new_york", PlaceLevel.CITY, "US/NY", 20),
            ("US/NY/buffalo", PlaceLevel.CITY, "US/NY", 6),
            ("US//austin", PlaceLevel.CITY, "US", 4),
        ]
        # Only a country with listed cities is counted.
        assert places.cities == {"US": (1, 3)}
        rows = {row.place: row for row in places.rows}
        assert (rows["US/NY/buffalo"].lowest, rows["US/NY/buffalo"].lowered) == ((0.6, True), False)
        assert (rows["DE"].not_judged, rows["DE"].lowest) == (True, (1.0, False))

    def test_no_place_named_is_a_row_once_a_dynamic_connector_has_a_record_there(self) -> None:
        records = [_record("dyn", "US", 0.5, reports=3), _record("pool", "", None, reports=9)]
        assert [row.place for row in self._places(records, set()).rows] == ["US"]

        records.append(_record("dyn", "", 0.8, reports=2))
        rows = self._places(records, set()).rows
        # Its reports are the dynamic connector's alone.
        assert [(row.place, row.level, row.reports) for row in rows] == [("", PlaceLevel.NONE, 2), ("US", PlaceLevel.COUNTRY, 3)]

    def test_a_country_where_a_connector_with_ips_has_an_avoided_row_is_a_row(self) -> None:
        places = self._places([_record("dyn", "US", 0.5, reports=3)], {"203.0.113.1"})

        rows = {row.place: row for row in places.rows}
        assert list(rows) == ["US", "GB"]
        assert (rows["GB"].reports, rows["GB"].shares["pool"].state, rows["GB"].shares["pool"].reputation.value) == (
            0, PlaceState.OWN, 0.75
        )

    def test_tiles_count_own_values_and_the_hardest_hit_skips_a_thin_city_of_a_connector_with_ips(self) -> None:
        records = [
            _record("dyn", "US", 0.5, reports=30),
            _record("dyn", "US/NY", None, reports=26),
            _record("dyn", "US/NY/new_york", 0.3, reports=20),
            _record("dyn", "US/NY/buffalo", None, reports=6),
            _record("dyn", "GB", 1.0, reports=40),
        ]
        # The pool's New York City row avoided: x0 there, the thinnest of places.
        places = self._places(records, {"198.51.100.1"})

        dyn, pool = places.tile("dyn"), places.tile("pool")
        assert (dyn.lowered_countries, dyn.countries, dyn.lowered_cities, dyn.cities) == (1, 2, 1, 1)
        # US x0.5, New York City x0.3; New York state and Buffalo inherit; GB x1.
        assert (dyn.heavy, dyn.lowered, dyn.inherited, dyn.full, dyn.not_judged) == (1, 1, 2, 1, 0)
        assert (pool.lowered_countries, pool.countries, pool.lowered_states, pool.states, pool.lowered_cities, pool.cities) == (
            1, 1, 1, 1, 1, 1
        )
        # The pool takes nothing from Buffalo.
        assert (pool.heavy, pool.lowered, pool.full) == (1, 2, 1)
        assert places.hardest == ("dyn", "US/NY/new_york", 0.3)
        assert (places.lowered, places.serving) == (["dyn", "pool"], ["dyn", "pool"])

    def test_the_hardest_hit_counts_a_city_where_a_connector_with_ips_has_more_than_two_rows(self) -> None:
        records = [
            _record("dyn", "US", 0.5, reports=30),
            _record("dyn", "US/NY", None, reports=26),
            _record("dyn", "US/NY/new_york", 0.3, reports=20),
        ]
        # Three rows in New York City, one elsewhere in New York state and one in California.
        rows = {
            f"198.51.100.{n}": {"country": "US", "state_code": "CA" if n == 5 else "NY", **({"city": "new_york"} if n <= 3 else {})}
            for n in range(1, 6)
        }
        # Every one of the pool's New York City rows avoided: x0 there, on three rows.
        places = self._places(records, {f"198.51.100.{n}" for n in range(1, 4)}, rows)

        assert places.hardest == ("pool", "US/NY/new_york", 0.0)

    def test_the_hardest_hit_names_the_finer_place_on_a_tie_then_the_more_reported(self) -> None:
        records = [
            _record("dyn", "US", 0.3, reports=40),
            _record("dyn", "US/NY", 0.3, reports=30),
            _record("dyn", "US/NY/new_york", 0.3, reports=5),
            _record("dyn", "US/NY/buffalo", 0.3, reports=8),
        ]

        # Equal values: a city before its state and its country, Buffalo the more reported of the two.
        assert self._places(records, set()).hardest == ("dyn", "US/NY/buffalo", 0.3)
