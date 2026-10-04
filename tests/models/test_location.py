# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Location targets, place slugs, state codes and the hand-set place on a proxy."""

import pytest

from api.models.location import (
    META_MANUAL_LOCATION,
    LocationTarget,
    normalize_state_code,
    set_manual_location,
    slugify_place,
    state_name_slug,
    us_state_code_from_name,
)
from api.models.proxy import Proxy, ProxyCreate, ProxyUpdate


class TestSlugsAndCodes:
    def test_slugify_place(self) -> None:
        assert slugify_place("Los Angeles") == "los_angeles"
        assert slugify_place("  new-york ") == "new_york"
        assert slugify_place("SÃO PAULO") == "sao_paulo"
        assert slugify_place("Saint-Denis") == "saint_denis"
        assert slugify_place("") is None and slugify_place(None) is None and slugify_place("--") is None
        # Spellings of one place collapse to one slug, so a database and a vendor cannot contradict each other on it.
        assert slugify_place("City of London") == "london" and slugify_place("New York City") == "new_york"
        assert slugify_place("St. Louis") == "saint_louis" and slugify_place("Washington, D.C.") == "washington"
        # DB-IP names sub-localities in parentheses; the qualifier is not part of the city.
        assert slugify_place("Sofia (g.k. Banishora)") == "sofia" and slugify_place("Sofia (Old City Center)") == "sofia"
        assert slugify_place("London (City of London)") == "london" and slugify_place("Frankfurt am Main (Innenstadt I)") == "frankfurt_am_main"
        assert slugify_place("Frankfurt (Oder) (Güldendorf)") == "frankfurt" and slugify_place("(Oder)") is None

    def test_state_codes(self) -> None:
        assert normalize_state_code("ny") == "NY"
        assert normalize_state_code("US-NY") == "NY"
        assert normalize_state_code(" eng ") == "ENG"
        assert normalize_state_code("") is None and normalize_state_code(None) is None
        with pytest.raises(ValueError):
            normalize_state_code("newyork")

    def test_state_names_are_us_only(self) -> None:
        assert state_name_slug("US", "NY") == "new_york"
        assert state_name_slug("US", "DC") == "district_of_columbia"
        assert state_name_slug("US", "ZZ") is None
        assert state_name_slug("GB", "ENG") is None
        assert us_state_code_from_name("US", "California") == "CA"
        assert us_state_code_from_name("US", "new york") == "NY"
        assert us_state_code_from_name("DE", "Berlin") is None


class TestLocationTarget:
    def test_reported_normalises_leniently(self) -> None:
        target = LocationTarget.reported("us", "California", "New York")
        assert target == LocationTarget(country="US", state="CA", city="new_york")
        assert target.below_country and target.levels == ("country", "state", "city")
        assert target.key == "US/CA/new_york" and target.describe() == "US, state CA, city new_york"
        assert target.state_name == "california"
        assert LocationTarget.reported("gb", "US-NY", None) == LocationTarget(country="GB", state="NY")
        # Anything unreadable is dropped rather than raised; nothing usable is None.
        assert LocationTarget.reported("USA", "newyork", "") is None
        assert LocationTarget.reported(None, None, None) is None

    def test_empty_target_is_falsy(self) -> None:
        assert not LocationTarget() and LocationTarget().is_empty
        assert LocationTarget(country="US") and not LocationTarget(country="US").is_empty
        assert (LocationTarget() or None) is None

    def test_keys_and_dicts(self) -> None:
        assert LocationTarget(country="US").key == "US"
        assert LocationTarget(country="US", state="TX").key == "US/TX"
        assert LocationTarget(country="US", city="austin").key == "US//austin"
        assert LocationTarget(country="US", city="austin").to_dict() == {"country": "US", "city": "austin"}
        assert LocationTarget.from_dict({"country": "US", "state": "", "city": "austin"}) == LocationTarget(country="US", city="austin")
        assert LocationTarget.from_dict({}) is None and LocationTarget.from_dict("US") is None
        assert LocationTarget(country="US", state="TX").with_country("CA") == LocationTarget(country="CA", state="TX")


