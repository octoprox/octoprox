"""Tests for CredentialRepository."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import CredentialRepository, ProjectRepository
from api.models.credential import Credential, CredentialType
from api.models.project import Project


class TestCredentialRepository:
    """Tests for CredentialRepository CRUD operations."""

    async def _create_project(
        self,
        project_repo: ProjectRepository,
        session: AsyncSession,
        name: str = "Test Project",
    ) -> Project:
        """Helper to create a project for credential tests."""
        project = Project(
            name=name,
            username=f"user_{name.lower().replace(' ', '_')}",
            password="pass",
        )
        await project_repo.create(project)
        await session.commit()
        return project

    async def test_create_credential(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a new credential."""
        project = await self._create_project(project_repo, db_session)

        credential = Credential(
            name="Test Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )

        result = await credential_repo.create(credential)
        await db_session.commit()

        assert result.id == credential.id
        assert result.name == "Test Credential"
        assert result.type == CredentialType.STATIC_PROXY_PROVIDER

    async def test_get_all_credentials(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving all credentials."""
        project = await self._create_project(project_repo, db_session)

        for i in range(3):
            credential = Credential(
                name=f"Credential {i}",
                type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id=project.id,
                config={},
            )
            await credential_repo.create(credential)
        await db_session.commit()

        credentials = await credential_repo.get_all()

        assert len(credentials) == 3

    async def test_get_by_project(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving credentials by project."""
        project1 = await self._create_project(project_repo, db_session, "Project 1")
        project2 = await self._create_project(project_repo, db_session, "Project 2")

        # Create credentials for project1
        for i in range(2):
            cred = Credential(
                name=f"P1 Cred {i}",
                type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id=project1.id,
                config={},
            )
            await credential_repo.create(cred)

        # Create credential for project2
        cred = Credential(
            name="P2 Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project2.id,
            config={},
        )
        await credential_repo.create(cred)
        await db_session.commit()

        p1_creds = await credential_repo.get_by_project(project1.id)
        p2_creds = await credential_repo.get_by_project(project2.id)

        assert len(p1_creds) == 2
        assert len(p2_creds) == 1

    async def test_get_by_id(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a credential by ID."""
        project = await self._create_project(project_repo, db_session)

        credential = Credential(
            name="Find Me",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={"key": "value"},
        )
        await credential_repo.create(credential)
        await db_session.commit()

        result = await credential_repo.get_by_id(credential.id)

        assert result is not None
        assert result.id == credential.id
        assert result.config == {"key": "value"}

    async def test_get_by_id_not_found(
        self,
        credential_repo: CredentialRepository,
    ) -> None:
        """Test retrieving a non-existent credential."""
        result = await credential_repo.get_by_id("non-existent-id")
        assert result is None

    async def test_update_credential(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a credential."""
        project = await self._create_project(project_repo, db_session)

        credential = Credential(
            name="Original",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await credential_repo.create(credential)
        await db_session.commit()

        credential.name = "Updated"
        credential.config = {"updated": True}
        await credential_repo.update(credential)
        await db_session.commit()

        result = await credential_repo.get_by_id(credential.id)
        assert result is not None
        assert result.name == "Updated"
        assert result.config == {"updated": True}

    async def test_delete_credential(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test deleting a credential."""
        project = await self._create_project(project_repo, db_session)

        credential = Credential(
            name="Delete Me",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await credential_repo.create(credential)
        await db_session.commit()

        await credential_repo.delete(credential.id)
        await db_session.commit()

        result = await credential_repo.get_by_id(credential.id)
        assert result is None

    async def test_credential_types(
        self,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating credentials with different types."""
        project = await self._create_project(project_repo, db_session)

        types = [
            CredentialType.STATIC_PROXY_PROVIDER,
            CredentialType.AWS,
            CredentialType.GCP,
            CredentialType.AZURE,
        ]

        for cred_type in types:
            credential = Credential(
                name=f"Cred {cred_type.value}",
                type=cred_type,
                project_id=project.id,
                config={},
            )
            await credential_repo.create(credential)

        await db_session.commit()

        credentials = await credential_repo.get_all()
        assert len(credentials) == len(types)

