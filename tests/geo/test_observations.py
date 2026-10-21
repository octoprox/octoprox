# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the observation pipeline: the recorder's appends to the observations stream, and the workers' ingest into Postgres.

Each worker runs on a Redis client of its own, as a process would, on the session's Redis and Postgres; a test that waits
on reclaims shrinks the claim timings to tenths of a second.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import api.core.consumers as consumers
import api.db.streams as streams
from api.core.consumers import decode_items
from api.core.workers import WorkerName
from api.db.geo_repository import ExitSighting, ObservationRepository
from api.db.redis import (
    OBSERVATION_STREAM_KEY,
    PROXY_LAST_EXIT_KEY,
    PROXY_LAST_EXIT_TTL_SECONDS,
    WORKER_GROUP,
    RedisClient,
)
from api.db.repository import ConnectorRepository, CredentialRepository, ProjectRepository
from api.db.streams import StreamEntry
from api.geo import observations
from api.geo.models import ExitJudgement, IpObservation, ObservationSource
from api.geo.observations import ObservationIngest, ObservationRecorder
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from tests.waits import running, until

T0 = datetime(2026, 10, 17, 12, 0, 0)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _ms(moment: datetime) -> int:
    return int((moment - datetime(1970, 1, 1)).total_seconds() * 1000)


def _observation(**overrides: object) -> IpObservation:
    values: dict[str, object] = {
        "connector_id": "conn-1",
        "proxy_id": "proxy-1",
        "source": ObservationSource.DISCOVERY,
        "ip": "81.2.69.160",
        "claimed_country": "US",
        "resolved_country": "GB",
        "country_conflict": True,
        "observed_at": T0,
    }
    values.update(overrides)
    return IpObservation(**values)  # type: ignore[arg-type]


def _entries(*items: IpObservation | ExitJudgement) -> list[StreamEntry]:
    """Stream entries carrying ``items``, as a read of the observations stream hands them over."""
    return [StreamEntry(f"{n + 1}-0", {"item": item.model_dump_json()}) for n, item in enumerate(items)]


async def _append(redis_client: RedisClient, *items: IpObservation | ExitJudgement) -> None:
    """Append ``items`` to the observations stream, an entry each, as the recorders do."""
    for item in items:
        await redis_client.client.xadd(OBSERVATION_STREAM_KEY, {"item": item.model_dump_json()})


async def _stream(redis_client: RedisClient) -> list[IpObservation | ExitJudgement]:
    """The items on the observations stream, oldest first."""
    return [ObservationIngest.parse(fields["item"]) for _id, fields in await redis_client.client.xrange(OBSERVATION_STREAM_KEY)]


async def _ips(redis_client: RedisClient) -> list[str]:
    return [item.ip for item in await _stream(redis_client)]


async def _last_exit(redis_client: RedisClient, proxy_id: str) -> str | None:
    value: str | None = await redis_client.client.get(PROXY_LAST_EXIT_KEY.format(proxy_id=proxy_id))
    return value


async def _exit(session_factory: async_sessionmaker[AsyncSession], ip: str) -> dict[str, Any]:
    """The exit table's row for ``ip`` on conn-1."""
    async with session_factory() as session:
        (row,), _, _ = await ObservationRepository(session).exit_ips(connector_ids=["conn-1"], ip=ip)
    return row


async def _hand_outs(session_factory: async_sessionmaker[AsyncSession], ip: str) -> int:
    """How many times the exit table counted ``ip`` on conn-1 handed out."""
    return int((await _exit(session_factory, ip))["sightings"])


async def _logged(session_factory: async_sessionmaker[AsyncSession]) -> int | None:
    async with session_factory() as session:
        return await ObservationRepository(session).count_matching()


async def _pending(redis_client: RedisClient) -> int:
    rows = await redis_client.client.xpending_range(OBSERVATION_STREAM_KEY, WORKER_GROUP, "-", "+", 1000)
    return len(rows)


