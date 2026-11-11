# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The audit log's rows (insert, one entity's history by cursor, the retention delete) and the system settings row."""

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.audit import AuditLog
from api.db.audit_repository import AuditRepository, SystemSettingsRepository
from api.models.audit import MASK, AuditEntityType, AuditEntry
from api.models.system import SystemSettings

T0 = datetime(2026, 11, 1, 12, 0, 0)


def _entry(entry_id: str, minutes: int, entity_id: str = "c1", **fields: object) -> AuditEntry:
    return AuditEntry(
        id=entry_id,
        created_at=T0 + timedelta(minutes=minutes),
        action="updated",
        entity_type=AuditEntityType.CONNECTOR,
        entity_id=entity_id,
        **fields,  # type: ignore[arg-type]
    )


class TestAuditRepository:
    async def test_entries_are_stored_as_given(self, db_session: AsyncSession) -> None:
        entry = _entry(
            "e1",
            0,
            entity_name="eu-pool",
            project_id="p1",
            actor_id="u1",
            actor="ivan",
            request_id="req-1",
            changes={"routing_config.weight": [None, 3]},
            details={"auth_token": "ci", "expires_at": datetime(2027, 1, 1)},
        )
        await AuditRepository(db_session).add([entry, _entry("e2", 1)])
        await db_session.commit()

        history = await AuditRepository(db_session).history(AuditEntityType.CONNECTOR, "c1", 10)

        assert [e.id for e in history.entries] == ["e2", "e1"] and history.more is False
        stored = history.entries[1]
        assert stored.model_dump(exclude={"details"}) == entry.model_dump(exclude={"details"})
        # Details are JSON: a time in them is stored as its ISO text.
        assert stored.details == {"auth_token": "ci", "expires_at": "2027-01-01T00:00:00"}

    async def test_nothing_to_add_writes_nothing(self, db_session: AsyncSession) -> None:
        await AuditRepository(db_session).add([])

        assert (await db_session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one() == 0

    async def test_history_is_one_entitys_newest_first_page_by_page(self, db_session: AsyncSession) -> None:
        repository = AuditRepository(db_session)
        # e3 and e4 share a time: the id breaks the tie.
        await repository.add(
            [
                _entry("e1", 1),
                _entry("e2", 2),
                _entry("e3", 3),
                _entry("e4", 3),
                _entry("e5", 4),
                _entry("other", 5, entity_id="c2"),
                AuditEntry(id="proxy", created_at=T0, action="created", entity_type="proxy", entity_id="c1"),
            ]
        )
        await db_session.commit()

        first = await repository.history("connector", "c1", 2)
        second = await repository.history("connector", "c1", 2, before=first.entries[-1].id)
        last = await repository.history("connector", "c1", 2, before=second.entries[-1].id)

        assert ([e.id for e in first.entries], first.more) == (["e5", "e4"], True)
        assert ([e.id for e in second.entries], second.more) == (["e3", "e2"], True)
        assert ([e.id for e in last.entries], last.more) == (["e1"], False)

    async def test_entries_recorded_while_paging_never_shift_a_page(self, db_session: AsyncSession) -> None:
        repository = AuditRepository(db_session)
        await repository.add([_entry("e1", 1), _entry("e2", 2), _entry("e3", 3)])
        await db_session.commit()
        first = await repository.history("connector", "c1", 2)

        await repository.add([_entry("e4", 4)])
        await db_session.commit()
        second = await repository.history("connector", "c1", 2, before=first.entries[-1].id)

        assert [e.id for e in second.entries] == ["e1"]

    async def test_a_cursor_retention_removed_reads_nothing_more_and_an_empty_one_reads_from_the_newest(
        self, db_session: AsyncSession
    ) -> None:
        repository = AuditRepository(db_session)
        await repository.add([_entry("e1", 1)])
        await db_session.commit()

        gone = await repository.history("connector", "c1", 2, before="no-such-entry")
        newest = await repository.history("connector", "c1", 2, before="")

        assert (gone.entries, gone.more) == ([], False)
        assert [e.id for e in newest.entries] == ["e1"]

    async def test_retention_deletes_only_the_older_entries(self, db_session: AsyncSession) -> None:
        repository = AuditRepository(db_session)
        await repository.add([_entry("old", 0), _entry("edge", 10), _entry("new", 20)])
        await db_session.commit()

        assert await repository.delete_older_than(T0 + timedelta(minutes=10)) == 1
        await db_session.commit()

        assert [e.id for e in (await repository.history("connector", "c1", 10)).entries] == ["new", "edge"]


class TestSystemSettingsRepository:
    async def test_no_row_means_the_defaults(self, db_session: AsyncSession) -> None:
        assert await SystemSettingsRepository(db_session).get() == SystemSettings()
        assert SystemSettings().audit_retention_days == 365

    async def test_each_setting_is_saved_without_touching_the_other(self, db_session: AsyncSession) -> None:
        repository = SystemSettingsRepository(db_session)

        retention = await repository.set_audit_retention(30)
        licensed = await repository.set_license_key("oxl1.key.sig")
        await db_session.commit()
        unchanged = await repository.set_audit_retention(30)

        assert (retention.audit_retention_days, retention.license_key) == (30, None)
        assert (licensed.audit_retention_days, licensed.license_key) == (30, "oxl1.key.sig")
        assert unchanged.license_key == "oxl1.key.sig"
        assert await repository.get() == unchanged

    async def test_a_license_saved_first_keeps_the_default_retention(self, db_session: AsyncSession) -> None:
        stored = await SystemSettingsRepository(db_session).set_license_key("oxl1.key.sig")

        assert stored.audit_retention_days == 365

    async def test_the_license_key_never_reaches_the_audit_log(self, db_session: AsyncSession) -> None:
        """Set, changed and cleared, the key is recorded only as set, changed or cleared."""
        repository = SystemSettingsRepository(db_session)
        actor = AuditLog().actor("u1", "admin", "req-1")
        keys = ["oxl1.first-secret-key.sig", "oxl1.second-secret-key.sig", None]
        for key in keys:
            before = await repository.get()
            await actor.updated(db_session, before, await repository.set_license_key(key))
        await db_session.commit()

        history = await AuditRepository(db_session).history(AuditEntityType.SYSTEM_SETTINGS, "1", 10)
        assert [(e.entity_name, e.changes) for e in reversed(history.entries)] == [
            ("System settings", {"license_key": [None, MASK]}),
            ("System settings", {"license_key": [MASK, MASK]}),
            ("System settings", {"license_key": [MASK, None]}),
        ]
        stored = (
            await db_session.execute(text("SELECT string_agg(CAST(changes AS text) || CAST(details AS text), '') FROM audit_log"))
        ).scalar_one()
        assert not any(key in stored for key in keys if key)
