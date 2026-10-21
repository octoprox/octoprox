# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProxyRepository: CRUD, the status column and the forward-only metadata patch."""
import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


class TestProxyRepository:
    """Tests for ProxyRepository CRUD operations."""

    async def _create_project_credential_connector(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        session: AsyncSession,
        suffix: str = "",
    ) -> tuple[Project, Credential, Connector]:
        """Helper to create prerequisites for proxy tests."""
        project = Project(
            name=f"Test Project{suffix}",
            username=f"user{suffix}",
            password="pass",
        )
        await project_repo.create(project)

        credential = Credential(
            name=f"Test Credential{suffix}",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await credential_repo.create(credential)

        connector = Connector(
            name=f"Test Connector{suffix}",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            enabled=True,
        )
        await connector_repo.create(connector)
        await session.commit()

        return project, credential, connector

    async def test_create_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a new proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )

        result = await proxy_repo.create(proxy)
        await db_session.commit()

        assert result.id == proxy.id
        assert result.host == "proxy.example.com"
        assert result.port == 8080

    async def test_get_all_proxies(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving all proxies."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        for i in range(3):
            proxy = Proxy(
                host=f"proxy{i}.example.com",
                port=8080 + i,
                protocol=ProxyProtocol.HTTP,
                connector_id=connector.id,
            )
            await proxy_repo.create(proxy)
        await db_session.commit()

        proxies = await proxy_repo.get_all()

        assert len(proxies) == 3

    async def test_get_by_connector(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving proxies by connector."""
        project1, cred1, conn1 = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session, "_1"
        )
        project2, cred2, conn2 = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session, "_2"
        )

        # Create proxies for connector1
        for i in range(2):
            proxy = Proxy(
                host=f"c1proxy{i}.example.com",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                connector_id=conn1.id,
            )
            await proxy_repo.create(proxy)

        # Create proxy for connector2
        proxy = Proxy(
            host="c2proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=conn2.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        c1_proxies = await proxy_repo.get_by_connector(conn1.id)
        c2_proxies = await proxy_repo.get_by_connector(conn2.id)

        assert len(c1_proxies) == 2
        assert len(c2_proxies) == 1

    async def test_get_by_id(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a proxy by ID."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="findme.example.com",
            port=3128,
            protocol=ProxyProtocol.SOCKS5,
            connector_id=connector.id,
            username="proxyuser",
            password="proxypass",
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)

        assert result is not None
        assert result.id == proxy.id
        assert result.host == "findme.example.com"
        assert result.username == "proxyuser"

    async def test_get_by_id_not_found(
        self,
        proxy_repo: ProxyRepository,
    ) -> None:
        """Test retrieving a non-existent proxy."""
        result = await proxy_repo.get_by_id("non-existent-id")
        assert result is None

    async def test_update_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="original.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        proxy.host = "updated.example.com"
        proxy.port = 3128
        proxy.protocol = ProxyProtocol.SOCKS5
        await proxy_repo.update(proxy)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)
        assert result is not None
        assert result.host == "updated.example.com"
        assert result.port == 3128
        assert result.protocol == ProxyProtocol.SOCKS5

    async def test_delete_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test deleting a proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="deleteme.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        await proxy_repo.delete(proxy.id)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)
        assert result is None

    async def test_proxy_protocols(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating proxies with different protocols."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        protocols = [ProxyProtocol.HTTP, ProxyProtocol.HTTPS, ProxyProtocol.SOCKS5]

        for i, protocol in enumerate(protocols):
            proxy = Proxy(
                host=f"proxy{i}.example.com",
                port=8080,
                protocol=protocol,
                connector_id=connector.id,
            )
            await proxy_repo.create(proxy)

        await db_session.commit()

        proxies = await proxy_repo.get_all()
        assert len(proxies) == len(protocols)



async def _connector(session: AsyncSession, name: str = "Pool") -> Connector:
    """A committed static connector in a project of its own."""
    project = Project(name=name, username=name.lower(), password="pw")
    credential = Credential(name=name, type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={})
    connector = Connector(
        name=name, credential_id=credential.id, credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id, config={},
    )
    await ProjectRepository(session).create(project)
    await CredentialRepository(session).create(credential)
    await ConnectorRepository(session).create(connector)
    await session.commit()
    return connector


async def _stored(proxy_repo: ProxyRepository, proxy_id: str) -> Proxy:
    proxy = await proxy_repo.get_by_id(proxy_id)
    assert proxy is not None
    return proxy


class TestStatus:
    async def test_new_proxies_are_stored_with_their_builders_status(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        slot = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.INITIALIZING)
        group = [
            Proxy(host="10.0.0.2", port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY),
            Proxy(host="10.0.0.3", port=8080, connector_id=connector.id),
        ]
        await proxy_repo.create(slot)
        assert await proxy_repo.bulk_create(group) == group
        await db_session.commit()
        stored = {proxy.host: proxy.status for proxy in await proxy_repo.get_by_connector(connector.id)}
        assert stored == {"10.0.0.1": ProxyStatus.INITIALIZING, "10.0.0.2": ProxyStatus.HEALTHY, "10.0.0.3": ProxyStatus.UNKNOWN}
        assert (await _stored(proxy_repo, slot.id)).status_changed_at is None

    async def test_a_proxy_whose_id_is_stored_is_skipped_and_only_the_inserted_are_returned(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        """Insert-or-skip: a row whose ID derives from what it stands for lands once, whoever saves it."""
        connector = await _connector(db_session)
        stored = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.INITIALIZING)
        await proxy_repo.create(stored)
        await db_session.commit()
        again = stored.model_copy(update={"host": "10.0.0.9", "status": ProxyStatus.HEALTHY})
        fresh = Proxy(host="10.0.0.2", port=8080, connector_id=connector.id)

        assert await proxy_repo.bulk_create([again, fresh]) == [fresh]
        assert await proxy_repo.bulk_create([again]) == []
        assert await proxy_repo.bulk_create([]) == []
        await db_session.commit()

        kept = await _stored(proxy_repo, stored.id)
        assert (kept.host, kept.status) == ("10.0.0.1", ProxyStatus.INITIALIZING)
        assert {proxy.id for proxy in await proxy_repo.get_by_connector(connector.id)} == {stored.id, fresh.id}

    async def test_an_edit_never_writes_the_status(self, proxy_repo: ProxyRepository, db_session: AsyncSession) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY)
        await proxy_repo.create(proxy)
        await db_session.commit()
        edited = proxy.model_copy(update={"host": "10.0.0.9", "status": ProxyStatus.UNHEALTHY})
        await proxy_repo.update(edited)
        await db_session.commit()
        stored = await _stored(proxy_repo, proxy.id)
        assert (stored.host, stored.status) == ("10.0.0.9", ProxyStatus.HEALTHY)

    async def test_the_health_compare_and_set_moves_only_from_the_status_it_expects(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY)
        await proxy_repo.create(proxy)
        await db_session.commit()
        before = utc_now()
        assert await proxy_repo.set_status_if(proxy.id, ProxyStatus.DEGRADED, ProxyStatus.HEALTHY) is True
        await db_session.commit()
        stored = await _stored(proxy_repo, proxy.id)
        assert stored.status == ProxyStatus.DEGRADED
        assert stored.status_changed_at is not None and stored.status_changed_at >= before
        # A check that loaded the row before that flip lost the race: nothing is written.
        assert await proxy_repo.set_status_if(proxy.id, ProxyStatus.UNHEALTHY, ProxyStatus.HEALTHY) is False
        assert await proxy_repo.set_status_if("gone", ProxyStatus.HEALTHY, ProxyStatus.DEGRADED) is False
        await db_session.commit()
        assert (await _stored(proxy_repo, proxy.id)).status == ProxyStatus.DEGRADED

    async def test_the_health_compare_and_set_never_leaves_draining_or_terminating(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        draining = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.DRAINING)
        terminating = Proxy(host="10.0.0.2", port=8080, connector_id=connector.id, status=ProxyStatus.TERMINATING)
        await proxy_repo.bulk_create([draining, terminating])
        await db_session.commit()
        assert await proxy_repo.set_status_if(draining.id, ProxyStatus.HEALTHY, ProxyStatus.DRAINING) is False
        assert await proxy_repo.set_status_if(terminating.id, ProxyStatus.HEALTHY, ProxyStatus.TERMINATING) is False
        await db_session.commit()
        assert {p.status for p in await proxy_repo.get_by_connector(connector.id)} == {
            ProxyStatus.DRAINING, ProxyStatus.TERMINATING,
        }

    async def test_drain_and_terminate_set_the_status_and_keep_the_time_it_was_entered(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.UNHEALTHY)
        await proxy_repo.create(proxy)
        await db_session.commit()
        assert await proxy_repo.set_status(proxy.id, ProxyStatus.DRAINING) is True
        await db_session.commit()
        entered = (await _stored(proxy_repo, proxy.id)).status_changed_at
        # Draining again (the scale job asks each cycle) changes nothing.
        assert await proxy_repo.set_status(proxy.id, ProxyStatus.DRAINING) is False
        await db_session.commit()
        assert (await _stored(proxy_repo, proxy.id)).status_changed_at == entered
        assert await proxy_repo.set_status(proxy.id, ProxyStatus.TERMINATING) is True
        assert await proxy_repo.set_status("gone", ProxyStatus.TERMINATING) is False
        await db_session.commit()
        assert (await _stored(proxy_repo, proxy.id)).status == ProxyStatus.TERMINATING

    async def test_a_drain_never_takes_a_terminating_proxy_back(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        """A scale run that read the pool before an admin deleted the proxy must not drain it again."""
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY)
        await proxy_repo.create(proxy)
        await db_session.commit()
        assert await proxy_repo.set_status(proxy.id, ProxyStatus.TERMINATING) is True
        await db_session.commit()
        entered = (await _stored(proxy_repo, proxy.id)).status_changed_at
        assert await proxy_repo.set_status(proxy.id, ProxyStatus.DRAINING) is False
        await db_session.commit()
        stored = await _stored(proxy_repo, proxy.id)
        assert (stored.status, stored.status_changed_at) == (ProxyStatus.TERMINATING, entered)


class TestEdit:
    async def test_a_row_read_for_update_holds_other_writers_until_the_edit_commits(
        self,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A sync's edit, or a country pinned by hand, must not undo a sighting's patch made after its read."""
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, metadata={"country": "US"})
        await proxy_repo.create(proxy)
        await db_session.commit()

        async def patch() -> bool:
            async with db_session_factory() as other:
                written = await ProxyRepository(other).patch_metadata(proxy.id, {"city": "london"}, set(), None)
                await other.commit()
                return written

        async with db_session_factory() as session:
            editor = ProxyRepository(session)
            current = await editor.get_in_project(connector.project_id, proxy.id, for_update=True)
            assert current is not None
            patching = asyncio.create_task(patch())
            await asyncio.sleep(0.2)
            # The patch waits for the row.
            assert not patching.done()
            await editor.update(current.model_copy(update={"metadata": {**current.metadata, "country": "GB"}}))
            await session.commit()
        assert await patching

        assert (await _stored(proxy_repo, proxy.id)).metadata == {"country": "GB", "city": "london"}


class TestMetadataPatch:
    async def test_only_the_named_keys_change(self, proxy_repo: ProxyRepository, db_session: AsyncSession) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(
            host="10.0.0.1", port=8080, connector_id=connector.id,
            metadata={"session_id": "s1", "country": "US", "location_conflict": True},
        )
        await proxy_repo.create(proxy)
        await db_session.commit()
        patched = await proxy_repo.patch_metadata(proxy.id, {"country": "GB"}, {"location_conflict"}, None)
        await db_session.commit()
        assert patched is True
        assert (await _stored(proxy_repo, proxy.id)).metadata == {"session_id": "s1", "country": "GB"}
        assert await proxy_repo.patch_metadata("gone", {"country": "GB"}, set(), None) is False

    async def test_a_sighting_older_than_the_rows_attribution_is_refused(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        checked = utc_now()
        proxy = Proxy(
            host="10.0.0.1", port=8080, connector_id=connector.id,
            metadata={"country": "GB", "location_checked_at": checked.isoformat()},
        )
        await proxy_repo.create(proxy)
        await db_session.commit()
        older = checked - timedelta(seconds=5)
        newer = checked + timedelta(seconds=5)
        assert await proxy_repo.patch_metadata(
            proxy.id, {"country": "US", "location_checked_at": older.isoformat()}, set(), older
        ) is False
        # Not newer is enough: a re-judgement keeps the stamp it read.
        assert await proxy_repo.patch_metadata(proxy.id, {"city": "london"}, set(), checked) is True
        assert await proxy_repo.patch_metadata(
            proxy.id, {"country": "FR", "location_checked_at": newer.isoformat()}, {"city"}, newer
        ) is True
        await db_session.commit()
        assert (await _stored(proxy_repo, proxy.id)).metadata == {"country": "FR", "location_checked_at": newer.isoformat()}

    async def test_a_row_never_attributed_takes_any_sighting(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id, metadata={"session_id": "s1"})
        await proxy_repo.create(proxy)
        await db_session.commit()
        stamp = utc_now()
        assert await proxy_repo.patch_metadata(
            proxy.id, {"country": "GB", "location_checked_at": stamp.isoformat()}, set(), stamp
        ) is True
        await db_session.commit()
        assert (await _stored(proxy_repo, proxy.id)).metadata["country"] == "GB"


class TestBatchReads:
    """Reads of many rows in one statement, for the jobs that work a batch at a time."""

    async def test_many_by_id_in_id_order(self, proxy_repo: ProxyRepository, db_session: AsyncSession) -> None:
        connector = await _connector(db_session)
        proxies = [Proxy(host=f"10.0.0.{i}", port=8080, connector_id=connector.id) for i in range(3)]
        await proxy_repo.bulk_create(proxies)
        await db_session.commit()

        found = await proxy_repo.get_many([proxies[2].id, "unknown", proxies[0].id])

        assert [proxy.id for proxy in found] == sorted([proxies[0].id, proxies[2].id])
        assert await proxy_repo.get_many([]) == []

    async def test_many_with_their_connectors_and_credentials(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        connector = await _connector(db_session)
        proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id)
        await proxy_repo.create(proxy)
        await db_session.commit()

        [(found, owner, credential)] = await proxy_repo.get_many_with_connectors([proxy.id, "unknown"])

        assert (found.id, owner.id, credential.id) == (proxy.id, connector.id, connector.credential_id)
        assert await proxy_repo.get_many_with_connectors([]) == []

    async def test_pages_follow_one_another_by_id_within_a_connector_or_a_project(
        self, proxy_repo: ProxyRepository, db_session: AsyncSession
    ) -> None:
        first, second = await _connector(db_session, "First"), await _connector(db_session, "Second")
        mine = [Proxy(host=f"10.0.1.{i}", port=8080, connector_id=first.id) for i in range(5)]
        await proxy_repo.bulk_create([*mine, Proxy(host="10.0.2.1", port=8080, connector_id=second.id)])
        await db_session.commit()

        pages: list[list[str]] = []
        after: str | None = None
        while page := await proxy_repo.page(after, 2, connector_id=first.id):
            pages.append([proxy.id for proxy in page])
            after = page[-1].id

        ordered = sorted(proxy.id for proxy in mine)
        assert pages == [ordered[0:2], ordered[2:4], ordered[4:]]
        assert [proxy.id for proxy in await proxy_repo.page(None, 10, project_id=first.project_id)] == ordered
        assert len(await proxy_repo.page(None, 10)) == 6
