# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the IP attribution repositories and the observation flusher against Postgres."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.db.geo_repository import (
    GeoDatabaseRepository,
    GeoSettingsRepository,
    ObservationRepository,
)
from api.db.redis import GEO_OBSERVATIONS_KEY, RedisClient
from api.db.repository import ConnectorRepository, CredentialRepository, ProjectRepository
from api.geo.models import (
    ConflictRule,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    GeoSourceKind,
    GeoVendor,
    IpObservation,
    ObservationSource,
)
from api.geo.observations import ObservationFlusher, ObservationRecorder
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project


async def _connectors(session: AsyncSession, *ids: str) -> None:
    """Create a project with one static connector per id, committed, so the aggregates' FK holds."""
    project = Project(name=f"geo-{ids[0]}", username=f"geo_{ids[0]}", password="pw")
    await ProjectRepository(session).create(project)
    credential = Credential(
        name="c", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={}
    )
    await CredentialRepository(session).create(credential)
    for connector_id in ids:
        await ConnectorRepository(session).create(
            Connector(
                id=connector_id,
                name=connector_id,
                credential_id=credential.id,
                credential_type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id=project.id,
                config={},
            )
        )
    await session.commit()


class TestGeoSettings:
    async def test_save_and_overwrite_single_row(self, db_session: AsyncSession) -> None:
        repo = GeoSettingsRepository(db_session)
        first = GeoSettings(
            echo_url="https://echo.example/ip", default_sources=[GeoSourceKind.VENDOR]
        )
        await repo.save(first, updated_by="admin")
        await db_session.commit()
        stored = await repo.get()
        assert stored is not None and stored.echo_url == "https://echo.example/ip"
        assert (
            stored.default_sources == [GeoSourceKind.VENDOR]
            and stored.default_conflict_rule == ConflictRule.CONSENSUS
        )

        await repo.save(
            first.model_copy(
                update={"default_conflict_rule": ConflictRule.FIRST, "preflight_max_attempts": 5}
            )
        )
        await db_session.commit()
        again = await repo.get()
        assert (
            again is not None
            and again.default_conflict_rule == ConflictRule.FIRST
            and again.preflight_max_attempts == 5
        )
        assert again.default_policy.sources == [GeoSourceKind.VENDOR]


class TestGeoDatabases:
    async def test_crud_with_blob(self, db_session: AsyncSession) -> None:
        repo = GeoDatabaseRepository(db_session)
        record = GeoDatabaseRecord(
            name="City", vendor=GeoVendor.MAXMIND, sha256="a" * 64, size_bytes=3
        )
        await repo.create(record, b"abc")
        await db_session.commit()

        fetched = await repo.get_by_id(record.id)
        assert (
            fetched is not None and fetched.name == "City" and fetched.vendor == GeoVendor.MAXMIND
        )
        assert await repo.get_blob(record.id) == b"abc"
        assert record.id in [r.id for r in await repo.get_all()]

        fetched.name = "Renamed"
        fetched.priority = 5
        await repo.update(fetched, blob=b"xyz")
        await db_session.commit()
        again = await repo.get_by_id(record.id)
        assert (
            again is not None
            and again.name == "Renamed"
            and again.version == 2
            and again.priority == 5
        )
        assert await repo.get_blob(record.id) == b"xyz"

        assert await repo.delete(record.id)
        await db_session.commit()
        assert await repo.get_by_id(record.id) is None
        assert await repo.get_blob(record.id) is None  # cascaded

    async def test_url_source_without_blob(self, db_session: AsyncSession) -> None:
        repo = GeoDatabaseRepository(db_session)
        record = GeoDatabaseRecord(
            name="Scheduled",
            source=GeoDatabaseSource.URL,
            update_url="https://vendor/x.mmdb",
            update_interval_hours=24,
            update_auth={"username": "acct", "password": "key"},
        )
        await repo.create(record, None)
        await db_session.commit()
        fetched = await repo.get_by_id(record.id)
        assert fetched is not None and fetched.update_auth == {
            "username": "acct",
            "password": "key",
        }
        assert await repo.get_blob(record.id) is None


