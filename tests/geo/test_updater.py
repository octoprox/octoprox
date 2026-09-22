# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Scheduled database downloads: redirects are followed by hand, each hop vetted."""

import gzip

import httpx
import pytest

from api.geo import updater
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.updater import DownloadError, download
from api.providers.sdk.egress import EgressGuard, EgressPolicy

OPEN = EgressPolicy(allow_http=True, allow_private=True, pin_dns=False)


def _record(url: str = "https://download.vendor.test/db.mmdb.gz", **auth: str) -> GeoDatabaseRecord:
    return GeoDatabaseRecord(name="db", source=GeoDatabaseSource.URL, update_url=url, update_auth=dict(auth))


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> list[httpx.Request]:
    """Route every client the downloader builds through ``handler`` and record the requests."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator]

    real = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(recording), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(updater.httpx, "AsyncClient", factory)
    return seen


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
