# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the IP attribution repositories against Postgres."""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.db import geo_repository
from api.db.geo_repository import (
    ExitSighting,
    GeoDatabaseRepository,
    GeoSettingsRepository,
    ObservationRepository,
)
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
)
from api.geo.models import (
    ConflictRule,
    ExitJudgement,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    GeoVendor,
    IpObservation,
    ObservationSource,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project

# What the sweep logs when it rewrote the blob table and gave the disk back.
RECLAIMED = "IP database storage reclaimed"


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
        first = GeoSettings(echo_url="https://echo.example/ip")
        await repo.save(first, updated_by="admin")
        await db_session.commit()
        stored = await repo.get()
        assert stored is not None and stored.echo_url == "https://echo.example/ip"
        assert stored.default_conflict_rule == ConflictRule.CONSENSUS

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
        assert again.default_policy.conflict_rule == ConflictRule.FIRST
        assert again.default_city_radius_km == 25

        await repo.save(again.model_copy(update={"default_city_radius_km": 40.0}))
        await db_session.commit()
        radius = await repo.get()
        assert radius is not None and radius.default_policy.city_radius_km == 40.0


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


    async def test_the_sweep_gives_back_the_disk_of_replaced_and_removed_files(
        self, db_session: AsyncSession, db_engine: Any
    ) -> None:
        """Postgres keeps the old bytes until the table is rewritten; the sweep does that when it pays."""
        size = 2 * 1024 * 1024  # random so TOAST cannot compress it away
        repo = GeoDatabaseRepository(db_session)
        record = GeoDatabaseRecord(name="City", sha256="a" * 64, size_bytes=size)
        await repo.create(record, os.urandom(size))
        await db_session.commit()
        one_file = await _blob_table_bytes(db_session)

        async with db_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            # A compact table is left alone.
            with structlog.testing.capture_logs() as logs:
                await geo_repository.reclaim_blob_space(conn)
            assert RECLAIMED not in [log["event"] for log in logs]

            # Every replaced file stays on disk until the sweep.
            for _ in range(3):
                await repo.update(record, blob=os.urandom(size))
                await db_session.commit()
            assert await _blob_table_bytes(db_session) >= one_file + 2 * size
            with structlog.testing.capture_logs() as logs:
                await geo_repository.reclaim_blob_space(conn)
            assert [log["event"] for log in logs] == [RECLAIMED]
            assert await _blob_table_bytes(db_session) < one_file + size // 2
            assert len(await repo.get_blob(record.id) or b"") == size

            await repo.delete(record.id)
            await db_session.commit()
            with structlog.testing.capture_logs() as logs:
                await geo_repository.reclaim_blob_space(conn)
            assert [log["event"] for log in logs] == [RECLAIMED]
        assert await _blob_table_bytes(db_session) < size // 2

        # The plain pass after the rewrite leaves the system page nothing to report.
        await asyncio.sleep(0.7)  # statistics reach the views asynchronously
        dead = await db_session.execute(
            text(
                "SELECT coalesce(s.n_dead_tup, 0) + coalesce(t.n_dead_tup, 0), "
                "greatest(s.last_vacuum, t.last_vacuum) IS NOT NULL "
                "FROM pg_class c JOIN pg_stat_all_tables s ON s.relid = c.oid "
                "LEFT JOIN pg_stat_all_tables t ON t.relid = c.reltoastrelid "
                "WHERE c.relname = 'geo_database_blobs'"
            )
        )
        await db_session.commit()
        assert tuple(dead.one()) == (0, True)

    async def test_pause_round_trips(self, db_session: AsyncSession) -> None:
        repo = GeoDatabaseRepository(db_session)
        record = GeoDatabaseRecord(
            name="Scheduled", source=GeoDatabaseSource.URL, update_url="https://vendor/x.mmdb", update_interval_hours=24
        )
        await repo.create(record, None)
        await db_session.commit()
        fetched = await repo.get_by_id(record.id)
        assert fetched is not None and fetched.update_paused is False
        fetched.update_paused = True
        await repo.update(fetched)
        await db_session.commit()
        again = await repo.get_by_id(record.id)
        assert again is not None and again.update_paused is True


async def _blob_table_bytes(session: AsyncSession) -> int:
    result = await session.execute(text("SELECT pg_total_relation_size('geo_database_blobs')"))
    await session.commit()
    return int(result.scalar_one())


def _observation(**overrides: object) -> IpObservation:
    values: dict[str, object] = {
        "connector_id": "conn-1",
        "proxy_id": "proxy-1",
        "source": ObservationSource.DISCOVERY,
        "ip": "81.2.69.160",
        "claimed_country": "US",
        "resolved_country": "GB",
        "country_conflict": True,
    }
    values.update(overrides)
    return IpObservation(**values)  # type: ignore[arg-type]


class TestObservations:
    async def test_insert_recent_and_stats(self, db_session: AsyncSession) -> None:
        await _connectors(db_session, "conn-1", "conn-2")
        repo = ObservationRepository(db_session)
        rows = [
            _observation(),
            _observation(ip="81.2.69.161", claimed_country="GB", country_conflict=False),
            _observation(
                connector_id="conn-2",
                proxy_id="proxy-2",
                claimed_country=None,
                resolved_country="DE",
                country_conflict=False,
            ),
        ]
        assert await repo.insert_many(rows) == 3
        await db_session.commit()

        recent, total, exact = await repo.recent(limit=10)
        assert len(recent) == 3 and total == 3 and exact and recent[0]["ip"]
        assert (await repo.recent(connector_id="conn-2"))[1] == 1
        assert (await repo.recent(conflicts_only=True))[1] == 1
        assert (await repo.recent(proxy_id="proxy-1"))[1] == 2

        # The status line reads the planner's estimate, current once the table is analysed.
        await db_session.execute(text("ANALYZE ip_observations"))
        assert await repo.estimated_count() == 3

    async def test_exit_ips_upsert_summary_and_list(self, db_session: AsyncSession) -> None:
        await _connectors(db_session, "conn-1", "conn-2")
        repo = ObservationRepository(db_session)

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
        ]
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in first]))
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in second]))
        # A later re-attribution of an existing exit by its holder rewrites its
        # verdict (claim now confirmed) and nothing else: no hand-out, no sighting time.
        applied = await repo.apply_judgements(
            [
                ExitJudgement(
                    proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.2",
                    claimed_country="GB", resolved_country="GB", country_conflict=False,
                ),
                # An exit never sighted has no row to judge; nothing is created.
                ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.42", claimed_country="GB"),
                # Computed before the exit's last sighting (a late arrival or a stale
                # read): the sighting saw the proxy's state later, so it stands.
                ExitJudgement(
                    proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.1",
                    judged_at=day2 - timedelta(minutes=1), claimed_country="US", resolved_country="US", country_conflict=False,
                ),
                # Another slot of the connector sharing the exit, with its own claim: the
                # row's verdict belongs to its holder, so this one is dropped.
                ExitJudgement(
                    proxy_id="proxy-9", connector_id="conn-1", ip="10.0.0.3",
                    claimed_country="DE", resolved_country="GB", country_conflict=True,
                ),
            ]
        )
        assert applied == 1  # rows actually rewritten, not judgements submitted
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

        rows, total, _ = await repo.exit_ips(connector_ids=["conn-1"])
        assert total == 3 and [r["ip"] for r in rows][-1] == "10.0.0.2"  # last seen day1; the judgement moved nothing
        by_ip = {r["ip"]: r for r in rows}
        reused = by_ip["10.0.0.1"]
        assert reused["sightings"] == 2 and reused["first_seen"] == day1 and reused["last_seen"] == day2
        assert reused["country"] == "FR" and reused["country_conflict"] is True and reused["source"] == "discovery"  # the older judgement was dropped
        third = by_ip["10.0.0.3"]
        assert third["claimed_country"] == "US" and third["source"] == "discovery"  # the non-holder's judgement was dropped
        # The judgement rewrote the verdict but not the count or the sighting times.
        refreshed = by_ip["10.0.0.2"]
        assert refreshed["sightings"] == 1 and refreshed["source"] == "reattribute"
        assert refreshed["claimed_country"] == "GB" and refreshed["country_conflict"] is False
        # The holder is the proxy whose sighting was most recent; a judgement does not change it.
        assert refreshed["proxy_id"] == "proxy-1" and refreshed["last_seen"] == day1 and refreshed["first_seen"] == day1

        # Paging and the view's filters.
        page, total, _ = await repo.exit_ips(connector_ids=["conn-1", "conn-2"], limit=2, offset=2)
        assert total == 4 and len(page) == 2
        assert await repo.exit_ips(connector_ids=[]) == ([], 0, True)
        assert await repo.count_exit_ips(connector_ids=["conn-1", "conn-2"]) == 4
        assert await repo.count_exit_ips(connector_ids=[]) == 0
        assert await repo.count_exit_ips(verdict="contradicted", proxy_id="proxy-1") == 3
        assert (await repo.exit_ips(verdict="confirmed"))[1] == 1
        assert (await repo.exit_ips(verdict="contradicted"))[1] == 3
        assert (await repo.exit_ips(country="fr"))[1] == 1
        assert (await repo.exit_ips(ip="10.0.0.1"))[1] == 2
        assert (await repo.exit_ips(proxy_id="proxy-1"))[1] == 4 and (await repo.exit_ips(proxy_id="proxy-9"))[1] == 0  # both connectors' rows

        # Accuracy counts each exit once with its latest verdict: conn-1 has three
        # exits with a claim, one of which the re-attribution confirmed.
        accuracy = {a["connector_id"]: a for a in await repo.exit_accuracy()}
        c1 = accuracy["conn-1"]
        assert c1["exits"] == 3 and c1["country"] == {"claimed": 3, "confirmed": 1, "contradicted": 2, "open": 0}
        assert sorted(c1["breakdown"], key=lambda b: b["observed"]) == [
            {"level": "country", "claimed": "US", "observed": "FR", "exits": 1},
            {"level": "country", "claimed": "US", "observed": "GB", "exits": 1},
        ]
        # The window is by last sighting; the judged exit was last seen on day1 and drops out of a day2 window.
        windowed = {a["connector_id"]: a for a in await repo.exit_accuracy(since=day2)}
        assert windowed["conn-1"]["exits"] == 2 and windowed["conn-1"]["country"]["confirmed"] == 0
        assert windowed["conn-2"]["exits"] == 1
        assert await repo.exit_accuracy(connector_ids=[]) == []

        # An older batch arriving late may add sightings but cannot roll the state back.
        late = [_observation(ip="10.0.0.2", observed_at=day1 - timedelta(hours=1), proxy_id="proxy-old")]
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in late]))
        await db_session.commit()
        (row,), _, _ = await repo.exit_ips(ip="10.0.0.2", connector_ids=["conn-1"])
        assert row["sightings"] == 2 and row["proxy_id"] == "proxy-1" and row["first_seen"] == day1 - timedelta(hours=1)

        # Retention goes by the last sighting; a judgement does not keep an exit alive.
        assert await repo.delete_exit_ips_last_seen_before(datetime(2026, 9, 10)) == 1
        await db_session.commit()
        assert (await repo.exit_ips(connector_ids=["conn-1"]))[1] == 2

        # Deleting the connector takes its aggregates with it.
        assert await ConnectorRepository(db_session).delete("conn-1")
        await db_session.commit()
        assert await repo.exit_ips(connector_ids=["conn-1"]) == ([], 0, True)
        assert await repo.existing_connector_ids({"conn-1", "conn-2", "ghost"}) == {"conn-2"}

    async def test_exit_sharing_counts_overlap_within_the_window(self, db_session: AsyncSession) -> None:
        """An exit is shared when another of the connectors also handed it out in the window, counted once per pair."""
        await _connectors(db_session, "share-a", "share-b", "share-c", "share-d")
        repo = ObservationRepository(db_session)
        old = datetime(2026, 9, 1)
        recent = datetime(2026, 9, 20)
        sightings = [
            # 10.0.0.1 behind a, b and c; a handed it out twice.
            (_observation(connector_id="share-a", ip="10.0.0.1", observed_at=recent), True),
            (_observation(connector_id="share-a", ip="10.0.0.1", observed_at=recent + timedelta(hours=1)), True),
            (_observation(connector_id="share-b", ip="10.0.0.1", observed_at=recent), True),
            (_observation(connector_id="share-c", ip="10.0.0.1", observed_at=recent), True),
            # 10.0.0.2 behind a and b.
            (_observation(connector_id="share-a", ip="10.0.0.2", observed_at=recent), True),
            (_observation(connector_id="share-b", ip="10.0.0.2", observed_at=recent), True),
            # a's own exit.
            (_observation(connector_id="share-a", ip="10.0.0.3", observed_at=recent), True),
            # Behind c in the window, but behind a only before it: not shared.
            (_observation(connector_id="share-c", ip="10.0.0.4", observed_at=recent), True),
            (_observation(connector_id="share-a", ip="10.0.0.4", observed_at=old), True),
            # A connector outside the list is not compared.
            (_observation(connector_id="share-d", ip="10.0.0.3", observed_at=recent), True),
        ]
        await repo.add_exit_ips(ExitSighting.fold(sightings))
        await db_session.commit()

        sharing = await repo.exit_sharing(connector_ids=["share-a", "share-b", "share-c"], since=datetime(2026, 9, 10))
        assert sharing is not None
        assert (sharing["exits"], sharing["shared"]) == (4, 2)
        assert {c["connector_id"]: (c["exits"], c["reused"], c["shared"]) for c in sharing["connectors"]} == {
            "share-a": (3, 1, 2),
            "share-b": (2, 0, 2),
            "share-c": (2, 0, 1),
        }
        assert sorted((p["connector_id"], p["other_id"], p["shared"]) for p in sharing["pairs"]) == [
            ("share-a", "share-b", 2),
            ("share-a", "share-c", 1),
            ("share-b", "share-c", 1),
        ]
        assert await repo.exit_sharing(connector_ids=[], since=old) == {"exits": 0, "shared": 0, "connectors": [], "pairs": []}

    async def test_exit_sharing_spans_more_connectors_than_one_mask_word(self, db_session: AsyncSession) -> None:
        """Connectors past the 63rd land in a further mask word and are compared like the first ones."""
        ids = [f"wide-{n:02d}" for n in range(70)]
        await _connectors(db_session, *ids)
        repo = ObservationRepository(db_session)
        seen = datetime(2026, 9, 20)
        sightings = [
            # 10.0.1.1 behind the first and the last connector, one in each word.
            (_observation(connector_id=ids[0], ip="10.0.1.1", observed_at=seen), True),
            (_observation(connector_id=ids[69], ip="10.0.1.1", observed_at=seen), True),
            (_observation(connector_id=ids[69], ip="10.0.1.1", observed_at=seen + timedelta(hours=1)), True),
            # 10.0.1.2 behind the 63rd and the 64th, either side of the word boundary.
            (_observation(connector_id=ids[62], ip="10.0.1.2", observed_at=seen), True),
            (_observation(connector_id=ids[63], ip="10.0.1.2", observed_at=seen), True),
        ]
        await repo.add_exit_ips(ExitSighting.fold(sightings))
        await db_session.commit()

        sharing = await repo.exit_sharing(connector_ids=ids, since=datetime(2026, 9, 10))
        assert sharing is not None
        assert (sharing["exits"], sharing["shared"]) == (2, 2)
        assert {c["connector_id"]: (c["exits"], c["reused"], c["shared"]) for c in sharing["connectors"]} == {
            ids[0]: (1, 0, 1),
            ids[62]: (1, 0, 1),
            ids[63]: (1, 0, 1),
            ids[69]: (1, 1, 1),
        }
        assert sorted((p["connector_id"], p["other_id"], p["shared"]) for p in sharing["pairs"]) == [
            (ids[0], ids[69], 1),
            (ids[62], ids[63], 1),
        ]

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
        page, total, exact = await repo.recent(connector_id="conn-1", limit=2, offset=0)
        assert [r["ip"] for r in page] == ["10.0.0.4", "10.0.0.3"] and total == 5 and exact
        page, total, _ = await repo.recent(connector_id="conn-1", limit=2, offset=4)
        assert [r["ip"] for r in page] == ["10.0.0.0"] and total == 5
        # Past the end the total is still where the end is, so the pager can step back to it.
        assert await repo.recent(connector_id="conn-1", limit=2, offset=40) == ([], 5, True)

        # The other connector's newer row does not leak into a filtered page,
        # and source and ip filters apply on their own.
        page, total, _ = await repo.recent(source=ObservationSource.DISCOVERY.value)
        assert total == 1 and page[0]["connector_id"] == "other"
        page, total, _ = await repo.recent(ip="10.0.0.2")
        assert total == 1 and page[0]["connector_id"] == "conn-1"
        assert await repo.recent(
            connector_id="conn-1", source=ObservationSource.DISCOVERY.value
        ) == ([], 0, True)

        # Until starts the log at that moment, and the count follows.
        page, total, _ = await repo.recent(connector_id="conn-1", until=base + timedelta(minutes=2))
        assert [r["ip"] for r in page] == ["10.0.0.2", "10.0.0.1", "10.0.0.0"] and total == 3
        assert await repo.count_matching(connector_id="conn-1") == 5
        assert await repo.count_matching(connector_id="conn-1", until=base + timedelta(minutes=2)) == 3

        # The verdict filter is the label the page shows, built from the claim and the nullable verdict.
        assert (await repo.recent(verdict="contradicted"))[1] == 6  # every fixture row conflicts
        assert (await repo.recent(verdict="confirmed"))[1] == 0
        await repo.insert_many(
            [
                _observation(ip="10.0.2.1", claimed_country="GB", country_conflict=False),
                _observation(ip="10.0.2.2", claimed_country="FR", country_conflict=None),  # claimed, no verdict
                _observation(ip="10.0.2.3", claimed_country=None, country_conflict=None),
            ]
        )
        await db_session.commit()
        page, total, _ = await repo.recent(verdict="confirmed")
        assert total == 1 and page[0]["ip"] == "10.0.2.1"
        page, total, _ = await repo.recent(verdict="open")
        assert total == 1 and page[0]["ip"] == "10.0.2.2"
        assert (await repo.recent(verdict="no_claim"))[1] == 1
        assert (await repo.recent(claimed_country="gb"))[1] == 1
        assert (await repo.recent(resolved_country="GB"))[1] == 9

    async def test_page_totals_stop_counting_past_the_cap(self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        """Past the cap the total is a lower bound that slides with the page, so there is always a next page."""
        monkeypatch.setattr(geo_repository, "COUNT_CAP", 3)
        repo = ObservationRepository(db_session)
        base = datetime(2026, 1, 1)
        await repo.insert_many([_observation(observed_at=base + timedelta(minutes=i), ip=f"10.0.0.{i}") for i in range(8)])
        await db_session.commit()

        # Eight rows, counted at most three past the page.
        assert (await repo.recent(limit=2, offset=0))[1:] == (3, False)
        assert (await repo.recent(limit=2, offset=2))[1:] == (5, False)
        # Close enough to the end to reach it: exact again.
        assert (await repo.recent(limit=2, offset=6))[1:] == (8, True)
        # A short page is the end and needs no count.
        assert (await repo.recent(limit=5, offset=5))[1:] == (8, True)
        assert await repo.count_matching() == 8

    async def test_an_exact_count_or_a_page_past_its_time_is_called_off(self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        """Counting a filter no index serves reads the table, as can a page under a value with no recent rows; past the limit each gives up and the session stays usable."""
        await db_session.execute(
            text(
                "INSERT INTO ip_observations (observed_at, source, ip, candidates, instance_id) "
                "SELECT timestamp '2026-01-01' + g * interval '1 second', 'health_check', '10.0.0.1', '[]', '' "
                "FROM generate_series(1, 200000) g"
            )
        )
        await db_session.commit()
        repo = ObservationRepository(db_session)
        # Work the caller has not committed yet survives the count, finished or called off.
        await repo.insert_many([_observation()])
        monkeypatch.setattr(geo_repository, "READ_TIMEOUT_MS", 1)
        assert await repo.count_matching(source="discovery") is None
        assert await repo.recent(source="discovery") is None
        monkeypatch.setattr(geo_repository, "READ_TIMEOUT_MS", 30_000)
        assert await repo.count_matching(source="discovery") == 1
        page = await repo.recent(source="discovery")
        assert page is not None and page[1] == 1
        # None of them leaves its time limit on the transaction.
        assert (await db_session.execute(text("SHOW statement_timeout"))).scalar() == "0"

    async def test_verdict_filter_is_the_country_verdict(self, db_session: AsyncSession) -> None:
        """The verdict label is the country's on both tables; state and city verdicts do not move it."""
        await _connectors(db_session, "conn-1")
        repo = ObservationRepository(db_session)
        rows = [
            # Country right, city wrong: still confirmed at the country.
            _observation(ip="10.1.0.1", claimed_country="US", resolved_country="US", country_conflict=False, claimed_city="new_york", resolved_city="newark", city_conflict=True),
            # Country right, state without a verdict: confirmed at the country.
            _observation(ip="10.1.0.2", claimed_country="US", resolved_country="US", country_conflict=False, claimed_state="NY", state_conflict=None),
            # Every claimed level right: confirmed.
            _observation(ip="10.1.0.3", claimed_country="US", resolved_country="US", country_conflict=False, claimed_state="NY", resolved_state="NY", state_conflict=False, claimed_city="new_york", resolved_city="new_york", city_conflict=False),
            # Country open, city wrong: open at the country.
            _observation(ip="10.1.0.4", claimed_country="US", resolved_country=None, country_conflict=None, claimed_city="new_york", resolved_city="newark", city_conflict=True),
            _observation(ip="10.1.0.5", claimed_country=None, resolved_country="US", country_conflict=None),
        ]

        await repo.insert_many(rows)
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in rows]))
        await db_session.commit()

        def ips(page: list[dict[str, Any]]) -> set[str]:
            return {r["ip"] for r in page}

        for fetch in (repo.recent, repo.exit_ips):
            assert ips((await fetch(verdict="contradicted"))[0]) == set()
            assert ips((await fetch(verdict="open"))[0]) == {"10.1.0.4"}
            assert ips((await fetch(verdict="confirmed"))[0]) == {"10.1.0.1", "10.1.0.2", "10.1.0.3"}
            assert ips((await fetch(verdict="no_claim"))[0]) == {"10.1.0.5"}
            assert (await fetch())[1] == 5

    async def test_a_session_or_instance_id_longer_than_its_column_is_cut(self, db_session: AsyncSession) -> None:
        """A client's ``-sessid-`` can be any length: one too long would fail its whole batch at every delivery."""
        repo = ObservationRepository(db_session)
        assert await repo.insert_many([_observation(session_id="s" * 300, instance_id="i" * 100), _observation()]) == 2
        await db_session.commit()

        rows, _, _ = await repo.recent()
        assert {(row["session_id"], row["instance_id"]) for row in rows} == {("s" * 255, "i" * 64), (None, "")}

    async def test_retention(self, db_session: AsyncSession) -> None:
        repo = ObservationRepository(db_session)
        old = _observation(observed_at=datetime(2020, 1, 1))
        await repo.insert_many([old, _observation()])
        await db_session.commit()
        assert await repo.delete_older_than(utc_now() - timedelta(days=7)) == 1
        await db_session.commit()
        assert await repo.count_matching() == 1

    async def test_the_log_is_planned_for_its_own_filter_values(self, db_session: AsyncSession) -> None:
        """A cached generic plan would sort a busy proxy's every row, or walk the whole history for a rare one."""
        repo = ObservationRepository(db_session)

        async def plan_cache_mode() -> str:
            return str((await db_session.execute(text("SHOW plan_cache_mode"))).scalar())

        await repo.recent(proxy_id="proxy-1")
        assert await plan_cache_mode() == "force_custom_plan"
        await db_session.rollback()
        await repo.count_matching(proxy_id="proxy-1")
        assert await plan_cache_mode() == "force_custom_plan"
        # The setting ends with the transaction, so nothing else on the connection is planned that way.
        await db_session.rollback()
        assert await plan_cache_mode() == "auto"

    async def test_an_exit_s_updates_write_no_index(self, db_session: AsyncSession) -> None:
        """Every batch moves an exit's last_seen and latest state; with neither indexed, each update is HOT."""
        await _connectors(db_session, "conn-1")
        repo = ObservationRepository(db_session)
        await repo.add_exit_ips(ExitSighting.fold([(_observation(ip="10.0.0.1", observed_at=datetime(2026, 9, 1)), True)]))
        await db_session.commit()

        later = _observation(ip="10.0.0.1", observed_at=datetime(2026, 9, 2), proxy_id="proxy-2")
        await repo.add_exit_ips(ExitSighting.fold([(later, True)]))
        assert await repo.apply_judgements([ExitJudgement(proxy_id="proxy-2", connector_id="conn-1", ip="10.0.0.1", claimed_country="GB")]) == 1
        updated, hot = (
            await db_session.execute(
                text("SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_xact_user_tables WHERE relname = 'connector_exit_ips'")
            )
        ).one()
        assert (updated, hot) == (2, 2)


