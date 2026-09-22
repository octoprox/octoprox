# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Scheduled database downloads: redirects are followed by hand, each hop vetted."""

import gzip
from datetime import datetime

import httpx
import pytest

from api.geo import updater
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.updater import DownloadError, candidate_urls, download, expand_update_url
from api.providers.sdk.egress import EgressGuard, EgressPolicy

OPEN = EgressPolicy(allow_http=True, allow_private=True, pin_dns=False)
# Captured before any test patches the module attribute, so repeated patching does not nest.
_REAL_CLIENT = httpx.AsyncClient


def _record(url: str = "https://download.vendor.test/db.mmdb.gz", **auth: str) -> GeoDatabaseRecord:
    return GeoDatabaseRecord(name="db", source=GeoDatabaseSource.URL, update_url=url, update_auth=dict(auth))


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> list[httpx.Request]:
    """Route every client the downloader builds through ``handler`` and record the requests."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator]

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(recording), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(updater.httpx, "AsyncClient", factory)
    return seen


class TestDatedUrls:
    def test_placeholders_fill_the_current_month_then_the_previous(self) -> None:
        template = "https://download.db-ip.com/free/dbip-country-lite-{YYYY}-{MM}.mmdb.gz"
        assert expand_update_url(template, datetime(2026, 1, 3)) == "https://download.db-ip.com/free/dbip-country-lite-2026-01.mmdb.gz"
        assert candidate_urls(template, now=datetime(2026, 1, 3, 9, 0)) == [
            "https://download.db-ip.com/free/dbip-country-lite-2026-01.mmdb.gz",
            "https://download.db-ip.com/free/dbip-country-lite-2025-12.mmdb.gz",
        ]
        # A fixed URL is tried once, as it is.
        assert candidate_urls("https://download.maxmind.com/x?suffix=tar.gz") == ["https://download.maxmind.com/x?suffix=tar.gz"]

    async def test_falls_back_to_last_month_only_on_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime(2026, 9, 1, 0, 5)
        monkeypatch.setattr(updater, "utc_now", lambda: now)
        payload = gzip.compress(b"september-not-yet")

        def handler(request: httpx.Request) -> httpx.Response:
            if "2026-09" in request.url.path:
                return httpx.Response(404)
            assert "2026-08" in request.url.path
            return httpx.Response(200, content=payload)

        seen = _use_transport(monkeypatch, handler)
        record = _record("https://download.db-ip.com/free/dbip-country-lite-{YYYY}-{MM}.mmdb.gz")
        assert await download(record, EgressGuard(OPEN)) == b"september-not-yet"
        assert [r.url.path for r in seen] == [
            "/free/dbip-country-lite-2026-09.mmdb.gz",
            "/free/dbip-country-lite-2026-08.mmdb.gz",
        ]

        # Any other failure stops at the first URL; a fixed URL never falls back.
        _use_transport(monkeypatch, lambda request: httpx.Response(500))
        with pytest.raises(DownloadError, match="HTTP 500"):
            await download(record, EgressGuard(OPEN))
        seen = _use_transport(monkeypatch, lambda request: httpx.Response(404))
        with pytest.raises(DownloadError, match="HTTP 404"):
            await download(_record("https://download.vendor.test/fixed.mmdb"), EgressGuard(OPEN))
        assert len(seen) == 1


class TestDownloadRedirects:
    async def test_redirect_to_storage_drops_vendor_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = gzip.compress(b"mmdb-bytes")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "download.vendor.test":
                assert request.headers["Authorization"] == "Bearer secret"
                return httpx.Response(302, headers={"Location": "https://storage.example.test/signed/db.mmdb.gz?sig=1"})
            assert request.url.host == "storage.example.test"
            assert "authorization" not in request.headers and request.headers["Host"] == "storage.example.test"
            return httpx.Response(200, content=payload)

        seen = _use_transport(monkeypatch, handler)
        assert await download(_record(token="secret"), EgressGuard(OPEN)) == b"mmdb-bytes"
        assert [r.url.host for r in seen] == ["download.vendor.test", "storage.example.test"]

    async def test_redirect_target_is_vetted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

        _use_transport(monkeypatch, handler)
        with pytest.raises(DownloadError, match="egress policy"):
            await download(_record(), EgressGuard(EgressPolicy(allow_http=True, allow_private=False, pin_dns=False)))

    async def test_redirect_loop_gives_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": str(request.url)})

        seen = _use_transport(monkeypatch, handler)
        with pytest.raises(DownloadError, match="redirects"):
            await download(_record(), EgressGuard(OPEN))
        assert len(seen) == updater.MAX_REDIRECTS + 1

    async def test_relative_location_and_plain_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/db.mmdb":
                return httpx.Response(301, headers={"Location": "/files/db.mmdb"})
            return httpx.Response(200, content=b"raw")

        _use_transport(monkeypatch, handler)
        assert await download(_record("https://download.vendor.test/db.mmdb"), EgressGuard(OPEN)) == b"raw"
