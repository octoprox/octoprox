# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProjectRepository."""

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import ProjectRepository
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
