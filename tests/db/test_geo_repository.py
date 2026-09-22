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
    GeoSourceKind,
    GeoVendor,
    IpObservation,
    ObservationSource,
)
from api.geo.observations import ObservationFlusher, ObservationRecorder
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import ProxyStatus


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
        ]
        await repo.add_exit_ips(aggregate_exits([(o, True) for o in first]))
        await repo.add_exit_ips(aggregate_exits([(o, True) for o in second]))
        # A later re-attribution of an existing exit by its holder rewrites its
        # verdict (claim now confirmed) and nothing else: no hand-out, no sighting time.
        applied = await repo.apply_judgements(
            [
                ExitJudgement(
                    proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.2",
                    claimed_country="GB", resolved_country="GB", conflict=False,
                ),
                # An exit never sighted has no row to judge; nothing is created.
                ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.42", claimed_country="GB"),
                # Computed before the exit's last sighting (a late arrival or a stale
                # read): the sighting saw the proxy's state later, so it stands.
                ExitJudgement(
                    proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.1",
                    judged_at=day2 - timedelta(minutes=1), claimed_country="US", resolved_country="US", conflict=False,
                ),
                # Another slot of the connector sharing the exit, with its own claim: the
                # row's verdict belongs to its holder, so this one is dropped.
                ExitJudgement(
                    proxy_id="proxy-9", connector_id="conn-1", ip="10.0.0.3",
                    claimed_country="DE", resolved_country="GB", conflict=True,
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

        rows, total = await repo.exit_ips(connector_ids=["conn-1"])
        assert total == 3 and [r["ip"] for r in rows][-1] == "10.0.0.2"  # last seen day1; the judgement moved nothing
        by_ip = {r["ip"]: r for r in rows}
        reused = by_ip["10.0.0.1"]
        assert reused["sightings"] == 2 and reused["first_seen"] == day1 and reused["last_seen"] == day2
        assert reused["country"] == "FR" and reused["conflict"] is True and reused["source"] == "discovery"  # the older judgement was dropped
        third = by_ip["10.0.0.3"]
        assert third["claimed_country"] == "US" and third["source"] == "discovery"  # the non-holder's judgement was dropped
        # The judgement rewrote the verdict but not the count or the sighting times.
        refreshed = by_ip["10.0.0.2"]
        assert refreshed["sightings"] == 1 and refreshed["source"] == "reattribute"
        assert refreshed["claimed_country"] == "GB" and refreshed["conflict"] is False
        # The holder is the proxy whose sighting was most recent; a judgement does not change it.
        assert refreshed["proxy_id"] == "proxy-1" and refreshed["last_seen"] == day1 and refreshed["first_seen"] == day1

        # Paging and the view's filters.
        page, total = await repo.exit_ips(connector_ids=["conn-1", "conn-2"], limit=2, offset=2)
        assert total == 4 and len(page) == 2
        assert await repo.exit_ips(connector_ids=[]) == ([], 0)
        assert (await repo.exit_ips(verdict="confirmed"))[1] == 1
        assert (await repo.exit_ips(verdict="contradicted"))[1] == 3
        assert (await repo.exit_ips(country="fr"))[1] == 1
        assert (await repo.exit_ips(ip="10.0.0.1"))[1] == 2
        assert (await repo.exit_ips(proxy_id="proxy-1"))[1] == 4 and (await repo.exit_ips(proxy_id="proxy-9"))[1] == 0  # both connectors' rows

        # Accuracy counts each exit once with its latest verdict: conn-1 has three
        # exits with a claim, one of which the re-attribution confirmed.
        accuracy = {a["connector_id"]: a for a in await repo.exit_accuracy()}
        c1 = accuracy["conn-1"]
        assert (c1["exits"], c1["claimed"], c1["confirmed"], c1["contradicted"], c1["uncertain"]) == (3, 3, 1, 2, 0)
        assert c1["breakdown"] == [{"claimed_country": "US", "observed_country": "FR", "exits": 1}, {"claimed_country": "US", "observed_country": "GB", "exits": 1}] or \
            c1["breakdown"] == [{"claimed_country": "US", "observed_country": "GB", "exits": 1}, {"claimed_country": "US", "observed_country": "FR", "exits": 1}]
        # The window is by last sighting; the judged exit was last seen on day1 and drops out of a day2 window.
        windowed = {a["connector_id"]: a for a in await repo.exit_accuracy(since=day2)}
        assert windowed["conn-1"]["exits"] == 2 and windowed["conn-1"]["confirmed"] == 0
        assert windowed["conn-2"]["exits"] == 1
        assert await repo.exit_accuracy(connector_ids=[]) == []

        # An older batch arriving late may add sightings but cannot roll the state back.
        late = [_observation(ip="10.0.0.2", observed_at=day1 - timedelta(hours=1), proxy_id="proxy-old")]
        await repo.add_exit_ips(aggregate_exits([(o, True) for o in late]))
        await db_session.commit()
        (row,), _ = await repo.exit_ips(ip="10.0.0.2", connector_ids=["conn-1"])
        assert row["sightings"] == 2 and row["proxy_id"] == "proxy-1" and row["first_seen"] == day1 - timedelta(hours=1)

        # Retention goes by the last sighting; a judgement does not keep an exit alive.
        assert await repo.delete_exit_ips_last_seen_before(datetime(2026, 9, 10)) == 1
        await db_session.commit()
        assert (await repo.exit_ips(connector_ids=["conn-1"]))[1] == 2

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
        # The flusher annotates the status hash the health checker owns; give proxy-1 one.
        await redis_client.set_proxy_status("proxy-1", ProxyStatus.HEALTHY)
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
        # The last counted exit went into the proxy's status hash, next to its health fields.
        assert await redis_client.client.hget("proxy:status:proxy-1", "exit_ip") == "81.2.69.161"
        status = await redis_client.get_proxy_status("proxy-1")
        assert status is not None and status["status"] == ProxyStatus.HEALTHY

        # The same exit reported again, by anyone, any time later: one raw row more, no hand-out.
        recorder.record(_observation(ip="81.2.69.161", instance_id="another"))
        await recorder.publish()
        assert await flusher.flush_once() == 1
        (row,), _ = await ObservationRepository(db_session).exit_ips(connector_ids=["conn-1"], ip="81.2.69.161")
        assert row["sightings"] == 1
        # A different exit for the same proxy is a hand-out and moves the recorded exit.
        recorder.record(_observation(ip="81.2.69.170"))
        await recorder.publish()
        assert await flusher.flush_once() == 1
        (row,), _ = await ObservationRepository(db_session).exit_ips(connector_ids=["conn-1"], ip="81.2.69.170")
        assert row["sightings"] == 1
        assert await redis_client.client.hget("proxy:status:proxy-1", "exit_ip") == "81.2.69.170"
        # A judgement of that exit travels the same list: verdict rewritten, no log row, no sighting.
        recorder.record(ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="81.2.69.170", claimed_country="US", resolved_country="GB", conflict=True))
        recorder.record(ExitJudgement(proxy_id="proxy-x", connector_id="gone", ip="81.2.69.170"))  # deleted connector: skipped
        await recorder.publish()
        assert await flusher.flush_once() == 1  # one verdict rewritten; the skipped judgement is not work done
        (row,), _ = await ObservationRepository(db_session).exit_ips(connector_ids=["conn-1"], ip="81.2.69.170")
        assert row["sightings"] == 1 and row["conflict"] is True and row["source"] == "reattribute"
        await redis_client.client.delete("proxy:status:proxy-1")
        assert await ObservationRepository(db_session).exit_ips(connector_ids=["gone"]) == ([], 0)
        assert await redis_client.client.llen(GEO_OBSERVATIONS_KEY) == 0
        assert await flusher.flush_once() == 0

        assert await repo.count() == before + 5  # three sightings, then the repeat and the move
        accuracy = {a["connector_id"]: a for a in await repo.exit_accuracy(connector_ids=["conn-1"])}
        assert accuracy["conn-1"]["claimed"] >= 2
        assert await repo.exit_accuracy(connector_ids=["gone"]) == []

    async def test_exit_on_record_is_not_counted_again_when_redis_knows_nothing(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        db_session: AsyncSession,
    ) -> None:
        """After an upgrade or a Redis loss the exit table, not a seed, says what was counted before."""
        await _connectors(db_session, "conn-1")
        await redis_client.client.delete(GEO_OBSERVATIONS_KEY)
        await redis_client.client.delete("proxy:status:proxy-7", "proxy:status:proxy-8", "proxy:status:proxy-9")
        for proxy_id in ("proxy-7", "proxy-8", "proxy-9"):
            await redis_client.set_proxy_status(proxy_id, ProxyStatus.HEALTHY)
        from api.geo.observations import aggregate_exits

        repo = ObservationRepository(db_session)
        # Two exits already on record from before: one held by proxy-7, one from before per-proxy state existed.
        held = _observation(proxy_id="proxy-7", ip="10.0.0.7", observed_at=datetime(2026, 9, 1))
        legacy = _observation(proxy_id=None, ip="10.0.0.8", observed_at=datetime(2026, 9, 1))
        await repo.add_exit_ips(aggregate_exits([(held, True), (legacy, True)]))
        await db_session.commit()

        recorder = ObservationRecorder(redis_client)
        recorder.record(_observation(proxy_id="proxy-7", ip="10.0.0.7", source=ObservationSource.HEALTH_CHECK))  # same holder
        recorder.record(_observation(proxy_id="proxy-8", ip="10.0.0.8", source=ObservationSource.HEALTH_CHECK))  # legacy row
        recorder.record(_observation(proxy_id="proxy-9", ip="10.0.0.7", source=ObservationSource.HEALTH_CHECK))  # another proxy: reuse
        recorder.record(_observation(proxy_id="proxy-8", ip="10.0.0.80", source=ObservationSource.HEALTH_CHECK))  # brand new exit
        # A sighting of a proxy removed before the flush (its status hash is gone): counted
        # for the connector's history, but no bookkeeping is written back, or the hash would return.
        recorder.record(_observation(proxy_id="proxy-gone", ip="10.0.0.99", source=ObservationSource.HEALTH_CHECK))
        await recorder.publish()
        flusher = ObservationFlusher(db_session_factory, redis_client, "inst", retention_days=lambda: 7)
        assert await flusher.flush_once() == 5
        assert not await redis_client.client.exists("proxy:status:proxy-gone")
        rows = {r["ip"]: r for r in (await repo.exit_ips(connector_ids=["conn-1"]))[0]}
        assert rows["10.0.0.7"]["sightings"] == 2  # held before by proxy-7 (1), handed to proxy-9 (+1)
        assert rows["10.0.0.8"]["sightings"] == 1  # legacy row, holder unknown: not counted again
        assert rows["10.0.0.80"]["sightings"] == 1
        # From here on Redis knows, and the table is not consulted for these proxies.
        assert await redis_client.client.hget("proxy:status:proxy-7", "exit_ip") == "10.0.0.7"
        assert await redis_client.client.hget("proxy:status:proxy-8", "exit_ip") == "10.0.0.80"
        await redis_client.client.delete("proxy:status:proxy-7", "proxy:status:proxy-8", "proxy:status:proxy-9")
