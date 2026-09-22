# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Local IP database readers.

MaxMind's mmdb container is the common denominator: MaxMind, DB-IP, IPinfo and
IP2Location all publish it, and one memory-mapped reader serves every file.
What differs per vendor is the *record layout*, so :func:`normalize_record`
probes each known layout and returns one :class:`IpLocation` whatever the
vendor. IP2Location's own BIN format gets a second reader when the optional
``IP2Location`` package is installed.

Readers are opened once per database per process and shared; lookups are
synchronous, sub-millisecond and never touch the network.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import maxminddb
import structlog

from api.geo.models import (
    GeoDatabaseFormat,
    GeoDatabaseKind,
    GeoVendor,
    IpLocation,
)

logger = structlog.get_logger()

MMDB_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"

# Attribution the free databases' licenses ask for. Shown in the admin panel
# next to the database while it is loaded.
ATTRIBUTIONS: dict[GeoVendor, str] = {
    GeoVendor.MAXMIND: "This product includes GeoLite2 data created by MaxMind, available from https://www.maxmind.com.",
    GeoVendor.DBIP: "IP Geolocation by DB-IP (https://db-ip.com), licensed under CC BY 4.0.",
    GeoVendor.IPINFO: "IP address data powered by IPinfo (https://ipinfo.io).",
    GeoVendor.IP2LOCATION: "This site or product includes IP2Location LITE data available from https://lite.ip2location.com.",
}


class GeoDatabaseError(Exception):
    """A file could not be opened or is not an IP database we can read."""


@dataclass(frozen=True)
class DatabaseInfo:
    """What a database file says about itself."""

    format: GeoDatabaseFormat
    database_type: str
    vendor: GeoVendor
    kind: GeoDatabaseKind
    build_epoch: datetime | None
    record_count: int
    ip_version: int
    languages: list[str] = field(default_factory=list)
    description: str = ""
    size_bytes: int = 0
    sha256: str = ""

    @property
    def attribution(self) -> str:
        """License attribution for free editions; commercial editions need none."""
        lowered = self.database_type.lower()
        if self.vendor == GeoVendor.MAXMIND and "geolite" not in lowered:
            return ""
        if self.vendor == GeoVendor.DBIP and "lite" not in lowered:
            return ""
        if self.vendor == GeoVendor.IP2LOCATION and "lite" not in lowered:
            return ""
        return ATTRIBUTIONS.get(self.vendor, "")


class GeoReader(Protocol):
    """One open database."""

    @property
    def info(self) -> DatabaseInfo: ...

    def lookup(self, ip: str) -> IpLocation | None:
        """Normalised record for ``ip``, None when the database has no entry."""
        ...

    def close(self) -> None: ...


# --- vendor detection --------------------------------------------------------------------------


def detect_vendor(database_type: str, description: str = "") -> GeoVendor:
    """Guess the vendor from the mmdb ``database_type`` (and description as a fallback)."""
    text = f"{database_type} {description}".lower()
    if "geoip2" in text or "geolite" in text or "maxmind" in text:
        return GeoVendor.MAXMIND
    if "dbip" in text or "db-ip" in text:
        return GeoVendor.DBIP
    if "ipinfo" in text:
        return GeoVendor.IPINFO
    if "ip2location" in text or "ip2proxy" in text:
        return GeoVendor.IP2LOCATION
    return GeoVendor.OTHER


def detect_kind(database_type: str, description: str = "") -> GeoDatabaseKind:
    """Guess what the database answers from its type name."""
    text = f"{database_type} {description}".lower()
    if any(word in text for word in ("anonymous", "privacy", "proxy", "vpn")):
        return GeoDatabaseKind.ANONYMOUS
    if "city" in text or "location" in text:
        return GeoDatabaseKind.CITY
    if "asn" in text:
        return GeoDatabaseKind.ASN
    if "country" in text:
        return GeoDatabaseKind.COUNTRY
    return GeoDatabaseKind.UNKNOWN


# --- record normalisation ----------------------------------------------------------------------


