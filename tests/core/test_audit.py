# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log's diff and masking rules on a small model, the entry's bounded text, and the actor's entries.

All pure: the actor records into a log that keeps its entries instead of writing them.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from fastapi import Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.audit import Actor, AuditLog
from api.core.auth import CurrentUser
from api.models.audit import (
    AUDIT_TEXT_WIDTHS,
    MASK,
    AuditAction,
    Audited,
    AuditEntityType,
    AuditEntry,
)
from api.models.user import UserRole
from api.routes.common import actor_of

SESSION = cast(AsyncSession, None)


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


class Gadget(Audited):
    """A recorded entity with a secret, two JSON settings and fields left out."""

    audit_type: ClassVar[AuditEntityType] = AuditEntityType.CONNECTOR
    audit_ignored: ClassVar[frozenset[str]] = Audited.audit_ignored | {"project_id", "status"}
    audit_masked: ClassVar[frozenset[str]] = frozenset({"password"})
    audit_nested: ClassVar[frozenset[str]] = frozenset({"config", "routing"})

    id: str = "g1"
    name: str = "gadget"
    project_id: str = "p1"
    description: str = ""
    password: str | None = None
    enabled: bool = True
    colour: Colour = Colour.RED
    expires_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    created_at: datetime = Field(default_factory=lambda: datetime(2026, 1, 1))
    updated_at: datetime = Field(default_factory=lambda: datetime(2026, 1, 1))


class ListedGadget(Gadget):
    """A model that lists its recorded fields positively, one of them derived."""

    def audit_fields(self) -> dict[str, Any]:
        return {"name": self.name, "password": self.password, "label": f"{self.name}:{self.colour}"}


class Recorder(AuditLog):
    """An audit log that keeps what it is given."""

    def __init__(self) -> None:
        self.calls: list[list[AuditEntry]] = []

    async def record(self, session: AsyncSession, entries: Sequence[AuditEntry]) -> None:
        self.calls.append(list(entries))

    @property
    def entries(self) -> list[AuditEntry]:
        return [entry for call in self.calls for entry in call]