class TestProxyPlace:
    def test_place_from_databases(self) -> None:
        proxy = Proxy(host="h", port=1, connector_id="c", metadata={"location": {"state_code": "NY", "city": "New York"}})
        assert proxy.state_code == "NY" and proxy.city_slug == "new_york"
        assert proxy.state_source == "database" and proxy.city_source == "database"
        assert proxy.location_matches(LocationTarget(country="US", state="NY"))
        assert proxy.location_matches(LocationTarget(country="US", state="NY", city="new_york"))
        assert not proxy.location_matches(LocationTarget(country="US", state="CA"))
        assert not proxy.location_matches(LocationTarget(country="US", city="buffalo"))

    def test_unknown_place_never_matches_a_named_one(self) -> None:
        proxy = Proxy(host="h", port=1, connector_id="c", metadata={"location": {"country": "US"}})
        assert proxy.state_code is None and proxy.city_slug is None and proxy.state_source is None and proxy.city_source is None
        assert not proxy.location_matches(LocationTarget(country="US", state="NY"))
        assert proxy.location_matches(LocationTarget(country="US"))

    def test_manual_place_wins(self) -> None:
        metadata = {"location": {"state_code": "NY", "city": "New York"}}
        # Values arrive canonical: ProxyCreate and ProxyUpdate normalise them before they are pinned.
        set_manual_location(metadata, state="CA", city="los_angeles")
        proxy = Proxy(host="h", port=1, connector_id="c", metadata=metadata)
        assert proxy.state_code == "CA" and proxy.city_slug == "los_angeles"
        assert proxy.state_source == "manual" and proxy.city_source == "manual"
        set_manual_location(metadata, state="", city=None)
        assert Proxy(host="h", port=1, connector_id="c", metadata=metadata).state_code == "NY"
        assert metadata[META_MANUAL_LOCATION] == {"city": "los_angeles"}
        set_manual_location(metadata, state=None, city="")
        assert META_MANUAL_LOCATION not in metadata

    def test_resolved_place_wins_over_the_databases_record(self) -> None:
        # Attribution writes what the policy resolved next to the databases' record; routing reads the former.
        metadata = {
            "location": {"state_code": "NY", "city": "New York"},
            "state_code": "NJ", "state_source": "vendor", "city": "jersey_city", "city_source": "vendor",
        }
        proxy = Proxy(host="h", port=1, connector_id="c", metadata=metadata)
        assert (proxy.state_code, proxy.state_source) == ("NJ", "vendor")
        assert (proxy.city_slug, proxy.city_source) == ("jersey_city", "vendor")
        assert proxy.location_matches(LocationTarget(country="US", state="NJ", city="jersey_city"))

    def test_create_and_update_schemas_normalise(self) -> None:
        created = ProxyCreate(host="h", port=1, connector_id="c", state="ny", city="New York")
        assert created.state == "NY" and created.city == "new_york"
        assert ProxyUpdate(state="", city="").state == "" and ProxyUpdate(city="").city == ""
        assert ProxyUpdate(state="us-ca").state == "CA"
        with pytest.raises(ValueError):
            ProxyCreate(host="h", port=1, connector_id="c", state="california")


class TestParse:
    """Client input, strictly: what proxy usernames and WireGuard peers share."""

    def test_normalises_every_level(self) -> None:
        target = LocationTarget.parse(" us ", "ny", "New York")
        assert target == LocationTarget(country="US", state="NY", city="new_york")
        assert LocationTarget.parse("uk") == LocationTarget(country="GB")
        assert LocationTarget.parse(None, "", "  ") is None

    def test_refuses_unreadable_values_and_orphans(self) -> None:
        with pytest.raises(ValueError, match="two-letter"):
            LocationTarget.parse("usa")
        with pytest.raises(ValueError, match="ISO 3166-2"):
            LocationTarget.parse("us", "new york")
        with pytest.raises(ValueError, match="letters or digits"):
            LocationTarget.parse("us", None, "!!!")
        with pytest.raises(ValueError, match="need a country"):
            LocationTarget.parse(None, "ny")
