# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connector service against the real Postgres and Redis test containers.

A static connector, an Oxylabs session pool (a provider that syncs) and an AWS one (a cloud connector the scale job
manages) stand for the ways a write continues, read off the changes, runtime and job streams.
"""

import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.roles import JobKind
from api.db.redis import (
    AUTOSCALER_STATE_KEY,
    QUARANTINE_KEY,
    SCALE_CREATING_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
    RedisClient,
)
from api.db.repository import ConnectorRepository, ProxyRepository
from api.db.streams import ChangeKind
from api.jobs.queue import JobQueue
from api.models.connector import Connector, ConnectorCreate, ConnectorUpdate
from api.models.credential import Credential, CredentialCreate
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.services.connectors import ConnectorService
from api.services.credentials import CredentialService
from api.services.errors import (
    InvalidConfigError,
    NameConflictError,
    NotFoundError,
    WriteRejectedError,
)
from tests.conftest import queued_job_keys
from tests.services.conftest import (
    AWS,
    OXYLABS_POOL,
    STATIC,
    Executed,
    Witness,
    announced,
    forget_announcements,
    runtime_entries,
)

# A rate limit config the validator accepts.
RATE_LIMIT = {"max_requests": 10, "window_seconds": 60, "quarantine_seconds_min": 60, "quarantine_seconds_max": 120}
# A traffic config with a block, a price and a warning line, each field away from its default.
LIMITED = {"limit_bytes": 1000, "action": "block", "reset_day": 2, "warn_percent": 70, "price_per_gb": 1.5, "currency": "EUR"}


async def stored(sessions: async_sessionmaker[AsyncSession], connector_id: str) -> Connector | None:
    async with sessions() as session:
        return await ConnectorRepository(session).get_by_id(connector_id)


async def pending_marks(redis_client: RedisClient) -> dict[str, str]:
    """The connectors marked waiting for a traffic decision, with when it was asked (ISO)."""
    return await redis_client.client.hgetall(TRAFFIC_PENDING_KEY)


async def add_proxies(
    sessions: async_sessionmaker[AsyncSession],
    connector: Connector,
    count: int,
    status: ProxyStatus = ProxyStatus.HEALTHY,
) -> list[Proxy]:
    proxies = [Proxy(host=f"10.0.0.{n}", port=8080, connector_id=connector.id, status=status) for n in range(count)]
    async with sessions() as session:
        await ProxyRepository(session).bulk_create(proxies)
        await session.commit()
    return proxies


class TestCreate:
    async def test_stores_the_validated_configs(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_credential: Credential,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        connector = await connector_service.create(
            project.id,
            ConnectorCreate(
                name="ISP",
                credential_id=static_credential.id,
                config={"countries": ["de", "us"]},
                routing_config={"weight": 3},
                traffic_config={"limit_bytes": 1000, "action": "block"},
            ),
        )

        assert connector.config == {"countries": ["DE", "US"]}
        assert (connector.weight, connector.parsed_traffic_config.limit_bytes) == (3, 1000)
        assert connector.credential_type == STATIC.type
        assert await stored(sessions, connector.id) == connector

    async def test_a_static_connector_starts_no_work(
        self, static_connector: Connector, redis_client: RedisClient
    ) -> None:
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []
        assert await queued_job_keys(redis_client, JobKind.SCALE) == []
        # New, it has carried nothing: no traffic to decide on.
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []

    async def test_a_provider_that_syncs_is_synced_now(self, pool: Connector, redis_client: RedisClient) -> None:
        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]
        assert await queued_job_keys(redis_client, JobKind.SCALE) == []

    async def test_a_cloud_connector_is_scaled_now(self, cloud: Connector, redis_client: RedisClient) -> None:
        assert await queued_job_keys(redis_client, JobKind.SCALE) == [cloud.id]
        assert await queued_job_keys(redis_client, JobKind.SYNC) == []

    async def test_invalid_configs_are_refused_and_nothing_is_stored(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_credential: Credential,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        def isp(**fields: object) -> ConnectorCreate:
            return ConnectorCreate.model_validate({"name": "ISP", "credential_id": static_credential.id, **fields})

        with pytest.raises(InvalidConfigError, match="limit_status"):
            await connector_service.create(project.id, isp(traffic_config={"limit_status": 200}))
        with pytest.raises(InvalidConfigError, match="two-letter ISO 3166-1 alpha-2 code, got 'zz-top'"):
            await connector_service.create(project.id, isp(config={"countries": ["zz-top"]}))

        async with sessions() as session:
            assert await ConnectorRepository(session).get_by_project(project.id) == []

    async def test_the_credential_must_exist_in_the_project(
        self,
        connector_service: ConnectorService,
        credential_service: CredentialService,
        project: Project,
        other_project: Project,
    ) -> None:
        theirs = await credential_service.create(other_project.id, CredentialCreate(name="Theirs", type=STATIC.type))

        with pytest.raises(NotFoundError, match="Credential not found"):
            await connector_service.create(project.id, ConnectorCreate(name="ISP", credential_id="nope"))
        with pytest.raises(WriteRejectedError, match="Credential does not belong to this project"):
            await connector_service.create(project.id, ConnectorCreate(name="ISP", credential_id=theirs.id))
        with pytest.raises(NotFoundError, match="Project not found"):
            await connector_service.create("nope", ConnectorCreate(name="ISP", credential_id=theirs.id))

    async def test_names_are_unique_per_project_whatever_their_case(
        self, connector_service: ConnectorService, project: Project, static_connector: Connector
    ) -> None:
        with pytest.raises(NameConflictError, match="A connector named 'isp' already exists in this project"):
            await connector_service.create(
                project.id, ConnectorCreate(name="isp", credential_id=static_connector.credential_id)
            )


class TestUpdate:
    async def test_applies_the_fields_it_sets_and_returns_what_was_stored(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        edit = ConnectorUpdate(name="Renamed", enabled=False, routing_config={"weight": 5})

        saved = await connector_service.update(project.id, static_connector.id, edit)

        assert (saved.name, saved.enabled, saved.weight) == ("Renamed", False, 5)
        assert saved.updated_at > static_connector.updated_at
        assert await stored(sessions, static_connector.id) == saved

    async def test_a_refused_edit_leaves_the_stored_connector_as_it_was(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """Validated on a loaded copy: the name set beside a bad config does not land (today's route kept it)."""
        edit = ConnectorUpdate(name="Renamed", traffic_config={"limit_status": 200})

        with pytest.raises(InvalidConfigError, match="limit_status"):
            await connector_service.update(project.id, static_connector.id, edit)

        assert await stored(sessions, static_connector.id) == static_connector

    async def test_moves_to_another_credential_of_the_project(
        self,
        connector_service: ConnectorService,
        credential_service: CredentialService,
        project: Project,
        other_project: Project,
        static_connector: Connector,
    ) -> None:
        aws = await credential_service.create(
            project.id, CredentialCreate(name="AWS", type=AWS.type, config=AWS.credential)
        )
        theirs = await credential_service.create(other_project.id, CredentialCreate(name="Theirs", type=STATIC.type))

        # The new config is validated for the new credential's type, and the type is read back from it.
        saved = await connector_service.update(
            project.id, static_connector.id, ConnectorUpdate(credential_id=aws.id, config=AWS.connector)
        )
        assert (saved.credential_id, saved.credential_type, saved.config["max_proxies"]) == (aws.id, "aws", 4)

        with pytest.raises(WriteRejectedError, match="Credential does not belong to this project"):
            await connector_service.update(project.id, static_connector.id, ConnectorUpdate(credential_id=theirs.id))

    async def test_an_edit_syncs_and_scales_again(
        self,
        connector_service: ConnectorService,
        project: Project,
        pool: Connector,
        cloud: Connector,
        redis_client: RedisClient,
    ) -> None:
        await redis_client.client.flushdb()

        await connector_service.update(
            project.id, pool.id, ConnectorUpdate(config={**OXYLABS_POOL.connector, "num_proxies": 3})
        )
        await connector_service.update(
            project.id, cloud.id, ConnectorUpdate(config={**AWS.connector, "max_proxies": 6})
        )

        assert await queued_job_keys(redis_client, JobKind.SYNC) == [pool.id]
        assert await queued_job_keys(redis_client, JobKind.SCALE) == [cloud.id]

    async def test_a_rate_limit_change_lifts_the_connectors_quarantines(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        static_credential: Credential,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """They were tripped under limits that no longer apply.

        Today's update compared the cached connector with itself, so it never lifted them.
        """
        other = await connector_service.create(
            project.id, ConnectorCreate(name="Other", credential_id=static_credential.id)
        )
        ours = await add_proxies(sessions, static_connector, 2)
        theirs = await add_proxies(sessions, other, 1)
        until = time.time() + 600
        await redis_client.client.zadd(QUARANTINE_KEY, {p.id: until for p in [*ours, *theirs]})

        # Another edit leaves them quarantined, and says nothing on the runtime stream.
        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(name="Renamed"))
        assert await redis_client.client.zcard(QUARANTINE_KEY) == 3
        assert await runtime_entries(redis_client) == []

        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(rate_limit_config=RATE_LIMIT))

        assert await redis_client.client.zrange(QUARANTINE_KEY, 0, -1) == [theirs[0].id]
        # The controller is told, so gateways route to them again at once; two proxies, so no one ref.
        assert await runtime_entries(redis_client) == [("quarantine", "")]

    async def test_the_lift_and_its_runtime_entry_go_in_one_multi(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        pipelines: Executed,
    ) -> None:
        """Never one without the other: an entry without the lift, or a lift the controller is never told of."""
        (proxy,) = await add_proxies(sessions, static_connector, 1)
        await redis_client.client.zadd(QUARANTINE_KEY, {proxy.id: time.time() + 600})

        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(rate_limit_config=RATE_LIMIT))

        assert pipelines == [(True, ["ZREM", "XADD"])]
        assert await runtime_entries(redis_client) == [("quarantine", proxy.id)]

    async def test_a_connector_of_another_project_is_not_found(
        self, connector_service: ConnectorService, other_project: Project, static_connector: Connector
    ) -> None:
        with pytest.raises(NotFoundError, match="Connector not found"):
            await connector_service.update(other_project.id, static_connector.id, ConnectorUpdate(name="Mine"))
        with pytest.raises(NotFoundError, match="Connector not found"):
            await connector_service.remove(other_project.id, static_connector.id)
        with pytest.raises(NotFoundError, match="Connector not found"):
            await connector_service.reset_traffic(other_project.id, static_connector.id)