def _observation(**overrides: object) -> IpObservation:
    values: dict[str, object] = {
        "connector_id": "conn-1",
        "proxy_id": "proxy-1",
        "source": ObservationSource.DISCOVERY,
        "ip": "81.2.69.160",
        "claimed_country": "US",
        "resolved_country": "GB",
        "conflict": True,
        "new_exit": True,
    }
    values.update(overrides)
    return IpObservation(**values)  # type: ignore[arg-type]


class TestObservations:
    async def test_insert_recent_and_stats(self, db_session: AsyncSession) -> None:
        await _connectors(db_session, "conn-1", "conn-2")
        repo = ObservationRepository(db_session)
        rows = [
            _observation(),
            _observation(ip="81.2.69.161", claimed_country="GB", conflict=False),
            _observation(
                connector_id="conn-2",
                proxy_id="proxy-2",
                claimed_country=None,
                resolved_country="DE",
                conflict=False,
            ),
        ]
        assert await repo.insert_many(rows) == 3
        await db_session.commit()

        recent, total = await repo.recent(limit=10)
        assert len(recent) == 3 and total == 3 and recent[0]["ip"]
        assert (await repo.recent(connector_id="conn-2"))[1] == 1
        assert (await repo.recent(conflicts_only=True))[1] == 1
        assert (await repo.recent(proxy_id="proxy-1"))[1] == 2

        assert await repo.count() == 3

    async def test_exit_ips_upsert_summary_and_list(self, db_session: AsyncSession) -> None:
        await _connectors(db_session, "conn-1", "conn-2")
        repo = ObservationRepository(db_session)
        from api.geo.observations import aggregate_exits

        day1 = datetime(2026, 9, 1, 8, 0, 0)
        day2 = datetime(2026, 9, 20, 8, 0, 0)
        first = [
            _observation(ip="10.0.0.1", observed_at=day1, resolved_country="GB"),
            _observation(ip="10.0.0.2", observed_at=day1, resolved_country="GB"),
        ]
        second = [
            _observation(ip="10.0.0.1", observed_at=day2, resolved_country="FR"),
            _observation(ip="10.0.0.3", observed_at=day2, resolved_country="GB"),
            _observation(connector_id="conn-2", ip="10.0.0.1", observed_at=day2),
            # A later re-attribution of an existing exit: refreshes the state
            # (claim now confirmed) without counting as a hand-out.
            _observation(
                ip="10.0.0.2",
                observed_at=day2 + timedelta(hours=1),
                new_exit=False,
                source=ObservationSource.REATTRIBUTE,
                claimed_country="GB",
                resolved_country="GB",
                conflict=False,
                proxy_id="proxy-9",
            ),
        ]
        await repo.add_exit_ips(aggregate_exits(first))
        await repo.add_exit_ips(aggregate_exits(second))
        await db_session.commit()

        summary = {
            s["connector_id"]: s
            for s in await repo.exit_summary(
                connector_ids=["conn-1", "conn-2"], since=datetime(2026, 9, 10)
            )
        }
        assert summary["conn-1"]["unique_total"] == 3 and summary["conn-1"]["unique_in_window"] == 1
        assert (
            summary["conn-1"]["sightings"] == 4
            and summary["conn-1"]["reused"] == 1
            and summary["conn-1"]["max_sightings"] == 2
        )
        assert summary["conn-2"]["unique_total"] == 1 and summary["conn-2"]["reused"] == 0
        assert await repo.exit_summary(connector_ids=[]) == []

        rows, total = await repo.exit_ips(connector_ids=["conn-1"])
        assert total == 3 and [r["ip"] for r in rows][0] == "10.0.0.2"  # newest state first
        by_ip = {r["ip"]: r for r in rows}
        reused = by_ip["10.0.0.1"]
        assert reused["sightings"] == 2 and reused["first_seen"] == day1 and reused["last_seen"] == day2
        assert reused["country"] == "FR" and reused["conflict"] is True and reused["source"] == "discovery"
        # The re-attribution moved the state but not the hand-out count.
        refreshed = by_ip["10.0.0.2"]
        assert refreshed["sightings"] == 1 and refreshed["source"] == "reattribute"
        assert refreshed["claimed_country"] == "GB" and refreshed["conflict"] is False
        assert refreshed["proxy_id"] == "proxy-9" and refreshed["last_seen"] == day2 + timedelta(hours=1)

        # Paging and the view's filters.
        page, total = await repo.exit_ips(connector_ids=["conn-1", "conn-2"], limit=2, offset=2)
        assert total == 4 and len(page) == 2
        assert await repo.exit_ips(connector_ids=[]) == ([], 0)
        assert (await repo.exit_ips(verdict="confirmed"))[1] == 1
        assert (await repo.exit_ips(verdict="contradicted"))[1] == 3
        assert (await repo.exit_ips(country="fr"))[1] == 1
        assert (await repo.exit_ips(ip="10.0.0.1"))[1] == 2
        assert (await repo.exit_ips(proxy_id="proxy-9"))[1] == 1

        # Accuracy counts each exit once with its latest verdict: conn-1 has three
        # exits with a claim, one of which the re-attribution confirmed.
        accuracy = {a["connector_id"]: a for a in await repo.exit_accuracy()}
        c1 = accuracy["conn-1"]
        assert (c1["exits"], c1["claimed"], c1["confirmed"], c1["contradicted"], c1["uncertain"]) == (3, 3, 1, 2, 0)
        assert c1["breakdown"] == [{"claimed_country": "US", "observed_country": "FR", "exits": 1}, {"claimed_country": "US", "observed_country": "GB", "exits": 1}] or \
            c1["breakdown"] == [{"claimed_country": "US", "observed_country": "GB", "exits": 1}, {"claimed_country": "US", "observed_country": "FR", "exits": 1}]
        # The window is by last sighting: only the re-attributed exit is newer than day2.
        windowed = {a["connector_id"]: a for a in await repo.exit_accuracy(since=day2 + timedelta(minutes=30))}
        assert windowed["conn-1"]["exits"] == 1 and windowed["conn-1"]["confirmed"] == 1
        assert "conn-2" not in windowed
        assert await repo.exit_accuracy(connector_ids=[]) == []

        # An older batch arriving late may add sightings but cannot roll the state back.
        late = [_observation(ip="10.0.0.2", observed_at=day1, proxy_id="proxy-old")]
        await repo.add_exit_ips(aggregate_exits(late))
        await db_session.commit()
        (row,), _ = await repo.exit_ips(ip="10.0.0.2", connector_ids=["conn-1"])
        assert row["sightings"] == 2 and row["proxy_id"] == "proxy-9" and row["first_seen"] == day1

        # Retention goes by the last sighting of any kind: the re-attribution
        # at 09:00 keeps 10.0.0.2, the 08:00 rows go.
        assert await repo.delete_exit_ips_last_seen_before(datetime(2026, 9, 10)) == 0
        assert await repo.delete_exit_ips_last_seen_before(datetime(2026, 9, 20, 9, 0)) == 3
        await db_session.commit()
        assert (await repo.exit_ips(connector_ids=["conn-1"]))[1] == 1

        # Deleting the connector takes its aggregates with it.
        assert await ConnectorRepository(db_session).delete("conn-1")
        await db_session.commit()
        assert await repo.exit_ips(connector_ids=["conn-1"]) == ([], 0)
        assert await repo.existing_connector_ids({"conn-1", "conn-2", "ghost"}) == {"conn-2"}

    async def test_recent_filters_and_pages_on_the_server(self, db_session: AsyncSession) -> None:
        """Filters narrow the query itself; a page never depends on what a previous page held."""
        repo = ObservationRepository(db_session)
        base = datetime(2026, 1, 1)
        rows = [
            _observation(
                observed_at=base + timedelta(minutes=i),
                ip=f"10.0.0.{i}",
                source=ObservationSource.HEALTH_CHECK,
            )
            for i in range(5)
        ]
        rows.append(
            _observation(connector_id="other", observed_at=base + timedelta(hours=1), ip="10.0.1.1")
        )
        await repo.insert_many(rows)
        await db_session.commit()

        # Newest first, paged; the total comes with every page and is the
        # size of the whole match, not of the page.
        page, total = await repo.recent(connector_id="conn-1", limit=2, offset=0)
        assert [r["ip"] for r in page] == ["10.0.0.4", "10.0.0.3"] and total == 5
        page, total = await repo.recent(connector_id="conn-1", limit=2, offset=4)
        assert [r["ip"] for r in page] == ["10.0.0.0"] and total == 5
        assert await repo.recent(connector_id="conn-1", limit=2, offset=40) == ([], 0)

        # The other connector's newer row does not leak into a filtered page,
        # and source and ip filters apply on their own.
        page, total = await repo.recent(source=ObservationSource.DISCOVERY.value)
        assert total == 1 and page[0]["connector_id"] == "other"
        page, total = await repo.recent(ip="10.0.0.2")
        assert total == 1 and page[0]["connector_id"] == "conn-1"
        assert await repo.recent(
            connector_id="conn-1", source=ObservationSource.DISCOVERY.value
        ) == ([], 0)

        # The verdict filter is the label the page shows, built from three columns.
        assert (await repo.recent(verdict="contradicted"))[1] == 6  # every fixture row conflicts
        assert (await repo.recent(verdict="confirmed"))[1] == 0
        await repo.insert_many(
            [
                _observation(ip="10.0.2.1", claimed_country="GB", conflict=False),
                _observation(
                    ip="10.0.2.2", claimed_country=None, conflict=False, disagreement=True
                ),
            ]
        )
        await db_session.commit()
        page, total = await repo.recent(verdict="confirmed")
        assert total == 1 and page[0]["ip"] == "10.0.2.1"
        page, total = await repo.recent(verdict="uncertain")
        assert total == 1 and page[0]["ip"] == "10.0.2.2"
        assert (await repo.recent(verdict="no_claim"))[1] == 1
        assert (await repo.recent(claimed_country="gb"))[1] == 1
        assert (await repo.recent(resolved_country="GB"))[1] == 8

    async def test_retention(self, db_session: AsyncSession) -> None:
        repo = ObservationRepository(db_session)
        old = _observation(observed_at=datetime(2020, 1, 1))
        await repo.insert_many([old, _observation()])
        await db_session.commit()
        assert await repo.delete_older_than(utc_now() - timedelta(days=7)) == 1
        await db_session.commit()
        assert await repo.count() == 1


