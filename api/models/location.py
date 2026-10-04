# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Location targets: the place a request asks to exit from, below the country.

A client names the target in the proxy username, ``-cc-us-st-ny-city-new_york``.
The country is an ISO 3166-1 alpha-2 code, the state the subdivision part of
an ISO 3166-2 code and the city a slug: lower case, spaces as underscores.
"State" is the word vendors and clients use; the level is the country's
first-level subdivision whatever it is called locally: a US state (``NY`` in
``US-NY``), a Canadian province (``ON``), a German Land (``BY``), a French
region (``IDF``), England (``ENG``). Vendors want other spellings (a
state as ``us_new_york``, a city without the underscore), which descriptor
templates derive from these canonical forms; the IP databases report the
same codes and names, which is what verification compares against.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# ISO 3166-2:US subdivision codes to the state's name as a slug. Two uses:
# five of the six shipped vendors want a US state spelled as its name
# (``us_new_york``), and IP databases that report the subdivision's name but
# not its code (IP2Location, IPinfo) are mapped back to the code with it.
US_STATES: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district_of_columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new_hampshire", "NJ": "new_jersey", "NM": "new_mexico", "NY": "new_york",
    "NC": "north_carolina", "ND": "north_dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode_island", "SC": "south_carolina", "SD": "south_dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west_virginia", "WI": "wisconsin", "WY": "wyoming",
}
_US_STATE_BY_SLUG: dict[str, str] = {slug: code for code, slug in US_STATES.items()}

# Common non-ISO spellings accepted anywhere a country code is entered, mapped
# to the ISO 3166-1 alpha-2 code the vendors, the IP databases and the map use.
# A connector allow-list saying UK would otherwise never match a -cc-gb request.
COUNTRY_ALIASES: dict[str, str] = {"UK": "GB"}


def normalize_country_code(value: str | None) -> str | None:
    """Normalise a country code to upper-case ISO 3166-1 alpha-2, or None if blank.

    Applies ``COUNTRY_ALIASES`` (UK becomes GB). Raises ValueError for values
    that cannot be a country code (anything other than two ASCII letters).
    """
    if value is None:
        return None
    code = value.strip().upper()
    if not code:
        return None
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValueError(f'country must be a two-letter ISO 3166-1 alpha-2 code, got {value!r}')
    return COUNTRY_ALIASES.get(code, code)


def normalize_country_list(value: Any) -> list[str]:
    """Normalise a countries value (list, or comma-separated string) to unique upper-case codes.

    Order is preserved; blanks are dropped. Raises ValueError on a malformed code.
    """
    if value is None:
        return []
    raw: list[Any] = value.split(",") if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in raw:
        code = normalize_country_code(str(item)) if item is not None else None
        if code and code not in result:
            result.append(code)
    return result