class TestRemove:
    async def test_a_connector_is_deleted_with_its_proxies_and_scale_state(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await add_proxies(sessions, static_connector, 2)
        state = AUTOSCALER_STATE_KEY.format(connector_id=static_connector.id)
        await redis_client.client.hset(state, mapping={"cooldown_until": "0"})

        await connector_service.remove(project.id, static_connector.id)

        assert await stored(sessions, static_connector.id) is None
        async with sessions() as session:
            assert await ProxyRepository(session).get_by_connector(static_connector.id) == []
        assert await redis_client.client.exists(state) == 0

    async def test_a_cloud_connector_waits_for_its_instances_to_be_terminated(
        self,
        connector_service: ConnectorService,
        project: Project,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """The scale job terminates the instances, then deletes the connector."""
        await add_proxies(sessions, cloud, 1)
        await add_proxies(sessions, cloud, 1, ProxyStatus.DRAINING)
        await redis_client.client.flushdb()

        await connector_service.remove(project.id, cloud.id)

        connector = await stored(sessions, cloud.id)
        assert connector is not None
        assert (connector.enabled, connector.pending_deletion) == (False, True)
        async with sessions() as session:
            statuses = {p.status for p in await ProxyRepository(session).get_by_connector(cloud.id)}
        assert statuses == {ProxyStatus.TERMINATING}
        assert await queued_job_keys(redis_client, JobKind.SCALE) == [cloud.id]

    async def test_the_scale_job_deletes_an_emptied_cloud_connector_with_its_scale_state(
        self,
        connector_service: ConnectorService,
        project: Project,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """The autoscaler hash and the instances being created go with the connector, so a later one starts clean."""
        await connector_service.remove(project.id, cloud.id)
        state = AUTOSCALER_STATE_KEY.format(connector_id=cloud.id)
        creating = SCALE_CREATING_KEY.format(connector_id=cloud.id)
        await redis_client.client.hset(state, mapping={"cooldown_until": "0"})
        await redis_client.client.hset(creating, mapping={"octo-1-abcdef": '{"purpose": "scale", "at": 0}'})
        other = SCALE_CREATING_KEY.format(connector_id="other")
        await redis_client.client.hset(other, mapping={"octo-2-abcdef": '{"purpose": "scale", "at": 0}'})

        assert await connector_service.delete(cloud.id) is True

        assert await stored(sessions, cloud.id) is None
        assert await redis_client.client.exists(state, creating) == 0
        assert await redis_client.client.exists(other) == 1
        assert await connector_service.delete(cloud.id) is False


class TestTrafficAndErrors:
    async def test_a_traffic_reset_stores_the_point_usage_counts_from(
        self, connector_service: ConnectorService, project: Project, static_connector: Connector
    ) -> None:
        saved = await connector_service.reset_traffic(project.id, static_connector.id)

        assert saved.traffic_reset_at is not None
        assert saved.traffic_reset_at >= static_connector.created_at

    async def test_a_traffic_reset_asks_for_a_traffic_decision_now_and_marks_it_pending_from_then(
        self, connector_service: ConnectorService, project: Project, static_connector: Connector, redis_client: RedisClient
    ) -> None:
        """The block it lifts is the traffic job's to lift: asked for now, not at the next minute's re-check."""
        before = utc_now()
        await connector_service.reset_traffic(project.id, static_connector.id)

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == [static_connector.id]
        [(connector_id, asked)] = (await pending_marks(redis_client)).items()
        assert connector_id == static_connector.id and before <= datetime.fromisoformat(asked) <= utc_now()

    async def test_the_pending_mark_and_the_run_now_entry_go_in_one_multi(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        pipelines: Executed,
    ) -> None:
        """Never one without the other: a mark no decision was asked for, or a decision the api never shows as awaited."""
        await connector_service.reset_traffic(project.id, static_connector.id)

        assert pipelines == [(True, ["HSET", "XADD"])]

    async def test_a_traffic_limit_edit_asks_for_a_traffic_decision_and_other_edits_do_not(
        self, connector_service: ConnectorService, project: Project, static_connector: Connector, redis_client: RedisClient
    ) -> None:
        """A raised limit lifts the block at once; a rename, or the same limit saved again, decides nothing new."""
        limited = ConnectorUpdate(traffic_config={"limit_bytes": 1000, "action": "block"})
        await connector_service.update(project.id, static_connector.id, limited)
        first = await pending_marks(redis_client)
        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(name="Renamed"))
        await connector_service.update(project.id, static_connector.id, limited)
        assert await pending_marks(redis_client) == first
        await connector_service.update(
            project.id, static_connector.id, ConnectorUpdate(traffic_config={"limit_bytes": 5000, "action": "block"})
        )

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == [static_connector.id] * 2
        # Marked from the second edit on: a decision that read before it does not answer it.
        last = await pending_marks(redis_client)
        assert list(last) == [static_connector.id]
        assert datetime.fromisoformat(last[static_connector.id]) > datetime.fromisoformat(first[static_connector.id])

    @pytest.mark.parametrize(
        "change",
        [{"limit_bytes": 5000}, {"action": "interrupt"}, {"period": "week"}, {"reset_day": 3}],
        ids=["limit", "action", "period", "reset day"],
    )
    async def test_an_edit_of_what_decides_the_block_asks_for_a_traffic_decision_and_marks_it_pending(
        self,
        change: dict[str, Any],
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        redis_client: RedisClient,
    ) -> None:
        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(traffic_config=LIMITED))
        first = await pending_marks(redis_client)

        await connector_service.update(
            project.id, static_connector.id, ConnectorUpdate(traffic_config={**LIMITED, **change})
        )

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == [static_connector.id] * 2
        last = await pending_marks(redis_client)
        assert datetime.fromisoformat(last[static_connector.id]) > datetime.fromisoformat(first[static_connector.id])

    @pytest.mark.parametrize(
        "change",
        [{"price_per_gb": 4.0}, {"currency": "GBP"}, {"warn_percent": 90}, {"limit_status": 429}],
        ids=["price", "currency", "warning line", "limit status"],
    )
    async def test_an_edit_of_what_decides_no_block_asks_for_nothing_and_marks_nothing(
        self,
        change: dict[str, Any],
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        redis_client: RedisClient,
    ) -> None:
        """Usage, spend and the status clients get follow the edit at once; the stored block cannot change with it."""
        await connector_service.update(project.id, static_connector.id, ConnectorUpdate(traffic_config=LIMITED))
        first = await pending_marks(redis_client)

        saved = await connector_service.update(
            project.id, static_connector.id, ConnectorUpdate(traffic_config={**LIMITED, **change})
        )

        assert saved.traffic_config == {**LIMITED, **change}
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == [static_connector.id]
        assert await pending_marks(redis_client) == first

    async def test_a_request_leaves_what_gateways_enforce_to_the_traffic_job(
        self, connector_service: ConnectorService, project: Project, static_connector: Connector, redis_client: RedisClient
    ) -> None:
        """The stored block stands, and the controller hears of nothing, until a worker's decision changes the block."""
        until = (utc_now() + timedelta(days=1)).isoformat()
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, static_connector.id, until)

        await connector_service.reset_traffic(project.id, static_connector.id)
        await connector_service.update(
            project.id, static_connector.id, ConnectorUpdate(traffic_config={"limit_bytes": 5000, "action": "block"})
        )

        assert await redis_client.client.hgetall(TRAFFIC_BLOCKED_KEY) == {static_connector.id: until}
        assert await runtime_entries(redis_client) == []

    async def test_errors_are_counted_in_a_row_and_cleared(
        self, connector_service: ConnectorService, cloud: Connector
    ) -> None:
        first = await connector_service.record_error(cloud.id, "quota exceeded")
        second = await connector_service.record_error(cloud.id, "still exceeded")
        assert first is not None and second is not None
        assert (second.last_error, second.consecutive_errors) == ("still exceeded", 2)
        assert second.last_error_at is not None and first.last_error_at is not None
        assert second.last_error_at >= first.last_error_at
        # An error is not an edit.
        assert second.updated_at == cloud.updated_at

        cleared = await connector_service.record_error(cloud.id, None)
        assert cleared is not None
        assert (cleared.last_error, cleared.last_error_at, cleared.consecutive_errors) == (None, None, 0)

    async def test_an_error_never_undoes_an_edit(
        self, connector_service: ConnectorService, project: Project, cloud: Connector
    ) -> None:
        """One targeted UPDATE of the error columns, where today's write saved the whole cached connector."""
        await connector_service.update(project.id, cloud.id, ConnectorUpdate(name="Renamed"))

        recorded = await connector_service.record_error(cloud.id, "quota exceeded")

        assert recorded is not None and recorded.name == "Renamed"

    async def test_errors_are_never_announced(
        self, connector_service: ConnectorService, cloud: Connector, redis_client: RedisClient
    ) -> None:
        """The feed carries no error field, so no compile could change: a failing vendor would recompile every retry."""
        await forget_announcements(redis_client)

        await connector_service.record_error(cloud.id, "quota exceeded")
        await connector_service.record_error(cloud.id, None)
        await connector_service.record_error(cloud.id, None)

        assert await announced(redis_client) == []

    async def test_an_error_of_a_gone_connector_is_none(self, connector_service: ConnectorService) -> None:
        assert await connector_service.record_error("nope", "quota exceeded") is None


