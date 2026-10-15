# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for GeoNames city coordinates: parsing, lookups, and loading through the database pipeline."""

import zipfile
from pathlib import Path

import pytest

from api.geo import places
from api.geo.models import GeoDatabaseFormat, GeoDatabaseKind, GeoVendor
from api.geo.places import PlaceFileError, PlaceIndex, distance_km, looks_like_geonames
from api.geo.readers import (
    GeoDatabaseError,
    GeoNamesReader,
    inspect_file,
    open_reader,
    sniff_format,
)
from api.geo.store import GeoDatabaseStore
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb


def _row(geoname_id: int, name: str, lat: float, lon: float, country: str, admin1: str, population: int, *,
         ascii_name: str | None = None, feature_class: str = "P", modified: str = "2025-01-01") -> str:
    """One line of the GeoNames ``geoname`` table (19 tab-separated columns)."""
    fields = [
        str(geoname_id), name, ascii_name or name, "", str(lat), str(lon), feature_class, "PPL", country, "",
        admin1, "", "", "", str(population), "", "0", "UTC", modified,
    ]
    return "\t".join(fields)


ROWS = [
    _row(3448439, "São Paulo", -23.5475, -46.63611, "BR", "27", 12400232, ascii_name="Sao Paulo"),
    _row(3464739, "Diadema", -23.68611, -46.62278, "BR", "27", 403579),
    _row(3467865, "Campinas", -22.90556, -47.06083, "BR", "27", 1031554),
    # The state of São Paulo itself: an administrative area, not a populated place.
    _row(3448433, "São Paulo", -22.0, -49.0, "BR", "27", 46649132, ascii_name="Sao Paulo", feature_class="A"),
    _row(2643743, "London", 51.50853, -0.12574, "GB", "ENG", 8961989, modified="2026-10-14"),
    _row(2652045, "Croydon", 51.38333, -0.09, "GB", "ENG", 173314),
    _row(5128581, "New York City", 40.71427, -74.00597, "US", "NY", 8804190),
    _row(4250542, "Springfield", 39.80172, -89.64371, "US", "IL", 116250),
    _row(4951788, "Springfield", 42.10148, -72.58981, "US", "MA", 155929),
    _row(4409896, "Springfield", 37.21533, -93.29824, "US", "MO", 169176),
]


def write_geonames(path: Path, rows: list[str] | None = None, *, zipped: bool = False) -> Path:
    """A GeoNames table as GeoNames publishes it: bare, or zipped with a readme beside it."""
    text = "\n".join(ROWS if rows is None else rows) + "\n"
    if not zipped:
        path.write_text(text, encoding="utf-8")
        return path
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "GeoNames readme\n" * 50)
        archive.writestr(f"{path.stem}.txt", text)
    return path


