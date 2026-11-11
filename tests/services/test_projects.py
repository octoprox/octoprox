# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the project service against the real Postgres and Redis test containers.

What a write stores is read back with the repositories, what it announces and starts off the changes and job streams,
and what it deletes in Redis key by key.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.audit import Actor
from api.core.roles import JobKind
from api.core.stats import FailureKind, MetricDelta
from api.db.redis import (
    FAILURE_LOG_DATA_KEY,
    FAILURE_LOG_KEY,
    MITM_REQUESTS_KEY,
    PROJECT_METRICS_KEY,
    RedisClient,
)
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.streams import ChangeKind
from api.geo.models import ConflictRule, ContradictedAction
from api.jobs.queue import JobQueue
from api.models.audit import MASK
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import (
    MitmBrowser,
    MitmEngine,
    MitmMode,
    Project,
    ProjectCreate,
    ProjectUpdate,
)
from api.models.proxy import Proxy, ProxyStatus
from api.services.connectors import ConnectorService
from api.services.errors import NameConflictError, NotFoundError, WriteRejectedError
from api.services.projects import ProjectService
from tests.conftest import queued_job_keys
from tests.services.conftest import (
    ACTOR_ID,
    ACTOR_NAME,
    OXYLABS_POOL,
    REQUEST_ID,
    Connect,
    Witness,
    announced,
    audit_entries,
    audit_text,
    forget_announcements,
)


async def stored(sessions: async_sessionmaker[AsyncSession], project_id: str) -> Project | None:
    async with sessions() as session:
        return await ProjectRepository(session).get_by_id(project_id)


def create(**overrides: Any) -> ProjectCreate:
    return ProjectCreate(**{"name": "Acme", "username": "acme", "password": "pw", **overrides})


class TestCreate:
    async def test_stores_the_project(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession], actor: Actor
    ) -> None:
        project = await project_service.create(
            create(routing_strategy="sticky", location_preflight=True, location_session_move=True, exit_headers=True),
            actor,
        )

        assert await stored(sessions, project.id) == project
        assert (project.routing_strategy, project.location_preflight, project.exit_headers) == ("sticky", True, True)
        assert project.location_session_move is True

    async def test_an_unknown_routing_strategy_is_refused(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession], actor: Actor
    ) -> None:
        with pytest.raises(WriteRejectedError, match="Unknown strategy: fastest"):
            await project_service.create(create(routing_strategy="fastest"), actor)

        async with sessions() as session:
            assert await ProjectRepository(session).get_all() == []

    async def test_a_taken_username_or_name_is_a_name_conflict(
        self, project_service: ProjectService, actor: Actor
    ) -> None:
        """Both are unique in Postgres; a duplicate was a 500 before the service translated it."""
        await project_service.create(create(), actor)

        with pytest.raises(NameConflictError, match="Project with username 'acme' already exists"):
            await project_service.create(create(name="Other"), actor)
        with pytest.raises(NameConflictError, match="Project with name 'Acme' already exists"):
            await project_service.create(create(username="other"), actor)