class TestChangeEntries:
    async def test_each_admin_write_appends_one_connector_entry(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_credential: Credential,
        redis_client: RedisClient,
    ) -> None:
        await forget_announcements(redis_client)

        connector = await connector_service.create(
            project.id, ConnectorCreate(name="ISP", credential_id=static_credential.id)
        )
        await connector_service.update(project.id, connector.id, ConnectorUpdate(name="Renamed"))
        await connector_service.reset_traffic(project.id, connector.id)
        await connector_service.remove(project.id, connector.id)

        assert await announced(redis_client) == [("connector", connector.id)] * 4

    async def test_a_cloud_connector_is_announced_when_marked_and_when_the_scale_job_deletes_it(
        self, connector_service: ConnectorService, project: Project, cloud: Connector, redis_client: RedisClient
    ) -> None:
        await forget_announcements(redis_client)

        await connector_service.remove(project.id, cloud.id)
        assert await connector_service.delete(cloud.id) is True
        # Already gone: nothing changed, nothing to announce.
        assert await connector_service.delete(cloud.id) is False

        assert await announced(redis_client) == [("connector", cloud.id)] * 2

    async def test_each_entry_follows_its_commit(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jobs: JobQueue,
        redis_client: RedisClient,
        registry: ProviderRegistry,
        project: Project,
        static_credential: Credential,
    ) -> None:
        """A consumer woken by an entry reads Postgres: the write must be there by then, or it is missed."""
        witness = Witness(redis_client, lambda ref: stored(sessions, ref))
        service = ConnectorService(sessions, jobs, redis_client, registry, changes=witness)

        connector = await service.create(project.id, ConnectorCreate(name="ISP", credential_id=static_credential.id))
        renamed = await service.update(project.id, connector.id, ConnectorUpdate(name="Renamed"))
        await service.remove(project.id, connector.id)

        assert witness.seen == [
            (ChangeKind.CONNECTOR, connector.id, connector),
            (ChangeKind.CONNECTOR, connector.id, renamed),
            (ChangeKind.CONNECTOR, connector.id, None),
        ]

    async def test_a_refused_write_announces_nothing(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        redis_client: RedisClient,
    ) -> None:
        await forget_announcements(redis_client)

        with pytest.raises(InvalidConfigError):
            await connector_service.update(
                project.id, static_connector.id, ConnectorUpdate(traffic_config={"limit_status": 200})
            )
        with pytest.raises(NameConflictError):
            await connector_service.create(
                project.id, ConnectorCreate(name="isp", credential_id=static_connector.credential_id)
            )
        with pytest.raises(NotFoundError):
            await connector_service.remove(project.id, "nope")

        assert await announced(redis_client) == []