class TestTheDiff:
    def test_a_creation_records_every_field_with_a_value(self) -> None:
        gadget = Gadget(
            password="hunter2", tags=["a"], config={"zone": "eu", "empty": ""}, expires_at=datetime(2027, 1, 2, 3, 4)
        )

        assert Gadget.audit_changes(None, gadget) == {
            "name": [None, "gadget"],
            "password": [None, MASK],
            "enabled": [None, True],
            "colour": [None, "red"],
            "expires_at": [None, "2027-01-02T03:04:00"],
            "tags": [None, ["a"]],
            "config.zone": [None, "eu"],
        }

    def test_a_deletion_keeps_the_last_state(self) -> None:
        gadget = Gadget(password="hunter2", routing={"weight": 3})

        assert Gadget.audit_changes(gadget, None) == {
            "name": ["gadget", None],
            "password": [MASK, None],
            "enabled": [True, None],
            "colour": ["red", None],
            "routing.weight": [3, None],
        }

    def test_an_update_records_the_fields_that_differ(self) -> None:
        before = Gadget(routing={"weight": 1, "region": "eu"}, enabled=True)
        after = before.model_copy(update={"name": "gadget-2", "routing": {"weight": 3, "region": "eu"}, "enabled": False})

        assert Gadget.audit_changes(before, after) == {
            "name": ["gadget", "gadget-2"],
            "enabled": [True, False],
            "routing.weight": [1, 3],
        }

    def test_an_update_that_changes_nothing_recorded_gives_nothing(self) -> None:
        before = Gadget(config={"zone": "eu"})
        after = before.model_copy(
            update={"status": "failing", "project_id": "p2", "updated_at": datetime(2026, 2, 2), "description": ""}
        )

        assert Gadget.audit_changes(before, after) == {}

    def test_an_absent_key_is_the_default_and_an_empty_value_says_nothing(self) -> None:
        before = Gadget(config={"zone": "eu"})
        after = Gadget(config={"weight": 2, "zone": "eu", "note": ""})

        assert Gadget.audit_changes(before, after) == {"config.weight": [None, 2]}
        assert Gadget.audit_changes(after, before) == {"config.weight": [2, None]}

    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            (None, "hunter2", [None, MASK]),
            ("hunter2", "correct horse", [MASK, MASK]),
            ("hunter2", None, [MASK, None]),
            ("hunter2", "", [MASK, None]),
        ],
    )
    def test_a_masked_field_is_compared_on_its_real_values_and_written_masked(
        self, before: str | None, after: str | None, expected: list[str | None]
    ) -> None:
        assert Gadget.audit_changes(Gadget(password=before), Gadget(password=after)) == {"password": expected}

    def test_an_unchanged_secret_records_nothing(self) -> None:
        assert Gadget.audit_changes(Gadget(password="hunter2"), Gadget(password="hunter2")) == {}

    def test_one_key_of_a_setting_can_be_masked_per_call(self) -> None:
        before = Gadget(config={"zone": "eu", "api_key": "k-1"})
        after = Gadget(config={"zone": "us", "api_key": "k-2"})

        assert Gadget.audit_changes(before, after, masked={"config.api_key"}) == {
            "config.zone": ["eu", "us"],
            "config.api_key": [MASK, MASK],
        }

    @pytest.mark.parametrize("masked", ["config.*", "config"])
    def test_a_whole_setting_can_be_masked(self, masked: str) -> None:
        before = Gadget(config={"zone": "eu"}, routing={"weight": 1})
        after = Gadget(config={"zone": "us", "api_key": "k"}, routing={"weight": 2})

        assert Gadget.audit_changes(before, after, masked={masked}) == {
            "config.zone": [MASK, MASK],
            "config.api_key": [None, MASK],
            "routing.weight": [1, 2],
        }

    def test_a_model_can_list_its_fields_positively(self) -> None:
        before = ListedGadget(password="hunter2", enabled=True)
        after = ListedGadget(name="renamed", password="hunter3", enabled=False, colour=Colour.BLUE)

        assert ListedGadget.audit_changes(before, after) == {
            "name": ["gadget", "renamed"],
            "password": [MASK, MASK],
            "label": ["gadget:red", "renamed:blue"],
        }
        assert ListedGadget.audit_changes(None, after) == {
            "name": [None, "renamed"],
            "password": [None, MASK],
            "label": [None, "renamed:blue"],
        }

    def test_an_entity_names_itself_and_its_project(self) -> None:
        gadget = Gadget(id="g9", name="nine", project_id="p9")

        assert (gadget.audit_id(), gadget.audit_name(), gadget.audit_project()) == ("g9", "nine", "p9")


class TestTheEntry:
    def test_bounded_text_is_cut_to_the_column(self) -> None:
        entry = AuditEntry(
            action="a" * 40,
            entity_type="t" * 40,
            entity_id="i" * 100,
            entity_name="n" * 300,
            actor="u" * 300,
            request_id="r" * 200,
        )

        assert {name: len(getattr(entry, name)) for name in AUDIT_TEXT_WIDTHS} == AUDIT_TEXT_WIDTHS

    def test_an_entry_has_its_own_id_and_time(self) -> None:
        first, second = AuditEntry(action="created", entity_type="proxy"), AuditEntry(action="created", entity_type="proxy")

        assert first.id != second.id and len(first.id) == 36
        assert first.created_at <= second.created_at

    def test_the_types_of_the_install_are_for_admins(self) -> None:
        open_to_all = {t for t in AuditEntityType if not t.admin_only}

        assert len(AuditEntityType) == 15
        assert open_to_all == {
            AuditEntityType.PROJECT,
            AuditEntityType.CONNECTOR,
            AuditEntityType.CREDENTIAL,
            AuditEntityType.PROXY,
            AuditEntityType.WIREGUARD_DEVICE,
            AuditEntityType.OPENVPN_DEVICE,
        }


