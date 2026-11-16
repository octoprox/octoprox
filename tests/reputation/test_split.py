# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The split the api shows against routing: for a request to a site from a place, the effective weights of
ReputationSplit are the weights the router hands the project's strategy, whatever domain filters, reservations and
countries keep connectors or rows off the request.

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
from api.reputation.split import ReputationSplit, place_target
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.strategies import ProxyGroup
from tests.routing.builder import FeedBuilder, healthy

SITE = "www.google.com"
# The dynamic connector's stored factors for the site, by level of the requested place.
FACTORS = {"": 0.8, "US": 0.5, "GB": 0.9}


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


def _records() -> list[ConnectorSiteReputation]:
    """The dynamic connector's records the feed's factors were read from."""
    now = utc_now()
    records = []
    for place, factor in FACTORS.items():
        record = ConnectorSiteReputation.new(ConnectorSiteKey("p", "dyn", SITE, place), now)
        record.factor = factor
        records.append(record)
    return records


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


def _split(feed: FeedBuilder, place: str, avoided: set[str]) -> ReputationSplit:
    """The api's split for the site from ``place``: over the preview, the compile without the reputation section."""
    sections = {name: section for name, section in feed.sections().items() if name != REPUTATION_SECTION}
    preview = Router(RoutingSnapshot.from_sections(sections, registry=feed.registry))
    project = preview.get_project("p")
    assert project is not None
    return ReputationSplit(
        project, preview, place=place, avoided={SITE: frozenset(avoided)}, records=_records(), now=utc_now()
    )


US = {f"198.51.100.{n}": {"country": "US", "state_code": "NY" if n <= 2 else "CA"} for n in range(1, 5)}
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

    split = _split(feed, place, avoided)
    shares = split.shares(SITE, ["pool", "dyn"])

    taking = {connector_id: share.effective_weight for connector_id, share in shares.items() if share.effective_weight}
    assert taking == pytest.approx(await _routed(feed.router(), place))


async def test_where_the_project_records_the_weights_are_the_configured_ones_of_the_connectors_serving_the_request() -> None:
    feed = _feed({"reserved_countries": ["US"]}, US | GB, ExitReputationMode.RECORD)

    shares = _split(feed, "US", {"198.51.100.1"}).shares(SITE, ["pool", "dyn"])

    # The reservation keeps the dynamic connector off; the pool's reputation is shown for what Avoid would make of it.
    assert {connector_id: share.effective_weight for connector_id, share in shares.items()} == {"pool": 1.0, "dyn": 0.0}
    assert (shares["pool"].reputation.usable_ips, shares["pool"].reputation.eligible_ips) == (3, 4)
    assert shares["dyn"].reputation.value == 0.5
