# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the credential service against the real Postgres and Redis test containers.

Bright Data's account check goes to a fake vendor that records every call (an ``httpx.MockTransport`` behind the
registry's SDK runtime), and what a write announces is read off the changes stream.
"""

from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.audit import Actor
from api.db.redis import RedisClient
from api.db.repository import CredentialRepository
from api.db.streams import ChangeKind, ChangeLog
from api.models.audit import MASK
from api.models.connector import ConnectorCreate
from api.models.credential import Credential, CredentialCreate, CredentialUpdate
from api.models.project import Project, ProjectCreate
from api.providers.registry import ProviderRegistry, build_registry
from api.providers.sdk.egress import EgressPolicy
from api.providers.sdk.provider import SdkRuntime
from api.services.connectors import ConnectorService
from api.services.credentials import CredentialService
from api.services.errors import (
    InvalidConfigError,
    NameConflictError,
    NotFoundError,
    WriteRejectedError,
)
from api.services.projects import ProjectService
from tests.services.conftest import (
    AWS_CREDENTIAL,
    STATIC,
    Witness,
    announced,
    audit_entries,
    audit_text,
    forget_announcements,
)

# A Bright Data credential: its provider checks the token with the vendor and captures the customer id.
BRIGHT_DATA = CredentialCreate(name="Bright", type="brightdata", config={"token": "T"})


class FakeVendor:
    """Bright Data's account API: answers the status call with ``status`` and a customer id, and records each call.

    ``meanwhile``, when set, runs once before the next answer: another write landing while the vendor is asked.
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = "active"
        self.meanwhile: Callable[[], Awaitable[object]] | None = None

    def runtime(self) -> SdkRuntime:
        async def answer(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if (meanwhile := self.meanwhile) is not None:
                self.meanwhile = None
                await meanwhile()
            return httpx.Response(200, json={"status": self.status, "customer": "c_777"})

        return SdkRuntime(
            egress_policy=EgressPolicy(allow_http=True, allow_private=True, pin_dns=False),
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer)),
        )


@pytest.fixture
def vendor() -> FakeVendor:
    return FakeVendor()


@pytest.fixture
def checked_service(
    sessions: async_sessionmaker[AsyncSession], vendor: FakeVendor, change_log: ChangeLog
) -> CredentialService:
    """A credential service whose providers reach the fake vendor."""
    registry = build_registry(runtime=vendor.runtime(), include_plugins=False)
    return CredentialService(sessions, registry, changes=change_log)


async def stored(sessions: async_sessionmaker[AsyncSession], credential_id: str) -> Credential | None:
    async with sessions() as session:
        return await CredentialRepository(session).get_by_id(credential_id)