def _get(record: Mapping[str, Any], *path: str) -> Any:
    """Walk nested keys; None when any step is missing or not a mapping."""
    current: Any = record
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _first_str(record: Mapping[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _get(record, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_float(record: Mapping[str, Any], *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        value = _get(record, *path)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _first_bool(record: Mapping[str, Any], *paths: tuple[str, ...]) -> bool | None:
    for path in paths:
        value = _get(record, *path)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
    return None


_ASN_RE = re.compile(r"^\s*(?:AS)?(\d+)\s*$", re.IGNORECASE)


def _first_asn(record: Mapping[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        value = _get(record, *path)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = _ASN_RE.match(value)
            if match:
                return int(match.group(1))
    return None


def _english_name(record: Mapping[str, Any], *path: str) -> str | None:
    """MaxMind-style ``{names: {en: ...}}`` or a plain string at ``path``."""
    value = _get(record, *path)
    if isinstance(value, Mapping):
        names = value.get("names")
        if isinstance(names, Mapping):
            english = names.get("en")
            if isinstance(english, str) and english.strip():
                return english.strip()
            for candidate in names.values():
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def normalize_record(record: Mapping[str, Any] | None) -> IpLocation | None:
    """Map any known vendor layout onto :class:`IpLocation`.

    Layouts probed, in order of specificity:

    * MaxMind GeoIP2/GeoLite2 and DB-IP mmdb: ``country.iso_code``,
      ``subdivisions[0].names.en``, ``city.names.en``, ``location.*``,
      ``traits.autonomous_system_*``; Anonymous IP: top-level ``is_*`` flags.
    * IPinfo: flat ``country``, ``region``, ``city``, ``latitude``, ``asn``
      (``AS13335``), ``as_name``; privacy: ``vpn``, ``proxy``, ``tor``, ``hosting``.
    * IP2Location mmdb and BIN: ``country_code``/``country_short``,
      ``region_name``, ``city_name``, ``latitude``, ``zip_code``.
    """
    if not isinstance(record, Mapping) or not record:
        return None

    subdivisions = record.get("subdivisions")
    region = None
    if isinstance(subdivisions, list) and subdivisions and isinstance(subdivisions[0], Mapping):
        region = _english_name(subdivisions[0])
    if region is None:
        region = _first_str(record, ("region",), ("region_name",), ("state",), ("stateprov",))

    country = _first_str(
        record,
        ("country", "iso_code"),
        ("registered_country", "iso_code"),
        ("country_code",),
        ("country_short",),
        ("countryCode",),
    )
    if country is None:
        # IPinfo puts the ISO code directly in ``country``; MaxMind puts a mapping there.
        plain = record.get("country")
        if isinstance(plain, str):
            country = plain

    location = IpLocation(
        country=country,
        region=region,
        city=_english_name(record, "city") or _first_str(record, ("city_name",)),
        postal_code=_first_str(record, ("postal", "code"), ("postal",), ("postal_code",), ("zip_code",), ("zipcode",)),
        latitude=_first_float(record, ("location", "latitude"), ("latitude",), ("lat",)),
        longitude=_first_float(record, ("location", "longitude"), ("longitude",), ("lng",), ("lon",)),
        asn=_first_asn(
            record,
            ("traits", "autonomous_system_number"),
            ("autonomous_system_number",),
            ("asn",),
            ("as_number",),
        ),
        organization=_first_str(
            record,
            ("traits", "autonomous_system_organization"),
            ("autonomous_system_organization",),
            ("as_name",),
            ("as",),
            ("organization",),
            ("org",),
            ("isp",),
        ),
        is_anonymous=_first_bool(record, ("is_anonymous",), ("traits", "is_anonymous"), ("traits", "is_anonymous_proxy")),
        is_hosting=_first_bool(record, ("is_hosting_provider",), ("traits", "is_hosting_provider"), ("hosting",)),
        is_vpn=_first_bool(record, ("is_anonymous_vpn",), ("traits", "is_anonymous_vpn"), ("vpn",)),
        is_public_proxy=_first_bool(record, ("is_public_proxy",), ("traits", "is_public_proxy"), ("proxy",)),
        is_tor=_first_bool(record, ("is_tor_exit_node",), ("traits", "is_tor_exit_node"), ("tor",)),
        is_residential_proxy=_first_bool(record, ("is_residential_proxy",), ("traits", "is_residential_proxy"), ("relay",)),
    )
    if location.is_empty():
        return None
    # A privacy database that only knows the flags still says something about anonymity.
    if location.is_anonymous is None and any(
        flag for flag in (location.is_vpn, location.is_public_proxy, location.is_tor, location.is_residential_proxy)
    ):
        location.is_anonymous = True
    return location


# --- mmdb --------------------------------------------------------------------------------------


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _mmdb_info(reader: maxminddb.Reader, path: Path) -> DatabaseInfo:
    meta = reader.metadata()
    description_map = meta.description if isinstance(meta.description, dict) else {}
    description = str((description_map.get("en") or next(iter(description_map.values()), "")) if description_map else "")
    sha256, size = _sha256_and_size(path)
    database_type = str(meta.database_type or "")
    return DatabaseInfo(
        format=GeoDatabaseFormat.MMDB,
        database_type=database_type,
        vendor=detect_vendor(database_type, description),
        kind=detect_kind(database_type, description),
        build_epoch=datetime.fromtimestamp(meta.build_epoch, tz=UTC).replace(tzinfo=None) if meta.build_epoch else None,
        record_count=int(meta.node_count or 0),
        ip_version=int(meta.ip_version or 6),
        languages=[str(lang) for lang in (meta.languages or [])],
        description=str(description),
        size_bytes=size,
        sha256=sha256,
    )


class MmdbReader:
    """Memory-mapped reader for any mmdb file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._reader = maxminddb.open_database(str(self._path), mode=maxminddb.MODE_AUTO)
        except (maxminddb.InvalidDatabaseError, OSError, ValueError) as exc:
            raise GeoDatabaseError(f"Not a readable mmdb file: {exc}") from exc
        try:
            self._info = _mmdb_info(self._reader, self._path)
        except Exception as exc:
            self._reader.close()
            raise GeoDatabaseError(f"Could not read mmdb metadata: {exc}") from exc

    @property
    def info(self) -> DatabaseInfo:
        return self._info

    @property
    def path(self) -> Path:
        return self._path

    def lookup(self, ip: str) -> IpLocation | None:
        try:
            record = self._reader.get(ip)
        except ValueError:
            return None
        except maxminddb.InvalidDatabaseError as exc:
            logger.warning("Corrupt mmdb database", path=str(self._path), error=str(exc))
            return None
        return normalize_record(record) if isinstance(record, Mapping) else None

    def close(self) -> None:
        self._reader.close()


# --- IP2Location BIN ---------------------------------------------------------------------------


class Ip2LocationBinReader:
    """Reader for IP2Location's proprietary BIN files (needs the ``IP2Location`` package)."""

    def __init__(self, path: str | Path) -> None:
        try:
            import IP2Location  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GeoDatabaseError(
                "IP2Location BIN files need the optional 'IP2Location' package (pip install IP2Location)"
            ) from exc
        self._path = Path(path)
        try:
            self._db = IP2Location.IP2Location(str(self._path))
        except Exception as exc:
            raise GeoDatabaseError(f"Not a readable IP2Location BIN file: {exc}") from exc
        sha256, size = _sha256_and_size(self._path)
        year = getattr(self._db, "_dbyear", None)
        month = getattr(self._db, "_dbmonth", None)
        day = getattr(self._db, "_dbday", None)
        build: datetime | None = None
        if year and month and day:
            try:
                build = datetime(2000 + int(year) if int(year) < 100 else int(year), int(month), int(day))
            except ValueError:
                build = None
        database_type = f"IP2Location-DB{getattr(self._db, '_dbtype', '')}"
        self._info = DatabaseInfo(
            format=GeoDatabaseFormat.IP2LOCATION_BIN,
            database_type=database_type,
            vendor=GeoVendor.IP2LOCATION,
            kind=detect_kind(database_type) if "PX" in database_type else GeoDatabaseKind.CITY,
            build_epoch=build,
            record_count=int(getattr(self._db, "_ipv4dbcount", 0) or 0) + int(getattr(self._db, "_ipv6dbcount", 0) or 0),
            ip_version=6 if getattr(self._db, "_ipv6dbcount", 0) else 4,
            size_bytes=size,
            sha256=sha256,
        )

    @property
    def info(self) -> DatabaseInfo:
        return self._info

    def lookup(self, ip: str) -> IpLocation | None:
        try:
            record = self._db.get_all(ip)
        except Exception:
            return None
        if record is None:
            return None
        data: dict[str, Any] = {}
        for key in ("country_short", "region", "city", "latitude", "longitude", "zipcode", "asn", "as", "isp"):
            value = getattr(record, key, None)
            if value in (None, "-", "", "N/A", "INVALID IP ADDRESS"):
                continue
            data[key] = value
        return normalize_record(data)

    def close(self) -> None:
        close = getattr(self._db, "close", None)
        if callable(close):
            close()


# --- opening -----------------------------------------------------------------------------------


def sniff_format(path: str | Path) -> GeoDatabaseFormat:
    """Tell mmdb from IP2Location BIN by content, falling back to the extension."""
    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            handle.seek(max(file_path.stat().st_size - 128 * 1024, 0))
            tail = handle.read()
    except OSError as exc:
        raise GeoDatabaseError(f"Cannot read {file_path}: {exc}") from exc
    if MMDB_METADATA_MARKER in tail:
        return GeoDatabaseFormat.MMDB
    if file_path.suffix.lower() == ".bin":
        return GeoDatabaseFormat.IP2LOCATION_BIN
    raise GeoDatabaseError("Unrecognised database file: expected an mmdb or IP2Location BIN file")


def open_reader(path: str | Path, fmt: GeoDatabaseFormat | None = None) -> GeoReader:
    """Open a database file with the reader for its format."""
    resolved = fmt or sniff_format(path)
    if resolved == GeoDatabaseFormat.MMDB:
        return MmdbReader(path)
    return Ip2LocationBinReader(path)


def inspect_file(path: str | Path) -> DatabaseInfo:
    """Validate a file by opening it, and return what it says about itself."""
    reader = open_reader(path)
    try:
        return reader.info
    finally:
        reader.close()


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True
