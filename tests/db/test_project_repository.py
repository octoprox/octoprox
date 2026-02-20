# Copyright 2025 Octoprox Authors
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

    async def test_get_by_username(
        self,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a project by username."""
        project = Project(
            name="Username Test",
            username="uniqueuser",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        result = await project_repo.get_by_username("uniqueuser")

        assert result is not None
        assert result.username == "uniqueuser"

    async def test_get_by_username_not_found(
        self,
        project_repo: ProjectRepository,
    ) -> None:
        """Test retrieving a project by non-existent username."""
        result = await project_repo.get_by_username("nonexistent")
        assert result is None

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
        await project_repo.update(project)
        await db_session.commit()

        # Verify update
        result = await project_repo.get_by_id(project.id)
        assert result is not None
        assert result.name == "Updated Name"
        assert result.description == "New description"

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