@pytest.fixture
async def connect(test_redis_url: str) -> AsyncIterator[Callable[..., Any]]:
    """Connect a client of its own, as another worker process would have; every one is closed at the end."""
    made: list[RedisClient] = []

    async def client(url: str = test_redis_url, **options: Any) -> RedisClient:
        made.append(RedisClient(url, **options))
        await made[-1].connect()
        return made[-1]

    yield client
    for made_client in made:
        await made_client.close()


@pytest.fixture
async def connector(db_session: AsyncSession) -> None:
    """A project with the static connector conn-1, committed, so the exit table's foreign key holds."""
    project = await ProjectRepository(db_session).create(Project(name="geo-obs", username="geo_obs", password="pw"))
    credential = await CredentialRepository(db_session).create(
        Credential(name="c", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={})
    )
    await ConnectorRepository(db_session).create(
        Connector(
            id="conn-1", name="conn-1", credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
        )
    )
    await db_session.commit()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries idle 0.3 s are reclaimed, checked every 0.1 s, and reads back off 0.05 s doubling to 0.1 s."""
    monkeypatch.setattr(consumers, "CLAIM_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 0.1)
    monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(consumers, "STOP_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.1)


class TestRecorder:
    async def test_what_it_records_is_appended_to_the_observations_stream_an_entry_each(
        self, redis_client: RedisClient
    ) -> None:
        recorder = ObservationRecorder(redis_client)
        recorder.record(_observation(ip="10.0.0.1"))
        recorder.record(ExitJudgement(proxy_id="proxy-1", connector_id="conn-1", ip="10.0.0.1", country_conflict=False))

        assert await recorder.flush() == 2
        assert recorder.name == WorkerName.GEO_OBSERVATION_PUBLISHER  # its row on the System page, and where it runs
        observation, judgement = await _stream(redis_client)
        assert isinstance(observation, IpObservation) and observation.ip == "10.0.0.1"
        assert isinstance(judgement, ExitJudgement) and judgement.country_conflict is False
        assert (recorder.published, recorder.pending) == (2, 0)

    async def test_the_stream_is_capped_and_what_the_cap_trims_is_counted_dropped(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(observations, "OBSERVATION_STREAM_MAXLEN", 10)
        recorder = ObservationRecorder(redis_client, max_buffer=1000)
        for n in range(500):
            recorder.record(_observation(ip=f"10.1.{n // 256}.{n % 256}"))

        assert await recorder.flush() == 500
        length = await redis_client.client.xlen(OBSERVATION_STREAM_KEY)
        # Approximately: Redis trims whole nodes of a hundred entries, oldest first.
        assert length < 200 and recorder.dropped == 500 - length

    async def test_a_full_buffer_lets_its_oldest_item_go_and_without_redis_keeps_the_rest(self) -> None:
        recorder = ObservationRecorder(None, max_buffer=3)
        for n in range(5):
            recorder.record(_observation(ip=f"10.0.0.{n}"))

        assert (recorder.pending, recorder.dropped) == (3, 2) and recorder._buffer[0].ip == "10.0.0.2"
        assert await recorder.flush() == 0 and recorder.pending == 3

    async def test_half_a_buffer_is_sent_at_once_rather_than_at_the_interval(self, redis_client: RedisClient) -> None:
        recorder = ObservationRecorder(redis_client, max_buffer=4, interval=3600)
        async with running(recorder):
            recorder.record(_observation(ip="10.0.0.1"))
            await asyncio.sleep(0.1)
            assert await _ips(redis_client) == []  # a quarter of the buffer waits for the interval
            recorder.record(_observation(ip="10.0.0.2"))
            await until(lambda: recorder.published == 2, timeout=2)

        assert await _ips(redis_client) == ["10.0.0.1", "10.0.0.2"]

    async def test_no_early_send_while_a_part_is_owed(self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """While Redis is in trouble, a send per observation would only hammer it: the loop sends again at its pace."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.1)
        sends: list[int] = []
        send_batch = redis_client.send_batch

        async def send(parts: Any) -> Any:
            sends.append(len(parts))
            if len(sends) == 1:
                raise ConnectionError("Redis went away")
            return await send_batch(parts)

        monkeypatch.setattr(redis_client, "send_batch", send)
        recorder = ObservationRecorder(redis_client, max_buffer=4, interval=3600)
        async with running(recorder):
            recorder.record(_observation(ip="10.0.0.1"))
            recorder.record(_observation(ip="10.0.0.2"))  # half: sent at once, and the send fails
            await until(lambda: recorder.owed == 2, timeout=2)
            recorder.record(_observation(ip="10.0.0.3"))
            recorder.record(_observation(ip="10.0.0.4"))  # half again, with a part owed: nothing sent
            await asyncio.sleep(0.3)
            assert sends == [1]

        # The stop sends what is owed, then the window.
        assert await _ips(redis_client) == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]

    async def test_queued_is_the_stream_s_length(self, redis_client: RedisClient, connect: Any) -> None:
        recorder = ObservationRecorder(redis_client)
        assert await recorder.queued() == 0
        for n in range(3):
            recorder.record(_observation(ip=f"10.0.0.{n}"))
        await recorder.flush()

        assert await recorder.queued() == 3
        assert await ObservationRecorder(None).queued() is None
        unreachable = await connect("redis://127.0.0.1:1/0", request_timeout=0.5, socket_connect_timeout=0.5)
        assert await ObservationRecorder(unreachable).queued() is None

    async def test_a_length_cut_by_a_deadline_is_logged_by_its_type(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text: the log line names its type, and the length is unknown."""
        monkeypatch.setattr(redis_client.client, "xlen", AsyncMock(side_effect=TimeoutError))

        with patch.object(observations, "logger") as log:
            assert await ObservationRecorder(redis_client).queued() is None

        log.warning.assert_called_once_with("Could not read the IP observation stream's length", error="TimeoutError")


@pytest.mark.usefixtures("connector")
class TestIngest:
    async def test_two_workers_ingest_disjoint_batches_each_entry_once(
        self,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        connect: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(observations, "INGEST_BATCH_SIZE", 10)
        insert_many = ObservationRepository.insert_many
        batches: list[int] = []

        async def slow(self: ObservationRepository, rows: list[IpObservation]) -> int:
            batches.append(len(rows))
            await asyncio.sleep(0.02)  # so each worker finds the other's batch in hand
            return await insert_many(self, rows)

        monkeypatch.setattr(ObservationRepository, "insert_many", slow)
        ips = [f"10.2.0.{n}" for n in range(60)]
        await _append(redis_client, *(_observation(proxy_id=None, ip=ip) for ip in ips))
        first = ObservationIngest(await connect(), db_session_factory)
        second = ObservationIngest(await connect(), db_session_factory)
        async with running(first), running(second):
            await until(lambda: first.consumed + second.consumed == 60)

        assert first.consumed > 0 and second.consumed > 0 and max(batches) == 10
        assert first.name == WorkerName.OBSERVATION_INGEST
        assert await _logged(db_session_factory) == 60
        async with db_session_factory() as session:
            rows, _, _ = await ObservationRepository(session).recent(limit=100)
        assert sorted(row["ip"] for row in rows) == sorted(ips)
        assert await redis_client.client.xlen(OBSERVATION_STREAM_KEY) == 0 and await _pending(redis_client) == 0

    async def test_an_older_observation_after_a_newer_one_is_no_hand_out(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ingest = ObservationIngest(redis_client, db_session_factory)
        await ingest._handle(_entries(_observation(ip="10.3.0.2", observed_at=_at(60))))
        # Seen a minute before, ingested after: late, so the proxy's record stays where it is.
        await ingest._handle(_entries(_observation(ip="10.3.0.1", observed_at=_at(0))))

        assert await _hand_outs(db_session_factory, "10.3.0.2") == 1
        assert await _hand_outs(db_session_factory, "10.3.0.1") == 0
        assert await _last_exit(redis_client, "proxy-1") == f"{_ms(_at(60))} 10.3.0.2"
        assert 0 < await redis_client.client.ttl(PROXY_LAST_EXIT_KEY.format(proxy_id="proxy-1")) <= PROXY_LAST_EXIT_TTL_SECONDS
        # A newer one of the first exit is a hand-out back to it.
        await ingest._handle(_entries(_observation(ip="10.3.0.1", observed_at=_at(120))))
        assert await _hand_outs(db_session_factory, "10.3.0.1") == 1
        assert await _last_exit(redis_client, "proxy-1") == f"{_ms(_at(120))} 10.3.0.1"

    async def test_a_batch_is_judged_oldest_first(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Entries reach the stream in the order processes published them, not the order the exits were seen."""
        ingest = ObservationIngest(redis_client, db_session_factory)
        await ingest._handle(
            _entries(_observation(ip="10.3.1.2", observed_at=_at(60)), _observation(ip="10.3.1.1", observed_at=_at(0)))
        )

        assert await _hand_outs(db_session_factory, "10.3.1.1") == 1
        assert await _hand_outs(db_session_factory, "10.3.1.2") == 1
        assert await _last_exit(redis_client, "proxy-1") == f"{_ms(_at(60))} 10.3.1.2"

    async def test_a_redelivered_batch_counts_no_hand_out(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Delivered again after its commit and before its acknowledgement: its log rows twice, its hand-outs once."""
        ingest = ObservationIngest(redis_client, db_session_factory)
        batch = _entries(
            _observation(ip="10.4.0.1", observed_at=_at(0)),
            _observation(ip="10.4.0.2", observed_at=_at(60)),
            _observation(ip="10.4.0.1", observed_at=_at(120)),
            # Another proxy, two exits seen in the same millisecond: the second is not newer, and no hand-out.
            _observation(proxy_id="proxy-2", ip="10.4.0.3", observed_at=_at(0)),
            _observation(proxy_id="proxy-2", ip="10.4.0.4", observed_at=_at(0)),
        )
        await ingest._handle(batch)
        await ingest._handle(batch)

        counts = {ip: await _hand_outs(db_session_factory, ip) for ip in ("10.4.0.1", "10.4.0.2", "10.4.0.3", "10.4.0.4")}
        assert counts == {"10.4.0.1": 2, "10.4.0.2": 1, "10.4.0.3": 1, "10.4.0.4": 0}
        assert await _logged(db_session_factory) == 10  # log rows are never deduplicated

    async def test_two_batches_at_once_count_a_transition_once(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession], connect: Any
    ) -> None:
        """Two gateways saw the proxy move to the same exit; their batches reach two workers at the same moment."""
        first = ObservationIngest(await connect(), db_session_factory)
        second = ObservationIngest(await connect(), db_session_factory)
        await first._handle(_entries(_observation(ip="10.5.0.1", observed_at=_at(0))))

        await asyncio.gather(
            first._handle(_entries(_observation(ip="10.5.0.2", observed_at=_at(60), instance_id="gateway-1"))),
            second._handle(_entries(_observation(ip="10.5.0.2", observed_at=_at(61), instance_id="gateway-2"))),
        )

        assert await _hand_outs(db_session_factory, "10.5.0.1") == 1
        assert await _hand_outs(db_session_factory, "10.5.0.2") == 1
        assert await _last_exit(redis_client, "proxy-1") == f"{_ms(_at(61))} 10.5.0.2"

    async def test_with_no_record_the_exit_table_says_whether_the_exit_was_counted_before(
        self,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a Redis wipe, or for a new or long idle proxy, the exit's holder on record decides."""
        async with db_session_factory() as session:
            held = _observation(proxy_id="proxy-7", ip="10.6.0.7", observed_at=datetime(2026, 9, 1))
            legacy = _observation(proxy_id=None, ip="10.6.0.8", observed_at=datetime(2026, 9, 1))
            await ObservationRepository(session).add_exit_ips(ExitSighting.fold([(held, True), (legacy, True)]))
            await session.commit()
        asked: list[set[tuple[str, str]]] = []
        exit_holders = ObservationRepository.exit_holders

        async def spy(self: ObservationRepository, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
            asked.append(set(keys))
            return await exit_holders(self, keys)

        monkeypatch.setattr(ObservationRepository, "exit_holders", spy)
        ingest = ObservationIngest(redis_client, db_session_factory)
        await ingest._handle(
            _entries(
                _observation(proxy_id="proxy-7", ip="10.6.0.7"),  # its own exit: counted before
                _observation(proxy_id="proxy-8", ip="10.6.0.8"),  # no holder on record: counted before
                _observation(proxy_id="proxy-9", ip="10.6.0.7"),  # another proxy's exit: handed out again
                _observation(proxy_id="proxy-10", ip="10.6.0.10"),  # never seen: a hand-out
                _observation(proxy_id="proxy-8", ip="10.6.0.80", observed_at=_at(1)),  # recorded now: a move
            )
        )

        assert {ip: await _hand_outs(db_session_factory, ip) for ip in ("10.6.0.7", "10.6.0.8", "10.6.0.10", "10.6.0.80")} == {
            "10.6.0.7": 2, "10.6.0.8": 1, "10.6.0.10": 1, "10.6.0.80": 1,
        }
        assert asked == [{("conn-1", "10.6.0.7"), ("conn-1", "10.6.0.8"), ("conn-1", "10.6.0.10")}]
        # From here on Redis knows, and the table is not asked for these proxies.
        await ingest._handle(_entries(_observation(proxy_id="proxy-7", ip="10.6.0.7", observed_at=_at(5))))
        assert asked[1:] == [set()] and await _hand_outs(db_session_factory, "10.6.0.7") == 2

    async def test_exit_rows_and_judgements_only_move_forward(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ingest = ObservationIngest(redis_client, db_session_factory)
        await ingest._handle(_entries(_observation(ip="10.7.0.1", observed_at=_at(60), claimed_country="US", country_conflict=True)))
        # A late sighting of the same exit widens its first seen and leaves its latest state alone.
        await ingest._handle(_entries(_observation(ip="10.7.0.1", observed_at=_at(0), claimed_country="GB", country_conflict=False)))
        row = await _exit(db_session_factory, "10.7.0.1")
        assert (row["first_seen"], row["last_seen"]) == (_at(0), _at(60))
        assert (row["claimed_country"], row["country_conflict"]) == ("US", True)

        def judgement(proxy_id: str, at: datetime) -> ExitJudgement:
            return ExitJudgement(
                proxy_id=proxy_id, connector_id="conn-1", ip="10.7.0.1", claimed_country="DE", country_conflict=False, judged_at=at
            )

        # Older than the last sighting, or by a proxy that does not hold the exit: nothing is rewritten.
        await ingest._handle(_entries(judgement("proxy-1", _at(30)), judgement("proxy-2", _at(90))))
        row = await _exit(db_session_factory, "10.7.0.1")
        assert (row["claimed_country"], row["source"]) == ("US", "discovery")
        await ingest._handle(_entries(judgement("proxy-1", _at(90))))
        row = await _exit(db_session_factory, "10.7.0.1")
        assert (row["claimed_country"], row["country_conflict"], row["source"]) == ("DE", False, "reattribute")
        assert (row["last_seen"], row["sightings"]) == (_at(60), 1)

    async def test_an_observation_with_no_proxy_counts_no_hand_out_and_a_deleted_connector_s_none_reach_the_exits(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ingest = ObservationIngest(redis_client, db_session_factory)
        await ingest._handle(
            _entries(
                _observation(proxy_id=None, ip="10.7.1.1", claimed_country="FR"),
                _observation(connector_id="gone", ip="10.7.1.2"),
                ExitJudgement(proxy_id="proxy-1", connector_id="gone", ip="10.7.1.2", country_conflict=False),
            )
        )

        row = await _exit(db_session_factory, "10.7.1.1")
        assert (row["sightings"], row["claimed_country"], row["proxy_id"]) == (0, "FR", None)
        async with db_session_factory() as session:
            assert await ObservationRepository(session).exit_ips(connector_ids=["gone"]) == ([], 0, True)
        assert await _logged(db_session_factory) == 2  # the log keeps both, with no foreign key to break

    async def test_a_session_id_longer_than_its_column_is_ingested_cut(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await ObservationIngest(redis_client, db_session_factory)._handle(
            _entries(_observation(session_id="s" * 300, instance_id="i" * 100))
        )

        async with db_session_factory() as session:
            (row,), _, _ = await ObservationRepository(session).recent()
        assert (row["session_id"], row["instance_id"]) == ("s" * 255, "i" * 64)

    async def test_malformed_entries_are_completed_with_their_batch(
        self, redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession], quick: None
    ) -> None:
        await _append(redis_client, _observation(ip="10.8.0.1"))
        for fields in ({"item": "not json"}, {"item": json.dumps({"ip": "10.8.0.2"})}, {"other": "no item"}):
            await redis_client.client.xadd(OBSERVATION_STREAM_KEY, fields)
        ingest = ObservationIngest(redis_client, db_session_factory)
        async with running(ingest):
            await until(lambda: ingest.consumed == 4)

        assert await _logged(db_session_factory) == 1
        assert await redis_client.client.xlen(OBSERVATION_STREAM_KEY) == 0 and await _pending(redis_client) == 0

    async def test_a_failing_batch_is_delivered_again_then_given_up(
        self,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        quick: None,
    ) -> None:
        attempts: list[int] = []

        async def failing(self: ObservationRepository, rows: list[IpObservation]) -> int:
            attempts.append(len(rows))
            raise RuntimeError("Postgres went away")

        monkeypatch.setattr(ObservationRepository, "insert_many", failing)
        await _append(redis_client, _observation(ip="10.9.0.1"))
        ingest = ObservationIngest(redis_client, db_session_factory)
        async with running(ingest):
            await until(lambda: ingest.dropped == 1, timeout=10)

        assert attempts == [1] * consumers.MAX_DELIVERIES and ingest.consumed == 0
        assert await redis_client.client.xlen(OBSERVATION_STREAM_KEY) == 0 and await _pending(redis_client) == 0


class TestItems:
    def test_an_item_is_a_judgement_when_its_kind_says_so_else_an_observation(self) -> None:
        good = _observation(ip="1.1.1.1")
        judgement = ExitJudgement(proxy_id="p1", connector_id="c1", ip="1.1.1.1", country_conflict=True)
        entries = [*_entries(good, judgement), StreamEntry("3-0", {"item": "not json"}), StreamEntry("4-0", {"item": "[1]"})]
        entries.append(StreamEntry("5-0", {"item": json.dumps({"ip": "x"})}))

        parsed, malformed = decode_items(entries, ObservationIngest.parse)

        assert parsed == [good, judgement] and [entry.id for entry in malformed] == ["3-0", "4-0", "5-0"]

    def test_exits_fold_per_connector_and_ip(self) -> None:
        early, late = datetime(2026, 9, 21, 10, 0, 0), datetime(2026, 9, 21, 12, 0, 0)
        hc, discovery = ObservationSource.HEALTH_CHECK, ObservationSource.DISCOVERY
        sightings = [
            (IpObservation(observed_at=late, connector_id="c1", source=hc, ip="1.1.1.1", resolved_country="GB"), True),
            (IpObservation(observed_at=early, connector_id="c1", source=discovery, ip="1.1.1.1", resolved_country="DE"), True),
            (IpObservation(observed_at=early, connector_id="c1", source=discovery, ip="1.1.1.2"), True),
            (IpObservation(observed_at=early, connector_id=None, source=ObservationSource.MANUAL, ip="1.1.1.3"), True),
            # Preflight re-verifying an exit the proxy already had: latest state, no hand-out.
            (IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.PREFLIGHT, ip="1.1.1.1", proxy_id="p1"), False),
        ]

        exits = ExitSighting.fold(sightings)

        assert set(exits) == {("c1", "1.1.1.1"), ("c1", "1.1.1.2")}
        first = exits[("c1", "1.1.1.1")]
        assert first.count == 2 and first.first_seen == early and first.last_seen == late and first.country == "GB"
        assert first.source == "preflight" and first.proxy_id == "p1"  # the newest observation sets the state
        assert exits[("c1", "1.1.1.2")].count == 1 and exits[("c1", "1.1.1.2")].country is None