class TestTheActor:
    @pytest.fixture
    def log(self) -> Recorder:
        return Recorder()

    @pytest.fixture
    def actor(self, log: Recorder) -> Actor:
        return log.actor("u1", "ivan", "req-1")

    async def test_a_creation_is_one_entry_naming_the_entity_its_project_and_the_actor(
        self, log: Recorder, actor: Actor
    ) -> None:
        await actor.created(SESSION, Gadget(password="hunter2"))

        [entry] = log.entries
        assert entry.model_dump(exclude={"id", "created_at", "changes"}) == {
            "action": "created",
            "entity_type": "connector",
            "entity_id": "g1",
            "entity_name": "gadget",
            "project_id": "p1",
            "actor_id": "u1",
            "actor": "ivan",
            "request_id": "req-1",
            "details": {},
        }
        assert entry.changes["password"] == [None, MASK]

    async def test_many_created_together_are_one_record_of_an_entry_each(self, log: Recorder, actor: Actor) -> None:
        gadgets = [Gadget(id=f"g{n}", name=f"gadget {n}") for n in range(3)]

        await actor.created(SESSION, gadgets, details={"via": "upload"})

        assert len(log.calls) == 1
        assert [(e.entity_id, e.details) for e in log.entries] == [(f"g{n}", {"via": "upload"}) for n in range(3)]

    async def test_an_update_records_its_diff_under_the_new_name_and_nothing_when_nothing_changed(
        self, log: Recorder, actor: Actor
    ) -> None:
        before = Gadget()

        await actor.updated(SESSION, before, before.model_copy(update={"status": "failing"}))
        await actor.updated(SESSION, before, before.model_copy(update={"name": "renamed"}), action=AuditAction.TRAFFIC_RESET)

        [entry] = log.entries
        assert (entry.action, entry.entity_name, entry.changes) == ("traffic_reset", "renamed", {"name": ["gadget", "renamed"]})

    async def test_a_deletion_records_the_last_state(self, log: Recorder, actor: Actor) -> None:
        await actor.deleted(SESSION, Gadget(), project_id="p-override")

        [entry] = log.entries
        assert (entry.action, entry.project_id, entry.changes["name"]) == ("deleted", "p-override", ["gadget", None])

    async def test_an_action_on_no_stored_entity(self, log: Recorder, actor: Actor) -> None:
        await actor.acted(SESSION, AuditAction.IMPORTED, AuditEntityType.BACKUP, entity_id="1", name="Backup", details={"n": 1})

        [entry] = log.entries
        assert (entry.action, entry.entity_type, entry.entity_id, entry.entity_name) == ("imported", "backup", "1", "Backup")
        assert (entry.changes, entry.details, entry.actor) == ({}, {"n": 1}, "ivan")

    async def test_the_actors_details_go_on_every_entry_with_the_calls(self, log: Recorder) -> None:
        actor = log.actor("u1", "ivan", None, details={"auth_token": "ci", "auth_token_id": "t1"})

        await actor.created(SESSION, Gadget())
        await actor.created(SESSION, Gadget(), details={"via": "upload"})

        assert [entry.details for entry in log.entries] == [
            {"auth_token": "ci", "auth_token_id": "t1"},
            {"auth_token": "ci", "auth_token_id": "t1", "via": "upload"},
        ]


class TestActorOf:
    @staticmethod
    def _request(log: AuditLog, request_id: str | None) -> Request:
        state = SimpleNamespace(request_id=request_id) if request_id else SimpleNamespace()
        return cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(audit_log=log)), state=state))

    async def test_a_session_user_acts_in_their_request(self) -> None:
        log = Recorder()
        user = CurrentUser(id="u1", username="ivan", role=UserRole.ADMIN)

        await actor_of(self._request(log, "req-1"), user).created(SESSION, Gadget())

        [entry] = log.entries
        assert (entry.actor_id, entry.actor, entry.request_id, entry.details) == ("u1", "ivan", "req-1", {})

    def test_a_token_user_acts_through_the_token(self) -> None:
        user = CurrentUser(id="u1", username="ivan", role=UserRole.EDITOR, auth_method="token", token_id="t1", token_name="ci")

        actor = actor_of(self._request(Recorder(), None), user)

        assert (actor.request_id, actor.details) == (None, {"auth_token": "ci", "auth_token_id": "t1"})