_STATE_CODE = re.compile(r"^[A-Z0-9]{1,3}$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# A qualifier in parentheses names a part of the place, not the place: DB-IP's
# city database labels sub-localities "Sofia (g.k. Banishora)", "London (Soho)",
# "Frankfurt am Main (Innenstadt I)" and so on, tens of spellings per city.
_PARENTHESISED = re.compile(r"\([^)]*\)")

# Slugs that name the same place as another slug. IP databases and vendors
# spell a few cities differently ("City of London" for London's centre, "New
# York City"), which would otherwise contradict each other on the city level.
# Applied inside ``slugify_place`` so every side gets the canonical slug.
CITY_ALIASES: dict[str, str] = {
    "city_of_london": "london",
    "new_york_city": "new_york",
    "nyc": "new_york",
    "washington_d_c": "washington",
    "washington_dc": "washington",
    "st_louis": "saint_louis",
    "st_paul": "saint_paul",
    "st_petersburg": "saint_petersburg",
}
# Longest slug a place can be: the width of the claimed_city / resolved_city
# columns on observations and exit IPs, so a client-typed -city- value or an
# odd database record can never fail the observation batch that carries it.
MAX_PLACE_SLUG_LENGTH = 120

# Why a place a client named cannot be honoured. Shared by every surface that
# takes client input (proxy usernames, WireGuard peers) so the wording is one.
LOCATION_NEEDS_COUNTRY = "State and city targeting need a country: add -cc-<code> to the username"
LOCATION_INVALID_COUNTRY = "Country must be a two-letter ISO 3166-1 code, such as -cc-us"
LOCATION_INVALID_STATE = "State must be the subdivision part of an ISO 3166-2 code, such as -st-ny or -st-eng"
LOCATION_INVALID_CITY = "City must be a name with letters or digits, such as -city-new_york"

# Proxy metadata key: the state code and city an operator pinned by hand on
# a static proxy, as {"state_code": "NY", "city": "new_york"}. Routing
# prefers it to the databases' record and attribution verifies against it.
META_MANUAL_LOCATION = "manual_location"


def set_manual_location(metadata: dict[str, Any], *, state: str | None, city: str | None) -> None:
    """Pin, change or clear the hand-set state code and city in a proxy's metadata.

    ``None`` leaves a value as it is, an empty string clears it; the key is
    dropped once nothing is pinned. Values are stored in canonical form.
    """
    current = metadata.get(META_MANUAL_LOCATION)
    pinned: dict[str, str] = dict(current) if isinstance(current, dict) else {}
    for key, value in (("state_code", state), ("city", city)):
        if value is None:
            continue
        if value == "":
            pinned.pop(key, None)
        else:
            pinned[key] = value
    if pinned:
        metadata[META_MANUAL_LOCATION] = pinned
    else:
        metadata.pop(META_MANUAL_LOCATION, None)


def slugify_place(value: Any) -> str | None:
    """Canonical slug of a place name: ASCII lower case, runs of anything else as one underscore.

    ``"Los Angeles"``, ``"los-angeles"`` and ``"LOS_ANGELES"`` all become
    ``los_angeles``; ``"São Paulo"`` becomes ``sao_paulo``. A qualifier in
    parentheses is dropped, so a database's ``"Sofia (g.k. Banishora)"`` and
    a request for ``sofia`` are the same city (the few cities whose name
    itself carries one, ``"Frankfurt (Oder)"``, lose it too and fall together
    with their namesake; the state verdict still tells them apart). Spellings
    in ``CITY_ALIASES`` collapse to their canonical slug (``"City of London"``
    becomes ``london``). None for blanks. Cut to ``MAX_PLACE_SLUG_LENGTH`` so
    it always fits the columns that store it.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKD", _PARENTHESISED.sub(" ", value)).encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_ALNUM.sub("_", text).strip("_")[:MAX_PLACE_SLUG_LENGTH].rstrip("_")
    return CITY_ALIASES.get(slug, slug) or None


def normalize_level(level: str, value: Any) -> str | None:
    """Canonical form of one level of a place a client typed, or None for a blank.

    ``country`` is an ISO 3166-1 alpha-2 code, ``state`` the subdivision part
    of an ISO 3166-2 code, ``city`` a slug. Raises ValueError with the message
    the client should read when the value cannot be one.
    """
    text = value.strip() if isinstance(value, str) else value
    if text is None or text == "":
        return None
    if level == "country":
        try:
            return normalize_country_code(text)
        except ValueError:
            raise ValueError(LOCATION_INVALID_COUNTRY) from None
    if level == "state":
        try:
            return normalize_state_code(text)
        except ValueError:
            raise ValueError(LOCATION_INVALID_STATE) from None
    if level == "city":
        slug = slugify_place(text)
        if slug is None:
            raise ValueError(LOCATION_INVALID_CITY)
        return slug
    raise ValueError(f"unknown location level {level!r}")


def normalize_state_code(value: Any) -> str | None:
    """Upper-case ISO 3166-2 subdivision part (1 to 3 letters or digits), or None for blanks.

    A full code with the country prefix (``US-NY``) is accepted and reduced
    to its subdivision part. Raises ValueError for anything else.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if "-" in text:
        text = text.rsplit("-", 1)[1]
    if not _STATE_CODE.match(text):
        raise ValueError(f"state must be an ISO 3166-2 subdivision code such as NY or ENG, got {value!r}")
    return text


def state_name_slug(country: str | None, state: str | None) -> str | None:
    """The state's name as a slug (``new_york``) when the code is known, else None.

    Only US states have names on record; a vendor that wants the name cannot
    be sent any other subdivision, and its connector is skipped for it.
    """
    if country != "US" or not state:
        return None
    return US_STATES.get(state)


def us_state_code_from_name(country: str | None, name: str | None) -> str | None:
    """Code for a US state name an IP database reported without its code (``California`` becomes ``CA``).

    MaxMind and DB-IP records carry the subdivision's ISO code; IP2Location
    and IPinfo records carry only its name. Without this, exits attributed
    by those databases would have no state to match ``-st-`` on.
    """
    if country != "US" or not name:
        return None
    slug = slugify_place(name)
    return _US_STATE_BY_SLUG.get(slug) if slug else None


@dataclass(frozen=True)
class LocationTarget:
    """Where a request must exit: a country, optionally narrowed to a state and a city.

    Every field is in canonical form: the username parser normalises client
    input, :meth:`reported` what vendors and endpoints say.
    """

    country: str | None = None
    state: str | None = None
    city: str | None = None

    @classmethod
    def reported(cls, country: Any = None, state: Any = None, city: Any = None) -> LocationTarget | None:
        """A place as a vendor or an endpoint reported it, normalised leniently.

        Unlike :meth:`parse` nothing raises: a value that cannot be read is
        dropped. A state may come as its code (``NY``, ``US-NY``) or, for the
        US, as its name (``California``); a city in any spelling. None when
        nothing usable was reported.
        """
        code: str | None
        try:
            code = normalize_country_code(country) if isinstance(country, str) else None
        except ValueError:
            code = None
        state_code: str | None = None
        if isinstance(state, str) and state.strip():
            try:
                state_code = normalize_state_code(state)
            except ValueError:
                state_code = us_state_code_from_name(code, state)
        return cls(country=code, state=state_code, city=slugify_place(city)) or None

    @classmethod
    def parse(cls, country: Any = None, state: Any = None, city: Any = None) -> LocationTarget | None:
        """A place as a client named it, normalised strictly.

        The counterpart of :meth:`reported`: a value that cannot be read, or
        a state or city without a country, raises ValueError with the message
        the client should see, because routing a request to whatever part of
        it did parse would be a silent widening. None when nothing was named.
        """
        target = cls(
            country=normalize_level("country", country),
            state=normalize_level("state", state),
            city=normalize_level("city", city),
        )
        if target.below_country and target.country is None:
            raise ValueError(LOCATION_NEEDS_COUNTRY)
        return target or None

    @property
    def is_empty(self) -> bool:
        return self.country is None and self.state is None and self.city is None

    def __bool__(self) -> bool:
        """A target that names nothing is falsy, so ``location or None`` drops it."""
        return not self.is_empty

    @property
    def below_country(self) -> bool:
        """Whether the request narrows within the country."""
        return self.state is not None or self.city is not None

    @property
    def levels(self) -> tuple[str, ...]:
        """The levels the target names, most general first."""
        return tuple(
            level for level, value in (("country", self.country), ("state", self.state), ("city", self.city)) if value
        )

    @property
    def state_name(self) -> str | None:
        """The state's name slug for vendors that want one, when known."""
        return state_name_slug(self.country, self.state)

    def with_country(self, country: str | None) -> LocationTarget:
        return LocationTarget(country=country, state=self.state, city=self.city)

    @property
    def key(self) -> str:
        """Stable text for cache keys and session seeds: ``US``, ``US/NY``, ``US/NY/new_york``, ``US//austin``."""
        parts = [self.country or ""]
        if self.state or self.city:
            parts.append(self.state or "")
        if self.city:
            parts.append(self.city)
        return "/".join(parts)

    def describe(self) -> str:
        """Readable form for messages: ``US, state NY, city new_york``."""
        parts: list[str] = []
        if self.country:
            parts.append(self.country)
        if self.state:
            parts.append(f"state {self.state}")
        if self.city:
            parts.append(f"city {self.city}")
        return ", ".join(parts) or "anywhere"

    def to_dict(self) -> dict[str, str]:
        return {level: getattr(self, level) for level in self.levels}

    @classmethod
    def from_dict(cls, data: Any) -> LocationTarget | None:
        if not isinstance(data, dict):
            return None
        target = cls(country=data.get("country") or None, state=data.get("state") or None, city=data.get("city") or None)
        return None if target.is_empty else target
