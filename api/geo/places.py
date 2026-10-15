# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""City coordinates from a GeoNames file, so a city claim can be judged by distance.

An IP database names the city it places an exit in, and a claim names the
city that was promised; the two often name neighbouring places. DB-IP puts
much of Greater São Paulo in Diadema, a separate municipality 15 km from the
centre, and some exits in São Paulo's own districts (Vila Olímpia). Compared
by name, every one of those contradicts a ``-city-sao_paulo`` claim. A
gazetteer gives the claimed city a point, so an answer within the project's
radius of it can confirm the claim instead (see ``api.geo.resolver``).

GeoNames publishes the files under CC BY 4.0 at
https://download.geonames.org/export/dump/: ``cities500.zip`` (every place
over 500 people and every administrative seat, about 236k places),
``cities1000``, ``cities5000``, ``cities15000``, and one file per country
(``BR.zip``). Every one is the same tab-separated ``geoname`` table, bare or
zipped; only populated places (feature class ``P``) are kept. Nothing is
shipped with Octoprox: an admin uploads a file or registers its URL like any
IP database.

The index is built in memory once per file and instance. cities500 takes
about 35 MB and a few seconds to parse; a lookup is one dict probe.
"""

from __future__ import annotations

import io
import math
import sys
import zipfile
from array import array
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO

from api.models.location import slugify_place

# A point as (latitude, longitude) in degrees.
Point = tuple[float, float]

# Where a claimed city is: (country code, state code or None, city slug) to a
# point, or None when no loaded file knows the place.
CityLocator = Callable[[str | None, str | None, str | None], Point | None]

GEONAMES_ATTRIBUTION = "City coordinates from GeoNames (https://www.geonames.org), licensed under CC BY 4.0."

# Populated places a file may hold. cities500 has about 236k; the guard is
# against allCountries (millions of places) filling every instance's memory.
MAX_PLACES = 1_500_000

ZIP_MAGIC = b"PK\x03\x04"

# What reading a damaged or unsupported file raises besides OSError: a corrupt
# zip, bytes that are not UTF-8, an encrypted member (RuntimeError), a
# compression method zipfile cannot read (NotImplementedError), a truncated
# member (EOFError).
_UNREADABLE = (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError, NotImplementedError, EOFError)

# Columns of the GeoNames ``geoname`` table that the index reads.
_NAME, _ASCII_NAME, _LATITUDE, _LONGITUDE, _FEATURE_CLASS = 1, 2, 4, 5, 6
_COUNTRY, _ADMIN1, _POPULATION, _MODIFIED = 8, 10, 14, 18
_MIN_COLUMNS = _POPULATION + 1

_EARTH_RADIUS_KM = 6371.0088


def distance_km(a: Point, b: Point) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


class PlaceFileError(ValueError):
    """The file is not a GeoNames table, or holds more places than an instance should load."""


def _is_place_row(fields: list[str]) -> bool:
    if len(fields) < _MIN_COLUMNS or not fields[0].isdigit():
        return False
    try:
        float(fields[_LATITUDE])
        float(fields[_LONGITUDE])
    except ValueError:
        return False
    return True


def looks_like_geonames(path: str | Path) -> bool:
    """Whether ``path`` is a GeoNames table, bare or zipped, judged by content."""
    try:
        with _open_table(Path(path)) as (_member, lines):
            for line in lines:
                if line.strip():
                    return _is_place_row(line.rstrip("\n").split("\t"))
    except (OSError, PlaceFileError, *_UNREADABLE):
        return False
    return False


@contextmanager
def _open_table(path: Path) -> Iterator[tuple[str, Iterable[str]]]:
    """The table's name and its lines: the largest ``.txt`` in a zip (not its readme), else the file itself."""
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic == ZIP_MAGIC:
        with zipfile.ZipFile(path) as archive:
            members = [
                info
                for info in archive.infolist()
                if info.filename.lower().endswith(".txt") and Path(info.filename).name.lower() != "readme.txt"
            ]
            if not members:
                raise PlaceFileError("the zip holds no GeoNames .txt table")
            member = max(members, key=lambda info: info.file_size)
            with archive.open(member) as raw:
                yield Path(member.filename).stem, _text(raw)
        return
    with path.open("rb") as raw:
        yield path.stem, _text(raw)


