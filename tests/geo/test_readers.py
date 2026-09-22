# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the IP database readers and vendor layout normalisation."""

from pathlib import Path

import pytest
from mmdb_writer import MMDBWriter
from netaddr import IPSet

from api.geo.models import GeoDatabaseFormat, GeoDatabaseKind, GeoVendor
from api.geo.readers import (
    GeoDatabaseError,
    MmdbReader,
    detect_kind,
    detect_vendor,
    inspect_file,
    normalize_record,
    open_reader,
    sniff_format,
)


def write_mmdb(path: Path, database_type: str, networks: dict[str, dict], description: str = "test") -> Path:
    writer = MMDBWriter(ip_version=6, database_type=database_type, ipv4_compatible=True, description={"en": description})
    for network, record in networks.items():
        writer.insert_network(IPSet([network]), record)
    writer.to_db_file(str(path))
    return path


MAXMIND_RECORD = {
    "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
    "subdivisions": [{"iso_code": "ENG", "names": {"en": "England"}}],
    "city": {"names": {"en": "London"}},
    "location": {"latitude": 51.5, "longitude": -0.12},
    "postal": {"code": "EC1"},
    "traits": {"autonomous_system_number": 12345, "autonomous_system_organization": "Example Ltd"},
}

IPINFO_RECORD = {
    "country": "DE",
    "country_name": "Germany",
    "region": "Berlin",
    "city": "Berlin",
    "latitude": "52.52",
    "longitude": "13.40",
    "asn": "AS3320",
    "as_name": "Deutsche Telekom",
    "postal": "10115",
}

IP2LOCATION_RECORD = {
    "country_code": "US",
    "country_name": "United States",
    "region_name": "California",
    "city_name": "Los Angeles",
    "latitude": 34.05,
    "longitude": -118.24,
    "zip_code": "90001",
}

ANONYMOUS_RECORD = {"is_anonymous": True, "is_anonymous_vpn": True, "is_hosting_provider": False}
IPINFO_PRIVACY_RECORD = {"hosting": "true", "proxy": "false", "tor": "false", "vpn": "false", "relay": "false"}


class TestNormalizeRecord:
    def test_maxmind_layout(self) -> None:
        location = normalize_record(MAXMIND_RECORD)
        assert location is not None
        assert location.country == "GB"
        assert location.region == "England"
        assert location.city == "London"
        assert location.postal_code == "EC1"
        assert location.latitude == 51.5 and location.longitude == -0.12
        assert location.asn == 12345 and location.organization == "Example Ltd"

    def test_ipinfo_layout(self) -> None:
        location = normalize_record(IPINFO_RECORD)
        assert location is not None
        assert location.country == "DE"
        assert location.region == "Berlin" and location.city == "Berlin"
        assert location.latitude == 52.52
        assert location.asn == 3320 and location.organization == "Deutsche Telekom"
        assert location.postal_code == "10115"

    def test_ip2location_layout(self) -> None:
        location = normalize_record(IP2LOCATION_RECORD)
        assert location is not None
        assert (location.country, location.region, location.city) == ("US", "California", "Los Angeles")
        assert location.postal_code == "90001"

    def test_anonymous_flags(self) -> None:
        location = normalize_record(ANONYMOUS_RECORD)
        assert location is not None
        assert location.is_anonymous is True and location.is_vpn is True and location.is_hosting is False
        assert location.country is None

    def test_ipinfo_privacy_strings(self) -> None:
        location = normalize_record(IPINFO_PRIVACY_RECORD)
        assert location is not None
        assert location.is_hosting is True and location.is_vpn is False
        # No explicit is_anonymous but a positive flag implies it
        assert location.is_anonymous is None or location.is_anonymous is True

    def test_empty_and_garbage(self) -> None:
        assert normalize_record(None) is None
        assert normalize_record({}) is None
        assert normalize_record({"country": {"iso_code": "usa"}}) is None
        assert normalize_record({"unrelated": 1}) is None

    def test_country_only(self) -> None:
        location = normalize_record({"country": {"iso_code": "fr"}})
        assert location is not None and location.country == "FR"


class TestDetection:
    def test_vendor(self) -> None:
        assert detect_vendor("GeoLite2-City") == GeoVendor.MAXMIND
        assert detect_vendor("GeoIP2-Country") == GeoVendor.MAXMIND
        assert detect_vendor("DBIP-City-Lite") == GeoVendor.DBIP
        assert detect_vendor("ipinfo standard_location.mmdb") == GeoVendor.IPINFO
        assert detect_vendor("IP2Location-DB9") == GeoVendor.IP2LOCATION
        assert detect_vendor("something") == GeoVendor.OTHER

    def test_kind(self) -> None:
        assert detect_kind("GeoLite2-Country") == GeoDatabaseKind.COUNTRY
        assert detect_kind("GeoLite2-City") == GeoDatabaseKind.CITY
        assert detect_kind("GeoLite2-ASN") == GeoDatabaseKind.ASN
        assert detect_kind("GeoIP2-Anonymous-IP") == GeoDatabaseKind.ANONYMOUS
        assert detect_kind("ipinfo privacy_detection") == GeoDatabaseKind.ANONYMOUS
        assert detect_kind("ipinfo standard_location") == GeoDatabaseKind.CITY
        assert detect_kind("custom") == GeoDatabaseKind.UNKNOWN


class TestMmdbReader:
    @pytest.fixture
    def city_db(self, tmp_path: Path) -> Path:
        return write_mmdb(
            tmp_path / "GeoLite2-City-Test.mmdb",
            "GeoLite2-City",
            {"81.2.69.0/24": MAXMIND_RECORD, "2001:db8::/32": IP2LOCATION_RECORD},
            description="GeoLite2 City test build",
        )

    def test_lookup_and_info(self, city_db: Path) -> None:
        reader = MmdbReader(city_db)
        try:
            info = reader.info
            assert info.format == GeoDatabaseFormat.MMDB
            assert info.vendor == GeoVendor.MAXMIND
            assert info.kind == GeoDatabaseKind.CITY
            assert info.database_type == "GeoLite2-City"
            assert info.build_epoch is not None
            assert info.size_bytes == city_db.stat().st_size
            assert len(info.sha256) == 64
            assert "GeoLite2" in info.attribution

            hit = reader.lookup("81.2.69.160")
            assert hit is not None and hit.country == "GB" and hit.city == "London"
            v6 = reader.lookup("2001:db8::1")
            assert v6 is not None and v6.country == "US"
            assert reader.lookup("8.8.8.8") is None
            assert reader.lookup("not-an-ip") is None
        finally:
            reader.close()

    def test_commercial_edition_needs_no_attribution(self, tmp_path: Path) -> None:
        path = write_mmdb(tmp_path / "GeoIP2-City.mmdb", "GeoIP2-City", {"1.0.0.0/24": MAXMIND_RECORD})
        assert inspect_file(path).attribution == ""

    def test_sniff_and_open(self, city_db: Path) -> None:
        assert sniff_format(city_db) == GeoDatabaseFormat.MMDB
        reader = open_reader(city_db)
        try:
            assert reader.info.database_type == "GeoLite2-City"
        finally:
            reader.close()

    def test_rejects_garbage(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.mmdb"
        junk.write_bytes(b"definitely not a database" * 100)
        with pytest.raises(GeoDatabaseError):
            sniff_format(junk)
        with pytest.raises(GeoDatabaseError):
            MmdbReader(junk)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(GeoDatabaseError):
            sniff_format(tmp_path / "missing.mmdb")
