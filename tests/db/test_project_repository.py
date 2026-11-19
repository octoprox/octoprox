# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProjectRepository."""

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from api.attention.alert import REOPEN_WINDOW
from api.core import utc_now
from api.db.models import AlertModel
from api.db.repository import ProjectRepository
from api.models.alerts import AlertSettings
from api.models.project import Project


class TestProjectRepository:
    """Tests for ProjectRepository CRUD operations."""

    async def test_create_project(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a new project."""
        project = Project(
            name="Test Project",
            description="A test project",
            username="testuser",
            password="testpass",
            routing_strategy="round_robin",
        )

        result = await project_repo.create(project)
        await db_session.commit()

        assert result.id == project.id
        assert result.name == "Test Project"
        assert result.username == "testuser"

    async def test_get_all_projects(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving all projects."""
        # Create multiple projects
        for i in range(3):
            project = Project(
                name=f"Project {i}",
                username=f"user{i}",
                password="pass",
            )
            await project_repo.create(project)
        await db_session.commit()

        projects = await project_repo.get_all()

        assert len(projects) == 3
        assert {p.name for p in projects} == {"Project 0", "Project 1", "Project 2"}

    async def test_get_by_id(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a project by ID."""
        project = Project(
            name="Find Me",
            username="findme",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        result = await project_repo.get_by_id(project.id)

        assert result is not None
        assert result.id == project.id
        assert result.name == "Find Me"

    async def test_get_by_id_not_found(
        self,
        project_repo: ProjectRepository,
    ) -> None:
        """Test retrieving a non-existent project."""
        result = await project_repo.get_by_id("non-existent-id")
        assert result is None

    async def test_many_by_id_in_one_read(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """The projects asked for that exist, in id order; an unknown id is left out."""
        projects = [Project(name=f"Many {i}", username=f"many{i}", password="pass") for i in range(3)]
        for project in projects:
            await project_repo.create(project)
        await db_session.commit()

        found = await project_repo.get_many([projects[2].id, projects[0].id, "unknown", projects[0].id])

        assert [project.id for project in found] == sorted([projects[0].id, projects[2].id])
        assert await project_repo.get_many([]) == []

    async def test_update_project(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a project."""
        project = Project(
            name="Original Name",
            username="updateme",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        # Update the project
        project.name = "Updated Name"
        project.description = "New description"
        saved = await project_repo.update(project)
        await db_session.commit()

        # Verify update
        result = await project_repo.get_by_id(project.id)
        assert result is not None
        assert result.name == "Updated Name"
        assert result.description == "New description"
        # The answer is the row as stored, its updated_at stamped by the write.
        assert saved == result and saved.updated_at > project.updated_at

    async def test_an_update_of_a_project_that_is_gone_answers_none(self, project_repo: ProjectRepository) -> None:
        assert await project_repo.update(Project(name="Gone", username="gone", password="pass")) is None

    async def test_delete_project(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test deleting a project."""
        project = Project(
            name="Delete Me",
            username="deleteme",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        # Delete the project
        await project_repo.delete(project.id)
        await db_session.commit()

        # Verify deletion
        result = await project_repo.get_by_id(project.id)
        assert result is None

    async def test_project_routing_strategy(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test project with different routing strategies."""
        strategies = ["round_robin", "least_used", "random", "sticky", "health_based"]

        for strategy in strategies:
            project = Project(
                name=f"Strategy {strategy}",
                username=f"user_{strategy}",
                password="pass",
                routing_strategy=strategy,
            )
            await project_repo.create(project)

        await db_session.commit()

        projects = await project_repo.get_all()
        assert len(projects) == len(strategies)


    async def test_city_radius_override_round_trip(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """A project's own city radius is stored; null means it inherits the install default."""
        project = Project(name="Radius", username="radius", password="pass")
        await project_repo.create(project)
        await db_session.commit()
        stored = await project_repo.get_by_id(project.id)
        assert stored is not None and stored.location_city_radius_km is None

        project.location_city_radius_km = 7.5
        await project_repo.update(project)
        await db_session.commit()
        stored = await project_repo.get_by_id(project.id)
        assert stored is not None and stored.location_city_radius_km == 7.5

        project.location_city_radius_km = 0
        await project_repo.update(project)
        await db_session.commit()
        stored = await project_repo.get_by_id(project.id)
        assert stored is not None and stored.location_city_radius_km == 0


class TestAttentionMutes:
    """The Needs attention mutes: their own column, written without stamping the project as edited."""

    @staticmethod
    async def _projects(project_repo: ProjectRepository, db_session: AsyncSession) -> list[Project]:
        projects = [Project(name=f"Muting {i}", username=f"muting{i}", password="pass") for i in range(2)]
        for project in projects:
            await project_repo.create(project)
        await db_session.commit()
        return projects

    async def test_mutes_are_written_as_given_and_read_for_one_project_or_every_one(
        self, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        one, two = await self._projects(project_repo, db_session)
        stamped = (await project_repo.get_by_id(one.id)).updated_at  # type: ignore[union-attr]
        mutes = {"connectors_off": {"severity": "problem", "until": None}}

        assert await project_repo.write_attention_mutes(one.id, mutes)
        assert not await project_repo.write_attention_mutes("unknown", mutes)
        await db_session.commit()
        db_session.expire_all()

        assert await project_repo.attention_mutes([one.id]) == {one.id: mutes}
        assert await project_repo.attention_mutes(None) == {one.id: mutes, two.id: {}}
        assert await project_repo.lock_attention_mutes(one.id) == mutes
        assert await project_repo.lock_attention_mutes("unknown") is None
        # A mute is not an edit.
        assert (await project_repo.get_by_id(one.id)).updated_at == stamped  # type: ignore[union-attr]

    async def test_a_project_edit_leaves_the_mutes_alone(
        self, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        one, _two = await self._projects(project_repo, db_session)
        mutes = {"connectors_off": {"severity": "problem", "until": None}}
        await project_repo.write_attention_mutes(one.id, mutes)
        await db_session.commit()
        db_session.expire_all()

        edited = (await project_repo.get_by_id(one.id)).model_copy(update={"description": "edited"})  # type: ignore[union-attr]
        await project_repo.update(edited)
        await db_session.commit()

        assert await project_repo.attention_mutes([one.id]) == {one.id: mutes}


def _mute(at: datetime, until: datetime | None = None) -> dict[str, Any]:
    """A stored mute, set at ``at``; until its line clears when ``until`` is None."""
    return {"severity": "warning", "duration": "until_clear" if until is None else "day",
            "until": until.isoformat() if until else None, "by": "ivan", "at": at.isoformat()}


def _alert(project_id: str, key: str, now: datetime, *, resolved: timedelta | None = None, hidden_by: str = "") -> AlertModel:
    """A stored alert of ``key``, open, or resolved ``resolved`` before ``now``."""
    return AlertModel(
        id=str(uuid4()), project_id=project_id, key=key, kind=key.partition(":")[0], severity="warning",
        worst_severity="warning", body=" text", hidden_by=hidden_by, opened_at=now - timedelta(hours=1),
        last_seen_at=now, resolved_at=now - resolved if resolved is not None else None,
    )


class TestEndClearedMutes:
    """The alerts job ends an until-clear mute once its line's alert has stayed resolved for the reopen window."""

    async def test_which_mutes_end(self, project_repo: ProjectRepository, db_session: AsyncSession) -> None:
        now = utc_now()
        one, two = (Project(name=f"Ending {i}", username=f"ending{i}", password="pass") for i in range(2))
        for project in (one, two):
            await project_repo.create(project)
        await db_session.commit()
        stamped = (await project_repo.get_by_id(one.id)).updated_at  # type: ignore[union-attr]
        earlier = now - timedelta(hours=2)
        mutes = {
            # Resolved before the window: ends.
            "cleared": _mute(earlier),
            # Never stored, or gone with retention: ends too.
            "unknown": _mute(earlier),
            # Resolved within the window: a flapping line comes back still muted.
            "flapping": _mute(earlier),
            # Open, covered by a wider line: its mute holds.
            "covered": _mute(earlier),
            # Open and shown.
            "open": _mute(earlier),
            # Timed: it ends by its time.
            "timed": _mute(earlier, until=now + timedelta(days=1)),
            # Set after the run read its data: never ended by that run.
            "late": _mute(now + timedelta(seconds=1)),
        }
        await project_repo.write_attention_mutes(one.id, mutes)
        await project_repo.write_attention_mutes(two.id, {"cleared": _mute(earlier)})
        db_session.add_all([
            _alert(one.id, "cleared", now, resolved=REOPEN_WINDOW + timedelta(seconds=1)),
            _alert(one.id, "flapping", now, resolved=REOPEN_WINDOW - timedelta(seconds=1)),
            _alert(one.id, "covered", now, hidden_by="connectors_off"),
            _alert(one.id, "open", now),
        ])
        await db_session.commit()

        assert await project_repo.end_cleared_mutes([one.id], now, reopen=REOPEN_WINDOW) == 1
        await db_session.commit()
        db_session.expire_all()

        stored = await project_repo.attention_mutes(None)
        assert set(stored[one.id]) == {"flapping", "covered", "open", "timed", "late"}
        # Only the projects asked about.
        assert set(stored[two.id]) == {"cleared"}
        # Nothing left to end: nothing written.
        assert await project_repo.end_cleared_mutes([one.id], now, reopen=REOPEN_WINDOW) == 0
        assert await project_repo.end_cleared_mutes([], now, reopen=REOPEN_WINDOW) == 0
        # Ending a mute is not an edit.
        assert (await project_repo.get_by_id(one.id)).updated_at == stamped  # type: ignore[union-attr]


class TestAlertSettings:
    """A project's alert cut-offs: their own column, never in the domain project, written without stamping it."""

    async def test_written_and_read_without_an_edit(
        self, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project = Project(name="Cut-offs", username="cutoffs", password="pass")
        await project_repo.create(project)
        await db_session.commit()
        stamped = (await project_repo.get_by_id(project.id)).updated_at  # type: ignore[union-attr]

        assert await project_repo.alert_settings([project.id, "unknown"]) == {project.id: AlertSettings()}
        settings = AlertSettings(vendor_min=20, keep_days=0)
        assert await project_repo.write_alert_settings(project.id, settings)
        assert not await project_repo.write_alert_settings("unknown", settings)
        await db_session.commit()
        db_session.expire_all()

        assert await project_repo.alert_settings([project.id]) == {project.id: settings}
        assert await project_repo.lock_alert_settings(project.id) == settings
        assert await project_repo.lock_alert_settings("unknown") is None
        assert (await project_repo.get_by_id(project.id)).updated_at == stamped  # type: ignore[union-attr]

        edited = (await project_repo.get_by_id(project.id)).model_copy(update={"description": "edited"})  # type: ignore[union-attr]
        await project_repo.update(edited)
        await db_session.commit()
        assert await project_repo.alert_settings([project.id]) == {project.id: settings}