class TestPlaceIndex:
    def test_locates_places_by_name_and_ascii_name(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "cities500.txt"))
        assert index.locate("BR", None, "sao_paulo") == pytest.approx((-23.5475, -46.63611), abs=1e-4)
        assert index.locate("BR", None, "diadema") == pytest.approx((-23.68611, -46.62278), abs=1e-4)
        assert index.locate("BR", None, "rio_de_janeiro") is None
        assert index.locate("GB", None, "sao_paulo") is None  # keyed by country
        assert index.locate(None, None, "london") is None and index.locate("GB", None, None) is None

    def test_only_populated_places_are_kept(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "cities500.txt"))
        assert len(index) == len(ROWS) - 1
        # The state's centroid never stands in for the city of the same name.
        assert index.locate("BR", None, "sao_paulo") == pytest.approx((-23.5475, -46.63611), abs=1e-4)

    def test_names_fold_like_every_claim(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "cities500.txt"))
        # "New York City" is aliased to new_york, the slug a -city-new_york claim carries.
        assert index.locate("US", None, "new_york") == pytest.approx((40.71427, -74.00597), abs=1e-4)

    def test_namesakes_resolve_by_state_then_population(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "cities500.txt"))
        assert index.locate("US", "IL", "springfield") == pytest.approx((39.80172, -89.64371), abs=1e-4)
        assert index.locate("US", "MA", "springfield") == pytest.approx((42.10148, -72.58981), abs=1e-4)
        # No state, or one the file numbers differently: the most populous.
        assert index.locate("US", None, "springfield") == pytest.approx((37.21533, -93.29824), abs=1e-4)
        assert index.locate("US", "CA", "springfield") == pytest.approx((37.21533, -93.29824), abs=1e-4)

    def test_table_name_and_latest_modification(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "cities500.zip", zipped=True))
        assert index.table == "cities500"
        assert index.modified is not None and index.modified.date().isoformat() == "2026-10-14"

    def test_reads_the_table_inside_a_zip_not_its_readme(self, tmp_path: Path) -> None:
        index = PlaceIndex.from_file(write_geonames(tmp_path / "BR.zip", ROWS[:3], zipped=True))
        assert index.table == "BR" and len(index) == 3

    def test_rejects_a_file_that_is_not_a_geonames_table(self, tmp_path: Path) -> None:
        other = tmp_path / "notes.txt"
        other.write_text("ip,country\n1.2.3.4,GB\n", encoding="utf-8")
        with pytest.raises(PlaceFileError):
            PlaceIndex.from_file(other)

    def test_rejects_a_zip_without_a_table(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("readme.txt", "nothing here")
        with pytest.raises(PlaceFileError):
            PlaceIndex.from_file(archive_path)

    def test_refuses_more_places_than_an_instance_should_hold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(places, "MAX_PLACES", 3)
        with pytest.raises(PlaceFileError, match="more than 3"):
            PlaceIndex.from_file(write_geonames(tmp_path / "allCountries.txt"))


class TestDetection:
    def test_bare_and_zipped_tables_are_recognised(self, tmp_path: Path) -> None:
        assert looks_like_geonames(write_geonames(tmp_path / "cities500.txt"))
        assert looks_like_geonames(write_geonames(tmp_path / "cities500.zip", zipped=True))

    def test_other_files_are_not(self, tmp_path: Path) -> None:
        mmdb = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        assert not looks_like_geonames(mmdb)
        csv = tmp_path / "list.csv"
        csv.write_text("a,b,c\n", encoding="utf-8")
        assert not looks_like_geonames(csv)

    def test_sniffed_by_content_whatever_the_extension(self, tmp_path: Path) -> None:
        # A scheduled download is validated from a temporary .mmdb file.
        assert sniff_format(write_geonames(tmp_path / "download.mmdb", zipped=True)) == GeoDatabaseFormat.GEONAMES


class TestReader:
    def test_inspect_describes_the_file(self, tmp_path: Path) -> None:
        info = inspect_file(write_geonames(tmp_path / "cities500.zip", zipped=True))
        assert info.format == GeoDatabaseFormat.GEONAMES
        assert info.vendor == GeoVendor.GEONAMES and info.kind == GeoDatabaseKind.PLACES
        assert info.database_type == "GeoNames cities500" and info.record_count == len(ROWS) - 1
        assert "GeoNames" in info.attribution and info.sha256 and info.size_bytes > 0

    def test_answers_no_ip(self, tmp_path: Path) -> None:
        reader = open_reader(write_geonames(tmp_path / "cities500.txt"))
        assert isinstance(reader, GeoNamesReader)
        assert reader.lookup("81.2.69.160") is None
        assert reader.locate("GB", "ENG", "london") is not None

    def test_unreadable_file_is_a_database_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "cities500.txt"
        bad.write_text("not\ta\tgeonames\ttable\n", encoding="utf-8")
        with pytest.raises(GeoDatabaseError):
            GeoNamesReader(bad)


class TestStore:
    async def test_places_file_loads_beside_the_ip_databases(self, tmp_path: Path) -> None:
        mmdb = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        gazetteer = write_geonames(tmp_path / "cities500.zip", zipped=True)
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(mmdb), "priority": 10}, {"path": str(gazetteer), "priority": 5}])
        assert await store.sync_all() == 2
        kinds = {loaded.kind for loaded in store.loaded}
        assert kinds == {GeoDatabaseKind.CITY, GeoDatabaseKind.PLACES}
        # Only the IP database answers for an IP, whatever the places file's priority.
        candidates = store.lookup("81.2.69.160")
        assert len(candidates) == 1 and candidates[0].country == "GB"
        assert store.places_loaded
        assert store.locate_city("GB", "ENG", "croydon") == pytest.approx((51.38333, -0.09), abs=1e-4)
        assert store.locate_city("GB", "ENG", "atlantis") is None

    async def test_disabled_places_file_places_nothing(self, tmp_path: Path) -> None:
        gazetteer = write_geonames(tmp_path / "cities500.txt")
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(gazetteer), "enabled": False}])
        await store.sync_all()
        assert store.locate_city("BR", None, "sao_paulo") is None
        assert not store.places_loaded

    async def test_first_file_that_knows_the_city_answers(self, tmp_path: Path) -> None:
        brazil = write_geonames(tmp_path / "BR.txt", ROWS[:3])
        world = write_geonames(tmp_path / "cities15000.txt", [_row(1, "Diadema", 0.0, 0.0, "BR", "27", 1), *ROWS[4:]])
        store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(world), "priority": 20}, {"path": str(brazil), "priority": 10}])
        await store.sync_all()
        assert store.locate_city("BR", None, "diadema") == pytest.approx((-23.68611, -46.62278), abs=1e-4)
        assert store.locate_city("GB", None, "london") is not None  # only the second file knows it


def test_distance() -> None:
    sao_paulo, diadema, campinas = (-23.5475, -46.63611), (-23.68611, -46.62278), (-22.90556, -47.06083)
    assert distance_km(sao_paulo, sao_paulo) == 0
    assert distance_km(sao_paulo, diadema) == pytest.approx(15.5, abs=0.2)
    assert distance_km(sao_paulo, campinas) == pytest.approx(83.5, abs=1.0)
