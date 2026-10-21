# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the api's internal endpoint that serves gateways, which have no Postgres, the IP database files their feed lists.

The api answers from its own copy, or from the stored bytes when it has none, never with a file at another checksum;
the token guard itself is tested with the app factory, and here only that this route is behind it.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.core.config import Settings
from api.core.roles import Role
from api.db.geo_repository import GeoDatabaseRepository
from api.feed.codec import internal_token
from api.geo.models import GeoDatabaseKind, GeoDatabaseRecord, GeoVendor
from api.geo.store import BlobMissingError, HttpBlobSource
from tests.conftest import api_runtime, on_app_loop
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb


def _blob_url(database_id: str) -> str:
    return f"/internal/v1/geo/databases/{database_id}/blob"


def _token(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {internal_token(settings.internal_secret)}"}


def _database(directory: Path) -> Path:
    return write_mmdb(directory / "GeoLite2-City.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})


def _upload(client: TestClient, path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        response = client.post(
            "/api/v1/geo/databases", files={"file": (path.name, handle, "application/octet-stream")}
        )
    assert response.status_code == 201, response.text
    record: dict[str, Any] = response.json()
    return record


class TestGeoDatabaseBlob:
    @pytest.fixture
    def stored(self, authenticated_client: TestClient, tmp_path: Path) -> tuple[dict[str, Any], bytes]:
        """A database uploaded through the admin API, and its bytes; deleted when the test ends."""
        path = _database(tmp_path)
        record = _upload(authenticated_client, path)
        yield record, path.read_bytes()
        authenticated_client.delete(f"/api/v1/geo/databases/{record['id']}")

    def test_the_internal_token_is_required(
        self, authenticated_client: TestClient, stored: tuple[dict[str, Any], bytes]
    ) -> None:
        record, _ = stored
        params = {"sha256": record["sha256"]}
        # The client signs in as an admin: a user's token is not the internal one.
        assert authenticated_client.get(_blob_url(record["id"]), params=params).status_code == 401
        refused = authenticated_client.get(
            _blob_url(record["id"]), params=params, headers={"Authorization": "Bearer not-the-token"}
        )
        assert refused.status_code == 401 and refused.headers["www-authenticate"] == "Bearer"

    def test_streams_the_file_at_its_checksum(
        self, authenticated_client: TestClient, test_settings: Settings, stored: tuple[dict[str, Any], bytes]
    ) -> None:
        record, data = stored
        response = authenticated_client.get(
            _blob_url(record["id"]), params={"sha256": record["sha256"]}, headers=_token(test_settings)
        )
        assert response.status_code == 200
        assert response.content == data and hashlib.sha256(response.content).hexdigest() == record["sha256"]
        assert response.headers["content-type"] == "application/octet-stream"
        assert int(response.headers["content-length"]) == len(data)

    def test_another_checksum_or_database_is_a_404(
        self, authenticated_client: TestClient, test_settings: Settings, stored: tuple[dict[str, Any], bytes]
    ) -> None:
        record, _ = stored
        stale = authenticated_client.get(
            _blob_url(record["id"]), params={"sha256": "0" * 64}, headers=_token(test_settings)
        )
        assert stale.status_code == 404
        unknown = authenticated_client.get(
            _blob_url("no-such-database"), params={"sha256": record["sha256"]}, headers=_token(test_settings)
        )
        assert unknown.status_code == 404

    def test_a_malformed_checksum_is_refused(
        self, authenticated_client: TestClient, test_settings: Settings, stored: tuple[dict[str, Any], bytes]
    ) -> None:
        record, _ = stored
        for params in ({}, {"sha256": "abc"}, {"sha256": record["sha256"].upper()}):
            response = authenticated_client.get(_blob_url(record["id"]), params=params, headers=_token(test_settings))
            assert response.status_code == 422, params

    def test_a_missing_copy_comes_from_the_stored_bytes(
        self, authenticated_client: TestClient, test_settings: Settings, stored: tuple[dict[str, Any], bytes]
    ) -> None:
        record, data = stored
        copy = Path(test_settings.geo_cache_dir) / f"{record['id']}.mmdb"
        assert copy.exists()
        copy.unlink()

        response = authenticated_client.get(
            _blob_url(record["id"]), params={"sha256": record["sha256"]}, headers=_token(test_settings)
        )
        assert response.status_code == 200 and response.content == data
        # Written back from Postgres, checksum checked, for the next gateway.
        assert copy.read_bytes() == data

    def test_a_database_written_through_another_api_process_is_served(
        self, build_app: Callable[..., FastAPI], tmp_path: Path
    ) -> None:
        """This process has not read the list since: the endpoint reads it before answering 404."""
        # The write announces nothing and no full read comes within the hour, so only the endpoint can learn of the row.
        app = build_app(Role.API, reconcile_interval=3600)
        data = _database(tmp_path).read_bytes()
        record = GeoDatabaseRecord(
            name="written elsewhere",
            vendor=GeoVendor.MAXMIND,
            kind=GeoDatabaseKind.CITY,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
        with TestClient(app) as client:
            runtime = api_runtime(client)

            async def write_elsewhere() -> None:
                async with runtime.session_factory() as session:
                    await GeoDatabaseRepository(session).create(record, data)
                    await session.commit()

            on_app_loop(client, write_elsewhere)
            assert not runtime.geo_database_store.is_loaded(record.id)
            try:
                response = client.get(
                    _blob_url(record.id), params={"sha256": record.sha256}, headers=_token(app.state.settings)
                )
                assert response.status_code == 200 and response.content == data
            finally:
                async def delete() -> None:
                    async with runtime.session_factory() as session:
                        await GeoDatabaseRepository(session).delete(record.id)
                        await session.commit()

                on_app_loop(client, delete)

    def test_a_gateway_fetches_the_file_it_lists(
        self,
        authenticated_client: TestClient,
        test_settings: Settings,
        stored: tuple[dict[str, Any], bytes],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The gateway's blob source against this endpoint: the path, the query, the token and what a 404 means."""
        record, data = stored
        app = authenticated_client.app
        client_class = httpx.AsyncClient

        def through_the_app(**options: Any) -> httpx.AsyncClient:
            return client_class(transport=httpx.ASGITransport(app=app), **options)

        monkeypatch.setattr(httpx, "AsyncClient", through_the_app)
        source = HttpBlobSource("http://api.internal:8000", internal_token(test_settings.internal_secret))

        fetched = tmp_path / "fetched.mmdb"
        on_app_loop(authenticated_client, source.fetch, record["id"], record["sha256"], fetched)
        assert fetched.read_bytes() == data

        # A checksum the api has no file at: the gateway keeps waiting and asks again later.
        with pytest.raises(BlobMissingError):
            on_app_loop(authenticated_client, source.fetch, record["id"], "1" * 64, tmp_path / "other.mmdb")
        assert not (tmp_path / "other.mmdb").exists()
