# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alembic environment, with an autogenerate that leaves alone the version columns only 5.x writes."""

from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool

from api.db.base import Base
from api.db.models import (  # noqa: F401
    ConnectorModel,
    CredentialModel,
    ProjectModel,
    ProxyMetricsModel,
    ProxyModel,
    UserModel,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Note: We don't use fileConfig() here because logging is configured
# in api/core/logging.py to avoid duplicate log handlers.

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# The tables whose version column 5.x still writes and 6.0 no longer maps.
_FIVE_X_VERSIONED = frozenset({"projects", "credentials", "connectors", "proxies"})


def include_object(item: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Whether autogenerate compares a schema item: never a version column only 5.x writes."""
    if type_ == "column" and name == "version":
        return item.table.name not in _FIVE_X_VERSIONED
    return True


def run_migrations_offline() -> None:
    """Emit the migrations as SQL for the configured URL, with no database connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations on a connection of their own to the configured URL."""
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("sqlalchemy.url is not set in alembic configuration")
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