class TestCreate:
    async def test_stores_the_validated_config(
        self,
        credential_service: CredentialService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        credential = await credential_service.create(
            project.id, CredentialCreate(name="AWS", type="aws", config={**AWS_CREDENTIAL, "role_arn": "  "}), actor
        )

        # The validator normalised the config: the default auth mode filled in, a blank role ARN dropped.
        assert credential.config == {"auth_mode": "access_keys", **AWS_CREDENTIAL}
        assert await stored(sessions, credential.id) == credential

    async def test_an_invalid_config_is_refused_with_the_validators_message(
        self, credential_service: CredentialService, project: Project, actor: Actor
    ) -> None:
        with pytest.raises(InvalidConfigError, match="secret_key"):
            await credential_service.create(
                project.id, CredentialCreate(name="AWS", type="aws", config={**AWS_CREDENTIAL, "secret_key": ""}), actor
            )
        with pytest.raises(InvalidConfigError, match="Unknown credential type: nope"):
            await credential_service.create(project.id, CredentialCreate(name="Nope", type="nope"), actor)

    async def test_an_unknown_project_is_not_found(self, credential_service: CredentialService, actor: Actor) -> None:
        with pytest.raises(NotFoundError, match="Project not found"):
            await credential_service.create("nope", CredentialCreate(name="Static", type=STATIC.type), actor)

    async def test_names_are_unique_per_project_whatever_their_case(
        self, credential_service: CredentialService, project_service: ProjectService, project: Project, actor: Actor
    ) -> None:
        await credential_service.create(project.id, CredentialCreate(name="Static", type=STATIC.type), actor)

        with pytest.raises(NameConflictError, match="A credential named 'STATIC' already exists in this project"):
            await credential_service.create(project.id, CredentialCreate(name="STATIC", type=STATIC.type), actor)
        other = await project_service.create(ProjectCreate(name="Other", username="other", password="pw"), actor)
        await credential_service.create(other.id, CredentialCreate(name="Static", type=STATIC.type), actor)

    async def test_the_vendor_check_stores_what_it_captured(
        self, checked_service: CredentialService, project: Project, vendor: FakeVendor, actor: Actor
    ) -> None:
        credential = await checked_service.create(project.id, BRIGHT_DATA, actor)

        assert credential.config == {"token": "T", "customer_id": "c_777"}
        assert len(vendor.calls) == 1

    async def test_a_credential_the_vendor_refuses_is_not_stored(
        self,
        checked_service: CredentialService,
        project: Project,
        vendor: FakeVendor,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        vendor.status = "suspended"

        with pytest.raises(WriteRejectedError, match="Bright Data"):
            await checked_service.create(project.id, BRIGHT_DATA, actor)

        async with sessions() as session:
            assert await CredentialRepository(session).get_by_project(project.id) == []


class TestUpdate:
    async def test_a_new_name_is_stored_without_asking_the_vendor(
        self, checked_service: CredentialService, project: Project, vendor: FakeVendor, actor: Actor
    ) -> None:
        credential = await checked_service.create(project.id, BRIGHT_DATA, actor)

        saved = await checked_service.update(project.id, credential.id, CredentialUpdate(name="Renamed"), actor)

        assert (saved.name, saved.config) == ("Renamed", credential.config)
        assert saved.updated_at > credential.updated_at
        assert len(vendor.calls) == 1

    async def test_a_changed_secret_is_checked_and_captured_values_are_carried_over(
        self, checked_service: CredentialService, project: Project, vendor: FakeVendor, actor: Actor
    ) -> None:
        credential = await checked_service.create(project.id, BRIGHT_DATA, actor)

        # The same token again: no secret moved, so the vendor is not asked, and the customer id stays.
        same = await checked_service.update(project.id, credential.id, CredentialUpdate(config={"token": "T"}), actor)
        assert (same.config, len(vendor.calls)) == ({"token": "T", "customer_id": "c_777"}, 1)

        vendor.status = "suspended"
        with pytest.raises(WriteRejectedError, match="Bright Data"):
            await checked_service.update(project.id, credential.id, CredentialUpdate(config={"token": "U"}), actor)
        assert len(vendor.calls) == 2

    async def test_a_refused_edit_leaves_the_stored_credential_as_it_was(
        self,
        credential_service: CredentialService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        """Validated on a loaded copy: the new name does not land with the rejected config (today's route kept it)."""
        credential = await credential_service.create(
            project.id, CredentialCreate(name="AWS", type="aws", config=AWS_CREDENTIAL), actor
        )

        with pytest.raises(InvalidConfigError, match="secret_key"):
            await credential_service.update(
                project.id,
                credential.id,
                CredentialUpdate(name="Renamed", config={"access_key": AWS_CREDENTIAL["access_key"]}),
                actor,
            )

        assert await stored(sessions, credential.id) == credential

    async def test_a_taken_name_is_a_name_conflict(
        self, credential_service: CredentialService, project: Project, static_credential: Credential, actor: Actor
    ) -> None:
        other = await credential_service.create(project.id, CredentialCreate(name="Other", type=STATIC.type), actor)

        with pytest.raises(NameConflictError, match="A credential named 'static' already exists"):
            await credential_service.update(project.id, other.id, CredentialUpdate(name="static"), actor)

    async def test_a_credential_of_another_project_is_not_found(
        self,
        credential_service: CredentialService,
        project_service: ProjectService,
        static_credential: Credential,
        actor: Actor,
    ) -> None:
        other = await project_service.create(ProjectCreate(name="Other", username="other", password="pw"), actor)

        with pytest.raises(NotFoundError, match="Credential not found"):
            await credential_service.update(other.id, static_credential.id, CredentialUpdate(name="Mine"), actor)
        with pytest.raises(NotFoundError, match="Credential not found"):
            await credential_service.delete(other.id, static_credential.id, actor)


class TestDelete:
    async def test_refused_while_a_connector_uses_it(
        self,
        credential_service: CredentialService,
        connector_service: ConnectorService,
        project: Project,
        static_credential: Credential,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        connector = await connector_service.create(
            project.id, ConnectorCreate(name="ISP", credential_id=static_credential.id), actor
        )

        with pytest.raises(WriteRejectedError, match="1 connector"):
            await credential_service.delete(project.id, static_credential.id, actor)
        assert await stored(sessions, static_credential.id) is not None

        await connector_service.remove(project.id, connector.id, actor)
        await credential_service.delete(project.id, static_credential.id, actor)
        assert await stored(sessions, static_credential.id) is None


class TestChangeEntries:
    async def test_each_write_appends_one_credential_entry(
        self, credential_service: CredentialService, project: Project, redis_client: RedisClient, actor: Actor
    ) -> None:
        await forget_announcements(redis_client)

        credential = await credential_service.create(
            project.id, CredentialCreate(name="Static", type=STATIC.type), actor
        )
        await credential_service.update(project.id, credential.id, CredentialUpdate(name="Renamed"), actor)
        await credential_service.delete(project.id, credential.id, actor)

        assert await announced(redis_client) == [("credential", credential.id)] * 3

    async def test_each_entry_follows_its_commit(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        project: Project,
        redis_client: RedisClient,
        actor: Actor,
    ) -> None:
        """A consumer woken by an entry reads Postgres: the write must be there by then, or it is missed."""
        witness = Witness(redis_client, lambda ref: stored(sessions, ref))
        service = CredentialService(sessions, registry, changes=witness)

        credential = await service.create(project.id, CredentialCreate(name="Static", type=STATIC.type), actor)
        renamed = await service.update(project.id, credential.id, CredentialUpdate(name="Renamed"), actor)
        await service.delete(project.id, credential.id, actor)

        assert witness.seen == [
            (ChangeKind.CREDENTIAL, credential.id, credential),
            (ChangeKind.CREDENTIAL, credential.id, renamed),
            (ChangeKind.CREDENTIAL, credential.id, None),
        ]

    async def test_a_refused_write_announces_nothing(
        self,
        credential_service: CredentialService,
        connector_service: ConnectorService,
        project: Project,
        static_credential: Credential,
        redis_client: RedisClient,
        actor: Actor,
    ) -> None:
        await connector_service.create(
            project.id, ConnectorCreate(name="ISP", credential_id=static_credential.id), actor
        )
        await forget_announcements(redis_client)

        with pytest.raises(InvalidConfigError):
            await credential_service.create(project.id, CredentialCreate(name="Nope", type="nope"), actor)
        with pytest.raises(NameConflictError):
            await credential_service.create(project.id, CredentialCreate(name="STATIC", type=STATIC.type), actor)
        with pytest.raises(WriteRejectedError, match="1 connector"):
            await credential_service.delete(project.id, static_credential.id, actor)

        assert await announced(redis_client) == []


class TestAuditLog:
    async def test_writes_record_the_config_one_key_down_with_the_secrets_masked_and_never_stored(
        self,
        credential_service: CredentialService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        config = {"username": "u", "password": "first-secret"}
        credential = await credential_service.create(
            project.id, CredentialCreate(name="Static", type=STATIC.type, config=config), actor
        )
        changed = CredentialUpdate(config={"username": "u", "password": "second-secret"})
        await credential_service.update(project.id, credential.id, changed, actor)
        await credential_service.update(project.id, credential.id, CredentialUpdate(config={"username": "u"}), actor)
        await credential_service.delete(project.id, credential.id, actor)

        entries = await audit_entries(sessions, credential.id)
        assert [(e.action, e.entity_name, e.project_id, e.changes) for e in entries] == [
            (
                "created",
                "Static",
                project.id,
                {"name": [None, "Static"], "config.username": [None, "u"], "config.password": [None, MASK]},
            ),
            ("updated", "Static", project.id, {"config.password": [MASK, MASK]}),
            ("updated", "Static", project.id, {"config.password": [MASK, None]}),
            ("deleted", "Static", project.id, {"name": ["Static", None], "config.username": ["u", None]}),
        ]
        stored = await audit_text(sessions)
        assert "first-secret" not in stored and "second-secret" not in stored

    async def test_a_cloud_key_is_recorded_as_set_changed_or_cleared_and_never_stored(
        self,
        credential_service: CredentialService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        credential = await credential_service.create(
            project.id, CredentialCreate(name="AWS", type="aws", config=AWS_CREDENTIAL), actor
        )
        changed = CredentialUpdate(config={**AWS_CREDENTIAL, "secret_key": "second-aws-secret"})
        await credential_service.update(project.id, credential.id, changed, actor)
        # The host's role stores no key pair: the key is cleared.
        await credential_service.update(project.id, credential.id, CredentialUpdate(config={"auth_mode": "host_role"}), actor)

        entries = await audit_entries(sessions, credential.id)
        assert [e.changes.get("config.secret_key") for e in entries] == [[None, MASK], [MASK, MASK], [MASK, None]]
        assert entries[-1].changes["config.access_key"] == [AWS_CREDENTIAL["access_key"], None]
        stored = await audit_text(sessions)
        assert AWS_CREDENTIAL["secret_key"] not in stored and "second-aws-secret" not in stored

    async def test_a_vendor_checked_token_is_masked_and_a_save_that_changes_nothing_records_nothing(
        self,
        checked_service: CredentialService,
        project: Project,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        credential = await checked_service.create(project.id, BRIGHT_DATA, actor)
        await checked_service.update(project.id, credential.id, CredentialUpdate(config={"token": "U"}), actor)
        await checked_service.update(project.id, credential.id, CredentialUpdate(config={"token": "U"}), actor)

        entries = await audit_entries(sessions, credential.id)
        assert [e.changes for e in entries] == [
            {"name": [None, "Bright"], "config.token": [None, MASK], "config.customer_id": [None, "c_777"]},
            {"config.token": [MASK, MASK]},
        ]

    async def test_an_edit_is_compared_with_the_credential_as_it_stands_when_written(
        self,
        checked_service: CredentialService,
        project: Project,
        vendor: FakeVendor,
        sessions: async_sessionmaker[AsyncSession],
        actor: Actor,
    ) -> None:
        """A rename lands while the vendor checks a new token; the edit then writes its copy, read before the check, so
        the rename is written over, and its entry says so rather than showing only the token."""
        credential = await checked_service.create(project.id, BRIGHT_DATA, actor)

        async def renamed() -> None:
            async with sessions() as session:
                await CredentialRepository(session).update(credential.model_copy(update={"name": "Renamed"}))
                await session.commit()

        vendor.meanwhile = renamed
        saved = await checked_service.update(project.id, credential.id, CredentialUpdate(config={"token": "U"}), actor)

        assert saved.name == "Bright"
        assert (await audit_entries(sessions, credential.id))[-1].changes == {
            "name": ["Renamed", "Bright"],
            "config.token": [MASK, MASK],
        }