@pytest.mark.usefixtures("db_engine")
class TestStatementSize:
    async def test_exit_upserts_and_judgements_go_out_in_bounded_statements(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """asyncpg binds at most 32767 values per statement; one statement for a 5000-row batch would overflow."""
        await _connectors(db_session, "conn-1")
        monkeypatch.setattr(ObservationRepository, "_EXIT_UPSERT_ROWS", 2)
        monkeypatch.setattr(ObservationRepository, "_JUDGEMENT_ROWS", 1)
        repo = ObservationRepository(db_session)

        day = datetime(2026, 9, 1, 8, 0, 0)
        sightings = [_observation(ip=f"10.9.0.{n}", observed_at=day) for n in range(5)]
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in sightings]))
        # The same exits again add to the rows, whichever statement they land in.
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in sightings]))
        # Two judgements of one row, out of order: the newer stays, and the row counts once.
        applied = await repo.apply_judgements([
            ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.9.0.0", judged_at=day + timedelta(hours=3), claimed_country="DE"),
            ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.9.0.1", judged_at=day + timedelta(hours=2), claimed_country="FR"),
            ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.9.0.0", judged_at=day + timedelta(hours=1), claimed_country="FR"),
        ])
        await db_session.commit()

        assert applied == 2
        rows = {r["ip"]: r for r in (await repo.exit_ips(connector_ids=["conn-1"], limit=1000))[0]}
        assert all(rows[f"10.9.0.{n}"]["sightings"] == 2 for n in range(5))
        assert (rows["10.9.0.0"]["claimed_country"], rows["10.9.0.1"]["claimed_country"]) == ("DE", "FR")


