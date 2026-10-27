# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the project service against the real Postgres and Redis test containers.

What a write stores is read back with the repositories, what it announces and starts off the changes and job streams,
and what it deletes in Redis key by key.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from api.models.proxy import Proxy
from api.services.errors import NameConflictError, NotFoundError, WriteRejectedError
from api.services.projects import ProjectService
from tests.conftest import queued_job_keys
from tests.services.conftest import Witness, announced, forget_announcements


async def stored(sessions: async_sessionmaker[AsyncSession], project_id: str) -> Project | None:
    async with sessions() as session:
        return await ProjectRepository(session).get_by_id(project_id)


def create(**overrides: Any) -> ProjectCreate:
    return ProjectCreate(**{"name": "Acme", "username": "acme", "password": "pw", **overrides})


class TestCreate:
    async def test_stores_the_project(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        project = await project_service.create(
            create(routing_strategy="sticky", location_preflight=True, exit_headers=True)
        )

        assert await stored(sessions, project.id) == project
        assert (project.routing_strategy, project.location_preflight, project.exit_headers) == ("sticky", True, True)

    async def test_an_unknown_routing_strategy_is_refused(
        self, project_service: ProjectService, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(WriteRejectedError, match="Unknown strategy: fastest"):
            await project_service.create(create(routing_strategy="fastest"))

        async with sessions() as session:
            assert await ProjectRepository(session).get_all() == []

    async def test_a_taken_username_or_name_is_a_name_conflict(self, project_service: ProjectService) -> None:
        """Both are unique in Postgres; a duplicate was a 500 before the service translated it."""
        await project_service.create(create())

        with pytest.raises(NameConflictError, match="Project with username 'acme' already exists"):
            await project_service.create(create(name="Other"))
        with pytest.raises(NameConflictError, match="Project with name 'Acme' already exists"):
            await project_service.create(create(username="other"))


class TestUpdate:
    async def test_applies_the_fields_it_sets_and_returns_what_was_stored(
        self, project_service: ProjectService, project: Project, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        saved = await project_service.update(project.id, ProjectUpdate(name="Renamed", description="New"))

        assert (saved.name, saved.description, saved.username) == ("Renamed", "New", "acme")
        assert saved.updated_at > project.updated_at
        assert await stored(sessions, project.id) == saved

    async def test_an_empty_string_clears_an_override(
        self, project_service: ProjectService, project: Project
    ) -> None:
        await project_service.update(
            project.id, ProjectUpdate(location_conflict_rule=ConflictRule.ANY, location_city_radius_km=50)
        )

        cleared = ProjectUpdate(location_conflict_rule="", location_city_radius_km="")
        saved = await project_service.update(project.id, cleared)

        assert (saved.location_conflict_rule, saved.location_city_radius_km) == (None, None)

    async def test_interception_fields_the_mode_does_not_use_are_cleared(
        self, project_service: ProjectService, project: Project
    ) -> None:
        override = ProjectUpdate(
            tls_mitm_mode=MitmMode.OVERRIDE_UA, tls_mitm_engine=MitmEngine.RNET, tls_mitm_browser=MitmBrowser.FIREFOX
        )
        assert (await project_service.update(project.id, override)).tls_mitm_browser == MitmBrowser.FIREFOX

        # The engine stays from the stored row; match_ua takes no browser.
        matched = await project_service.update(project.id, ProjectUpdate(tls_mitm_mode=MitmMode.MATCH_UA))
        assert (matched.tls_mitm_engine, matched.tls_mitm_browser) == (MitmEngine.RNET, None)

        off = await project_service.update(project.id, ProjectUpdate(tls_mitm_mode=MitmMode.OFF))
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
    ) -> None:
        """Validated on a loaded copy: nothing of the edit reaches Postgres (today's routes edited the cache first)."""
        with pytest.raises(WriteRejectedError, match=message):
            await project_service.update(project.id, update)

        assert await stored(sessions, project.id) == project

    async def test_a_taken_username_is_a_name_conflict(
        self, project_service: ProjectService, project: Project, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await project_service.create(create(name="Other", username="other"))

        with pytest.raises(NameConflictError, match="Project with username 'other' already exists"):
            await project_service.update(project.id, ProjectUpdate(name="Renamed", username="other"))
        assert await stored(sessions, project.id) == project

    async def test_an_unknown_project_is_not_found(self, project_service: ProjectService) -> None:
        with pytest.raises(NotFoundError, match="Project not found"):
            await project_service.update("nope", ProjectUpdate(name="Renamed"))

    async def test_a_change_to_how_locations_are_judged_requests_reattribution(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient
    ) -> None:
        await project_service.update(project.id, ProjectUpdate(location_contradicted=ContradictedAction.REFUSE))

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == [f"project:{project.id}"]

    async def test_other_edits_request_no_reattribution(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient
    ) -> None:
        """Preflight decides whether sessions are checked, not how a row is judged."""
        await project_service.update(project.id, ProjectUpdate(name="Renamed", location_preflight=True))
        # The same rule again changes nothing.
        await project_service.update(project.id, ProjectUpdate(location_contradicted=project.location_contradicted))

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == []


class TestDelete:
    async def test_cascades_to_everything_the_project_holds(
        self,
        project_service: ProjectService,
        project: Project,
        static_credential: Credential,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sessions() as session:
            await ProxyRepository(session).create(Proxy(host="10.0.0.1", port=8080, connector_id=static_connector.id))
            await session.commit()

        await project_service.delete(project.id)

        async with sessions() as session:
            assert await ProjectRepository(session).get_by_id(project.id) is None
            assert await CredentialRepository(session).get_by_project(project.id) == []
            assert await ConnectorRepository(session).get_by_project(project.id) == []
            assert await ProxyRepository(session).get_by_project(project.id) == []

    async def test_deletes_the_captures_and_the_failure_log_and_nothing_else(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient
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

        await project_service.delete(project.id)

        assert await redis_client.client.exists(*owned) == 0
        assert await redis_client.client.exists(window) == 1

    async def test_an_unknown_project_is_not_found(self, project_service: ProjectService) -> None:
        with pytest.raises(NotFoundError, match="Project not found"):
            await project_service.delete("nope")


class TestChangeEntries:
    async def test_each_write_appends_one_project_entry(
        self, project_service: ProjectService, redis_client: RedisClient
    ) -> None:
        project = await project_service.create(create())
        await project_service.update(project.id, ProjectUpdate(name="Renamed"))
        await project_service.delete(project.id)

        assert await announced(redis_client) == [("project", project.id)] * 3

    async def test_each_entry_follows_its_commit(
        self, sessions: async_sessionmaker[AsyncSession], jobs: JobQueue, redis_client: RedisClient
    ) -> None:
        """A consumer woken by an entry reads Postgres: the write must be there by then, or it is missed."""
        witness = Witness(redis_client, lambda ref: stored(sessions, ref))
        service = ProjectService(sessions, jobs, redis_client, changes=witness)

        project = await service.create(create())
        renamed = await service.update(project.id, ProjectUpdate(name="Renamed"))
        await service.delete(project.id)

        assert witness.seen == [
            (ChangeKind.PROJECT, project.id, project),
            (ChangeKind.PROJECT, project.id, renamed),
            (ChangeKind.PROJECT, project.id, None),
        ]

    async def test_a_refused_write_announces_nothing(
        self, project_service: ProjectService, project: Project, redis_client: RedisClient
    ) -> None:
        await forget_announcements(redis_client)

        with pytest.raises(WriteRejectedError):
            await project_service.create(create(name="Other", username="other", routing_strategy="fastest"))
        with pytest.raises(NameConflictError):
            await project_service.create(create(name="Other"))
        with pytest.raises(WriteRejectedError):
            await project_service.update(project.id, ProjectUpdate(routing_strategy="fastest"))
        with pytest.raises(NotFoundError):
            await project_service.update("nope", ProjectUpdate(name="Renamed"))
        with pytest.raises(NotFoundError):
            await project_service.delete("nope")

        assert await announced(redis_client) == []