def _text(raw: IO[bytes]) -> Iterable[str]:
    return io.TextIOWrapper(raw, encoding="utf-8", newline="\n")


class PlaceIndex:
    """Populated places by country and city slug, with their coordinates.

    Compact on purpose: coordinates and populations live in typed arrays,
    and the dict maps ``"<country>:<slug>"`` to one index, or to a tuple of
    indexes (most populous first) when a country has several places of that
    name. Both the name and the ASCII name are keyed, through the same
    ``slugify_place`` every claim goes through.
    """

    def __init__(self) -> None:
        self._latitudes = array("f")
        self._longitudes = array("f")
        self._populations = array("q")
        self._admin1: list[str] = []
        self._keys: dict[str, int | tuple[int, ...]] = {}
        self.table = ""
        self.modified: datetime | None = None

    def __len__(self) -> int:
        return len(self._latitudes)

    @classmethod
    def from_file(cls, path: str | Path) -> PlaceIndex:
        """Parse a GeoNames table, bare or zipped. Raises PlaceFileError."""
        index = cls()
        try:
            with _open_table(Path(path)) as (table, lines):
                index.table = table
                index._load(lines)
        except _UNREADABLE as exc:
            raise PlaceFileError(f"not a readable GeoNames file: {exc}") from exc
        if not len(index):
            raise PlaceFileError("no populated places found: expected a GeoNames table such as cities500.txt")
        return index

    def _load(self, lines: Iterable[str]) -> None:
        intern = sys.intern
        latest = ""
        for line in lines:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < _MIN_COLUMNS or fields[_FEATURE_CLASS] != "P":
                continue
            if not _is_place_row(fields):
                if not len(self):
                    raise PlaceFileError("not a GeoNames table: expected tab-separated geoname rows")
                continue
            if len(self) >= MAX_PLACES:
                raise PlaceFileError(
                    f"more than {MAX_PLACES:,} populated places: load cities500 or a per-country file instead"
                )
            position = len(self._latitudes)
            self._latitudes.append(float(fields[_LATITUDE]))
            self._longitudes.append(float(fields[_LONGITUDE]))
            self._populations.append(int(fields[_POPULATION]) if fields[_POPULATION].isdigit() else 0)
            self._admin1.append(intern(fields[_ADMIN1].upper()))
            if len(fields) > _MODIFIED and fields[_MODIFIED] > latest:
                latest = fields[_MODIFIED]
            country = fields[_COUNTRY].upper()
            for slug in {slugify_place(fields[_NAME]), slugify_place(fields[_ASCII_NAME])}:
                if slug:
                    self._add(f"{country}:{slug}", position)
        for key, value in self._keys.items():
            if isinstance(value, tuple):
                self._keys[key] = tuple(sorted(value, key=lambda i: -self._populations[i]))
        try:
            self.modified = datetime.strptime(latest, "%Y-%m-%d") if latest else None
        except ValueError:
            self.modified = None

    def _add(self, key: str, position: int) -> None:
        current = self._keys.get(key)
        if current is None:
            self._keys[key] = position
        elif isinstance(current, int):
            self._keys[key] = (current, position)
        else:
            self._keys[key] = (*current, position)

    def locate(self, country: str | None, state: str | None, city: str | None) -> Point | None:
        """The point of ``city`` (a slug) in ``country``, or None when the file does not know it.

        Several places of one name in a country resolve to the one in
        ``state`` when the file's first-level code is the same (true for US
        states and the UK's nations; GeoNames numbers most other countries'
        subdivisions), else to the most populous.
        """
        if not country or not city:
            return None
        found = self._keys.get(f"{country}:{city}")
        if found is None:
            return None
        if isinstance(found, int):
            position = found
        else:
            position = next((i for i in found if state and self._admin1[i] == state), found[0])
        return float(self._latitudes[position]), float(self._longitudes[position])