class TestLockOrder:
    async def test_two_workers_writing_the_same_exits_wait_rather_than_deadlock(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two ingest batches sharing exits, the second listing them the other way round; each upserts in key order."""
        await _connectors(db_session, "conn-1")
        low, high = _observation(ip="10.0.0.1"), _observation(ip="10.0.0.2")

        async with db_session_factory() as first, db_session_factory() as second:
            await ObservationRepository(first).add_exit_ips(ExitSighting.fold([(low, True)]))
            waiting = asyncio.create_task(
                ObservationRepository(second).add_exit_ips(ExitSighting.fold([(high, True), (low, True)]))
            )
            # The second batch asks for the low exit first and waits on it, holding nothing the first wants next.
            for _ in range(100):
                if (await db_session.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))).scalar():
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("The second batch never waited on the first")
            await ObservationRepository(first).add_exit_ips(ExitSighting.fold([(high, True)]))
            await first.commit()
            await asyncio.wait_for(waiting, 5)
            await second.commit()

        rows, _, _ = await ObservationRepository(db_session).exit_ips(connector_ids=["conn-1"], limit=10)
        assert {row["ip"]: row["sightings"] for row in rows} == {"10.0.0.1": 2, "10.0.0.2": 2}


class TestPlaceAccuracy:
    async def test_state_and_city_verdicts_per_connector(self, db_session: AsyncSession) -> None:
        await _connectors(db_session, "conn-1")
        repo = ObservationRepository(db_session)

        when = datetime(2026, 9, 20, 8, 0, 0)
        sightings = [
            # City asked and confirmed; state asked and confirmed.
            _observation(ip="10.1.0.1", observed_at=when, claimed_country="GB", resolved_country="GB", country_conflict=False,
                         claimed_state="ENG", claimed_city="london", resolved_state="ENG", resolved_city="london",
                         state_conflict=False, city_conflict=False),
            # Right country, wrong city.
            _observation(ip="10.1.0.2", observed_at=when, claimed_country="GB", resolved_country="GB", country_conflict=False,
                         claimed_city="manchester", resolved_city="london", city_conflict=True),
            # City asked, no city-level answer: unverified.
            _observation(ip="10.1.0.3", observed_at=when, claimed_country="GB", resolved_country="GB", country_conflict=False,
                         claimed_city="leeds"),
            # Wrong state, and the country itself contradicted.
            _observation(ip="10.1.0.4", observed_at=when, claimed_country="GB", resolved_country="FR", country_conflict=True,
                         claimed_state="SCT", resolved_state="IDF", state_conflict=True),
        ]
        await repo.insert_many(sightings)
        await repo.add_exit_ips(ExitSighting.fold([(o, True) for o in sightings]))
        await db_session.commit()

        (row,) = await repo.exit_accuracy(connector_ids=["conn-1"])
        assert row["country"] == {"claimed": 4, "confirmed": 3, "contradicted": 1, "open": 0}
        assert row["state"] == {"claimed": 2, "confirmed": 1, "contradicted": 1, "open": 0}
        assert row["city"] == {"claimed": 3, "confirmed": 1, "contradicted": 1, "open": 1}
        assert sorted((b["level"], b["claimed"], b["observed"]) for b in row["breakdown"]) == [
            ("city", "manchester", "london"),
            ("country", "GB", "FR"),
            ("state", "SCT", "IDF"),
        ]
        # The exit rows carry the place for the views.
        rows, _, _ = await repo.exit_ips(connector_ids=["conn-1"], ip="10.1.0.2")
        assert rows[0]["claimed_city"] == "manchester" and rows[0]["resolved_city"] == "london" and rows[0]["city_conflict"] is True
        page, _, _ = await repo.recent(connector_id="conn-1", ip="10.1.0.1")
        assert page[0]["claimed_state"] == "ENG" and page[0]["state_conflict"] is False
        # A judgement rewrites the place verdict with the rest.
        applied = await repo.apply_judgements([
            ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.1.0.2", claimed_country="GB",
                          resolved_country="GB", country_conflict=False, claimed_city="manchester", resolved_city="manchester", city_conflict=False),
        ])
        await db_session.commit()
        assert applied == 1
        (row,) = await repo.exit_accuracy(connector_ids=["conn-1"])
        assert row["city"]["contradicted"] == 0 and row["city"]["confirmed"] == 2
