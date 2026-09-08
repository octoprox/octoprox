# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 022: existing duplicate names are renamed before the unique index is created."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import Settings
from api.db.migrations import MIGRATIONS_DIR


def _alembic(url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_duplicates_are_suffixed_oldest_first(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = _alembic(test_settings.database_url_sync)
    project_id = str(uuid4())
    base = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    try:
        command.downgrade(cfg, "021")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, name, username, password, created_at, updated_at) "
                    "VALUES (:id, :name, :username, 'pw', :ts, :ts)"
                ),
                {"id": project_id, "name": f"mig022 {project_id}", "username": f"mig022_{project_id[:8]}", "ts": base},
            )
            # Three credentials sharing a name up to case, created in a known order, plus an unrelated one.
            for index, (name, day) in enumerate([("Vendor", 1), ("vendor", 2), ("VENDOR", 3), ("Other", 1)]):
                conn.execute(
                    text(
                        "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
                        "VALUES (:id, :name, 'static_proxy_provider', '{}', :project_id, :ts, :ts)"
                    ),
                    {"id": f"cred-{project_id[:8]}-{index}", "name": name, "project_id": project_id, "ts": base.replace(day=day)},
                )
            for index, name in enumerate(["Fleet", "fleet"]):
                conn.execute(
                    text(
                        "INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, updated_at) "
                        "VALUES (:id, :name, '{}', true, :project_id, :credential_id, :ts, :ts)"
                    ),
                    {"id": f"conn-{project_id[:8]}-{index}", "name": name, "project_id": project_id, "credential_id": f"cred-{project_id[:8]}-0", "ts": base.replace(day=index + 1)},
                )

        command.upgrade(cfg, "head")

        with engine.begin() as conn:
            credentials = conn.execute(
                text("SELECT name FROM credentials WHERE project_id = :p ORDER BY created_at, id"), {"p": project_id}
            ).scalars().all()
            assert credentials == ["Vendor", "Other", "vendor (2)", "VENDOR (3)"]
            connectors = conn.execute(
                text("SELECT name FROM connectors WHERE project_id = :p ORDER BY created_at"), {"p": project_id}
            ).scalars().all()
            assert connectors == ["Fleet", "fleet (2)"]
            # From here on the index does the enforcing.
            with pytest.raises(IntegrityError, match="ix_credentials_project_name_unique"):
                conn.execute(
                    text(
                        "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
                        "VALUES (:id, 'other', 'static_proxy_provider', '{}', :project_id, :ts, :ts)"
                    ),
                    {"id": str(uuid4()), "project_id": project_id, "ts": base},
                )
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
        engine.dispose()


async def test_index_present_after_fresh_migration(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text("SELECT indexname FROM pg_indexes WHERE indexname LIKE 'ix_%_project_name_unique' ORDER BY indexname")
    )
    assert rows.scalars().all() == ["ix_connectors_project_name_unique", "ix_credentials_project_name_unique"]