@pytest.mark.usefixtures("db_engine")
class TestFlusher:
    async def test_recorder_to_redis_to_postgres(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        db_session: AsyncSession,
    ) -> None:
        await _connectors(db_session, "conn-1")
        repo = ObservationRepository(db_session)
        before = await repo.count()
        # Other test modules' apps publish to the same list; start from a clean queue.
        await redis_client.client.delete(GEO_OBSERVATIONS_KEY)
        recorder = ObservationRecorder(redis_client)
        recorder.record(_observation())
        recorder.record(_observation(ip="81.2.69.161", claimed_country="GB", conflict=False))
        # A connector deleted before the flush: its raw rows are kept, its aggregates skipped.
        recorder.record(_observation(connector_id="gone", ip="81.2.69.162"))
        assert await recorder.publish() == 3
        assert recorder.pending == 0 and recorder.published == 3
        assert await redis_client.client.llen(GEO_OBSERVATIONS_KEY) == 3

        flusher = ObservationFlusher(
            db_session_factory, redis_client, "inst", retention_days=lambda: 7
        )
        assert await flusher.flush_once() == 3
        exits, _ = await ObservationRepository(db_session).exit_ips(connector_ids=["conn-1"])
        assert {e["ip"] for e in exits} >= {"81.2.69.160", "81.2.69.161"}
        assert await ObservationRepository(db_session).exit_ips(connector_ids=["gone"]) == ([], 0)
        assert await redis_client.client.llen(GEO_OBSERVATIONS_KEY) == 0
        assert await flusher.flush_once() == 0

        assert await repo.count() == before + 3
        accuracy = {a["connector_id"]: a for a in await repo.exit_accuracy(connector_ids=["conn-1"])}
        assert accuracy["conn-1"]["claimed"] >= 2
        assert await repo.exit_accuracy(connector_ids=["gone"]) == []
