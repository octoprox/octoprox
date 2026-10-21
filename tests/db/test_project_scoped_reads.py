# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Reads scoped to a project, and the counts the projects list shows.

Two projects with a credential, a connector and proxies each: a read under
one project never returns the other's rows, which is what lets a route
answer 404 for an id that belongs to another project.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectCounts,
    ProjectRepository,
    ProxyRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus


@dataclass
class Holdings:
    project: Project
    credential: Credential
    connector: Connector
    proxies: list[Proxy]


async def _holdings(session: AsyncSession, name: str, statuses: list[ProxyStatus]) -> Holdings:
    project = Project(name=name, username=name.lower(), password="pw")
    credential = Credential(name=name, type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={})
    connector = Connector(
        name=name, credential_id=credential.id, credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id, config={},
    )
    proxies = [
        Proxy(host=f"{name.lower()}-{n}.example.com", port=8080, connector_id=connector.id, status=status)
        for n, status in enumerate(statuses)
    ]
    await ProjectRepository(session).create(project)
    await CredentialRepository(session).create(credential)
    await ConnectorRepository(session).create(connector)
    await ProxyRepository(session).bulk_create(proxies)
    await session.commit()
    return Holdings(project, credential, connector, proxies)


@pytest.fixture
async def ours(db_session: AsyncSession) -> Holdings:
    return await _holdings(db_session, "Ours", [ProxyStatus.HEALTHY, ProxyStatus.HEALTHY, ProxyStatus.DRAINING])


@pytest.fixture
async def theirs(db_session: AsyncSession) -> Holdings:
    return await _holdings(db_session, "Theirs", [ProxyStatus.UNHEALTHY])


class TestScopedReads:
    async def test_lists_hold_only_the_projects_rows(
        self, db_session: AsyncSession, ours: Holdings, theirs: Holdings
    ) -> None:
        credentials = await CredentialRepository(db_session).get_by_project(ours.project.id)
        connectors = await ConnectorRepository(db_session).get_by_project(ours.project.id)
        proxies = await ProxyRepository(db_session).get_by_project(ours.project.id)
        assert [c.id for c in credentials] == [ours.credential.id]
        assert [c.id for c in connectors] == [ours.connector.id]
        assert {p.id for p in proxies} == {p.id for p in ours.proxies}

    async def test_an_id_from_another_project_reads_as_absent(
        self, db_session: AsyncSession, ours: Holdings, theirs: Holdings
    ) -> None:
        credentials = CredentialRepository(db_session)
        connectors = ConnectorRepository(db_session)
        proxies = ProxyRepository(db_session)
        mine = (
            await credentials.get_in_project(ours.project.id, ours.credential.id),
            await connectors.get_in_project(ours.project.id, ours.connector.id),
            await proxies.get_in_project(ours.project.id, ours.proxies[0].id),
        )
        assert [entity.id if entity else None for entity in mine] == [
            ours.credential.id, ours.connector.id, ours.proxies[0].id,
        ]
        assert await credentials.get_in_project(ours.project.id, theirs.credential.id) is None
        assert await connectors.get_in_project(ours.project.id, theirs.connector.id) is None
        assert await proxies.get_in_project(ours.project.id, theirs.proxies[0].id) is None
        assert await proxies.get_in_project(ours.project.id, "gone") is None


class TestCounts:
    async def test_every_project_is_counted_in_one_query(
        self, db_session: AsyncSession, ours: Holdings, theirs: Holdings
    ) -> None:
        empty = Project(name="Empty", username="empty", password="pw")
        await ProjectRepository(db_session).create(empty)
        await db_session.commit()
        counts = await ProjectRepository(db_session).get_counts()
        assert counts == {
            ours.project.id: ProjectCounts(credentials=1, connectors=1, proxies_by_status={"healthy": 2, "draining": 1}),
            theirs.project.id: ProjectCounts(credentials=1, connectors=1, proxies_by_status={"unhealthy": 1}),
        }
        assert counts[ours.project.id].proxies == 3

    async def test_one_project_can_be_counted_alone(
        self, db_session: AsyncSession, ours: Holdings, theirs: Holdings
    ) -> None:
        counts = await ProjectRepository(db_session).get_counts(theirs.project.id)
        assert counts == {theirs.project.id: ProjectCounts(credentials=1, connectors=1, proxies_by_status={"unhealthy": 1})}