class TestUpdate:
    async def test_applies_the_fields_it_sets_and_returns_what_was_stored(
        self,
        project_service: ProjectService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        saved = await project_service.update(project.id, ProjectUpdate(name="Renamed", description="New"), actor)

        assert (saved.name, saved.description, saved.username) == ("Renamed", "New", "acme")
        assert saved.updated_at > project.updated_at
        assert await stored(sessions, project.id) == saved

    async def test_an_empty_string_clears_an_override(
        self, project_service: ProjectService, project: Project, actor: Actor
    ) -> None:
        await project_service.update(
            project.id, ProjectUpdate(location_conflict_rule=ConflictRule.ANY, location_city_radius_km=50), actor
        )

        cleared = ProjectUpdate(location_conflict_rule="", location_city_radius_km="")
        saved = await project_service.update(project.id, cleared, actor)

        assert (saved.location_conflict_rule, saved.location_city_radius_km) == (None, None)

    async def test_interception_fields_the_mode_does_not_use_are_cleared(
        self, project_service: ProjectService, project: Project, actor: Actor
    ) -> None:
        override = ProjectUpdate(
            tls_mitm_mode=MitmMode.OVERRIDE_UA, tls_mitm_engine=MitmEngine.RNET, tls_mitm_browser=MitmBrowser.FIREFOX
        )
        assert (await project_service.update(project.id, override, actor)).tls_mitm_browser == MitmBrowser.FIREFOX

        # The engine stays from the stored row; match_ua takes no browser.
        matched = await project_service.update(project.id, ProjectUpdate(tls_mitm_mode=MitmMode.MATCH_UA), actor)
        assert (matched.tls_mitm_engine, matched.tls_mitm_browser) == (MitmEngine.RNET, None)

        off = await project_service.update(project.id, ProjectUpdate(tls_mitm_mode=MitmMode.OFF), actor)
        assert (off.tls_mitm_engine, off.tls_mitm_browser) == (None, None)

    @pytest.mark.parametrize(
        ("update", "message"),
        [
            (ProjectUpdate(name="Renamed", tls_mitm_mode=MitmMode.MATCH_UA), "tls_mitm_engine is required"),
            (
                ProjectUpdate(name="Renamed", tls_mitm_mode=MitmMode.OVERRIDE_UA, tls_mitm_engine=MitmEngine.RNET),
                "tls_mitm_browser is required",
            ),
            (ProjectUpdate(name="Renamed", routing_strategy="fastest"), "Unknown strategy"),
        ],
    )
    async def test_a_refused_edit_leaves_the_stored_project_as_it_was(
        self,
        project_service: ProjectService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        update: ProjectUpdate,
        message: str,
        actor: Actor,
    ) -> None:
        """Validated on a loaded copy: nothing of the edit reaches Postgres (today's routes edited the cache first)."""
        with pytest.raises(WriteRejectedError, match=message):
            await project_service.update(project.id, update, actor)

        assert await stored(sessions, project.id) == project

    async def test_a_taken_username_is_a_name_conflict(
        self,
        project_service: ProjectService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        await project_service.create(create(name="Other", username="other"), actor)

        with pytest.raises(NameConflictError, match="Project with username 'other' already exists"):
            await project_service.update(project.id, ProjectUpdate(name="Renamed", username="other"), actor)
        assert await stored(sessions, project.id) == project

    async def test_an_unknown_project_is_not_found(self, project_service: ProjectService, actor: Actor) -> None:
        with pytest.raises(NotFoundError, match="Project not found"):
            await project_service.update("nope", ProjectUpdate(name="Renamed"), actor)

    async def test_a_change_to_how_locations_are_judged_requests_reattribution(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient, actor: Actor
    ) -> None:
        await project_service.update(project.id, ProjectUpdate(location_contradicted=ContradictedAction.REFUSE), actor)

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == [f"project:{project.id}"]

    async def test_other_edits_request_no_reattribution(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient, actor: Actor
    ) -> None:
        """Preflight decides whether sessions are checked, not how a row is judged."""
        await project_service.update(project.id, ProjectUpdate(name="Renamed", location_preflight=True), actor)
        # The same rule again changes nothing.
        await project_service.update(
            project.id, ProjectUpdate(location_contradicted=project.location_contradicted), actor
        )

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == []


class TestDelete:
    async def test_cascades_to_everything_the_project_holds(
        self,
        project_service: ProjectService,
        project: Project,
        static_credential: Credential,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        async with sessions() as session:
            await ProxyRepository(session).create(Proxy(host="10.0.0.1", port=8080, connector_id=static_connector.id))
            await session.commit()

        assert await project_service.delete(project.id, actor) is True

        async with sessions() as session:
            assert await ProjectRepository(session).get_by_id(project.id) is None
            assert await CredentialRepository(session).get_by_project(project.id) == []
            assert await ConnectorRepository(session).get_by_project(project.id) == []
            assert await ProxyRepository(session).get_by_project(project.id) == []

    async def test_a_project_with_a_cloud_connector_waits_for_the_scale_job_to_terminate_its_instances(
        self,
        project_service: ProjectService,
        project: Project,
        cloud: Connector,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        actor: Actor,
    ) -> None:
        """The cloud connector is removed as its own delete removes it; the rest goes now."""
        async with sessions() as session:
            await ProxyRepository(session).create(Proxy(host="10.0.0.1", port=8080, connector_id=cloud.id))
            await session.commit()
        await redis_client.client.flushdb()

        assert await project_service.delete(project.id, actor) is False

        async with sessions() as session:
            kept = await ProjectRepository(session).get_by_id(project.id)
            connectors = await ConnectorRepository(session).get_by_project(project.id)
            proxies = await ProxyRepository(session).get_by_project(project.id)
        assert kept is not None and kept.pending_deletion
        assert [(c.id, c.enabled, c.pending_deletion) for c in connectors] == [(cloud.id, False, True)]
        assert [p.status for p in proxies] == [ProxyStatus.TERMINATING]
        assert await queued_job_keys(redis_client, JobKind.SCALE) == [cloud.id]

    async def test_asked_again_once_no_connector_is_left_it_goes(
        self,
        project_service: ProjectService,
        project: Project,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        """The way out when the connector's row went by hand, or the scale run stopped between its deletes."""
        assert await project_service.delete(project.id, actor) is False
        async with sessions() as session:
            await ConnectorRepository(session).delete(cloud.id)
            await session.commit()

        assert await project_service.delete(project.id, actor) is True
        assert await stored(sessions, project.id) is None

    async def test_a_project_being_deleted_takes_no_new_connector(
        self, project_service: ProjectService, project: Project, cloud: Connector, connect: Connect, actor: Actor
    ) -> None:
        await project_service.delete(project.id, actor)

        with pytest.raises(WriteRejectedError, match="being deleted"):
            await connect(project, "Pool", OXYLABS_POOL)

    async def test_only_a_project_pending_deletion_is_deleted_once_emptied(
        self, project_service: ProjectService, project: Project, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await project_service.delete_if_emptied(project.id) is False
        assert await stored(sessions, project.id) is not None

    async def test_deletes_the_captures_and_the_failure_log_and_nothing_else(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient, actor: Actor
    ) -> None:
        """Keyed deletes only: the metrics window goes with the next flush, which drops a gone project's counts."""
        owned = [
            MITM_REQUESTS_KEY.format(project_id=project.id),
            *(FAILURE_LOG_KEY.format(project_id=project.id, kind=kind.value) for kind in FailureKind),
            *(FAILURE_LOG_DATA_KEY.format(project_id=project.id, kind=kind.value) for kind in FailureKind),
        ]
        for key in owned:
            await redis_client.client.set(key, "x")
        window = PROJECT_METRICS_KEY.format(project_id=project.id)
        await redis_client.client.hset(window, mapping=MetricDelta(request_count=1).to_dict())

        await project_service.delete(project.id, actor)

        assert await redis_client.client.exists(*owned) == 0
        assert await redis_client.client.exists(window) == 1

    async def test_an_unknown_project_is_not_found(self, project_service: ProjectService, actor: Actor) -> None:
        with pytest.raises(NotFoundError, match="Project not found"):
            await project_service.delete("nope", actor)


class TestChangeEntries:
    async def test_each_write_appends_one_project_entry(
        self, project_service: ProjectService, redis_client: RedisClient, actor: Actor
    ) -> None:
        project = await project_service.create(create(), actor)
        await project_service.update(project.id, ProjectUpdate(name="Renamed"), actor)
        await project_service.delete(project.id, actor)

        # A delete is two writes: the mark, then the delete.
        assert await announced(redis_client) == [("project", project.id)] * 4

    async def test_each_entry_follows_its_commit(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jobs: JobQueue,
        redis_client: RedisClient,
        connector_service: ConnectorService,
        actor: Actor,
    ) -> None:
        """A consumer woken by an entry reads Postgres: the write must be there by then, or it is missed."""
        witness = Witness(redis_client, lambda ref: stored(sessions, ref))
        service = ProjectService(sessions, jobs, redis_client, changes=witness, connectors=connector_service)

        project = await service.create(create(), actor)
        renamed = await service.update(project.id, ProjectUpdate(name="Renamed"), actor)
        await service.delete(project.id, actor)

        marked = witness.seen[2][2]
        assert isinstance(marked, Project) and marked.pending_deletion
        assert witness.seen == [
            (ChangeKind.PROJECT, project.id, project),
            (ChangeKind.PROJECT, project.id, renamed),
            (ChangeKind.PROJECT, project.id, marked),
            (ChangeKind.PROJECT, project.id, None),
        ]

    async def test_a_refused_write_announces_nothing(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient, actor: Actor
    ) -> None:
        await forget_announcements(redis_client)

        with pytest.raises(WriteRejectedError):
            await project_service.create(create(name="Other", username="other", routing_strategy="fastest"), actor)
        with pytest.raises(NameConflictError):
            await project_service.create(create(name="Other"), actor)
        with pytest.raises(WriteRejectedError):
            await project_service.update(project.id, ProjectUpdate(routing_strategy="fastest"), actor)
        with pytest.raises(NotFoundError):
            await project_service.update("nope", ProjectUpdate(name="Renamed"), actor)
        with pytest.raises(NotFoundError):
            await project_service.delete("nope", actor)

        assert await announced(redis_client) == []


class TestAuditLog:
    async def test_a_create_records_each_field_set_by_whom_with_the_password_masked(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession], actor: Actor
    ) -> None:
        project = await project_service.create(create(description="Shop", exit_headers=True), actor)

        [entry] = await audit_entries(sessions)
        assert (entry.action, entry.entity_type, entry.entity_id, entry.entity_name, entry.project_id) == (
            "created",
            "project",
            project.id,
            "Acme",
            project.id,
        )
        assert (entry.actor_id, entry.actor, entry.request_id, entry.details) == (ACTOR_ID, ACTOR_NAME, REQUEST_ID, {})
        assert entry.changes["password"] == [None, MASK]
        assert (entry.changes["description"], entry.changes["exit_headers"]) == ([None, "Shop"], [None, True])
        # Unset fields, the delete mark and the stamps are not recorded.
        assert not entry.changes.keys() & {"tls_mitm_engine", "pending_deletion", "created_at", "updated_at", "id"}

    async def test_an_edit_records_what_it_changed_and_one_that_changes_nothing_records_nothing(
        self, project_service: ProjectService, project: Project, sessions: async_sessionmaker[AsyncSession], actor: Actor
    ) -> None:
        edit = ProjectUpdate(name="Renamed", location_preflight=True)
        await project_service.update(project.id, edit, actor)
        # Saved again as it stands: the save changed nothing.
        await project_service.update(project.id, edit, actor)

        created, *edited = await audit_entries(sessions, project.id)
        assert created.action == "created"
        assert [(e.action, e.entity_name, e.changes) for e in edited] == [
            ("updated", "Renamed", {"name": ["Acme", "Renamed"], "location_preflight": [False, True]}),
        ]

    async def test_the_password_is_recorded_as_set_changed_or_cleared_and_never_stored(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession], actor: Actor
    ) -> None:
        project = await project_service.create(create(password="first-secret"), actor)
        await project_service.update(project.id, ProjectUpdate(password="second-secret"), actor)
        await project_service.update(project.id, ProjectUpdate(password=""), actor)

        assert [e.changes["password"] for e in await audit_entries(sessions, project.id)] == [
            [None, MASK],
            [MASK, MASK],
            [MASK, None],
        ]
        stored = await audit_text(sessions)
        assert "first-secret" not in stored and "second-secret" not in stored

    async def test_a_delete_is_recorded_as_it_marks_the_project_then_each_connector_it_removes(
        self,
        project_service: ProjectService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        """Nothing to wait for, so the project goes at once (the 204); the credential goes by cascade, unrecorded."""
        assert await project_service.delete(project.id, actor) is True

        deleted = [e for e in await audit_entries(sessions) if e.action == "deleted"]
        assert [(e.entity_type, e.entity_id, e.entity_name, e.project_id) for e in deleted] == [
            ("project", project.id, "Acme", project.id),
            ("connector", static_connector.id, "ISP", project.id),
        ]
        # The last state outlives the project.
        assert (deleted[0].changes["name"], deleted[0].changes["password"]) == (["Acme", None], [MASK, None])

    async def test_a_delete_waiting_for_the_cloud_is_recorded_once_however_often_it_is_asked(
        self,
        project_service: ProjectService,
        project: Project,
        cloud: Connector,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        """The 202: the mark and the cloud connector's are recorded; asking again marks nothing new, and the scale
        job's last delete is a system write."""
        assert await project_service.delete(project.id, actor) is False
        assert await project_service.delete(project.id, actor) is False
        recorded = await audit_entries(sessions)

        async with sessions() as session:
            await ConnectorRepository(session).delete(cloud.id)
            await session.commit()
        assert await project_service.delete_if_emptied(project.id) is True

        assert [(e.entity_type, e.entity_id) for e in recorded if e.action == "deleted"] == [
            ("project", project.id),
            ("connector", cloud.id),
        ]
        assert await audit_entries(sessions) == recorded
