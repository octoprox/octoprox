# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Rows the exit reputation tests keep records for: projects in a mode, and connectors on them."""

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import ConnectorRepository, CredentialRepository, ProjectRepository
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import ExitReputationMode, Project


async def make_project(
    session: AsyncSession, name: str, mode: ExitReputationMode = ExitReputationMode.RECORD, **settings: object
) -> Project:
    """A project in ``mode``, committed."""
    project = await ProjectRepository(session).create(
        Project(name=name, username=name, password="pw", exit_reputation=mode, **settings)  # type: ignore[arg-type]
    )
    await session.commit()
    return project


async def make_connector(session: AsyncSession, project: Project, name: str = "pool") -> Connector:
    """A static connector of ``project``, committed."""
    credential = Credential(
        name=f"{name}-credential", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={}
    )
    await CredentialRepository(session).create(credential)
    connector = Connector(
        name=name,
        credential_id=credential.id,
        credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id,
        config={},
    )
    await ConnectorRepository(session).create(connector)
    await session.commit()
    return connector
