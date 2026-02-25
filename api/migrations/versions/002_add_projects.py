# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add projects table and project_id to sources.

Revision ID: 002
Revises: 001
Create Date: 2026-02-13 00:00:00.000000

"""
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Default project ID for migration
DEFAULT_PROJECT_ID = str(uuid4())


def upgrade() -> None:
    # Create projects table
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), default=""),
        sa.Column("username", sa.String(255), nullable=False, unique=True),
        sa.Column("password", sa.String(255), nullable=False),
        sa.Column("routing_strategy", sa.String(50), default="round_robin"),
        sa.Column("health_check_interval", sa.Integer(), default=60),
        sa.Column("health_check_timeout", sa.Integer(), default=30),
        sa.Column("connection_timeout", sa.Integer(), default=30),
        sa.Column("max_retries", sa.Integer(), default=3),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_projects_name", "projects", ["name"])
    op.create_index("ix_projects_username", "projects", ["username"])

    # Create default project for existing data
    op.execute(
        sa.text(
            """
            INSERT INTO projects (id, name, description, username, password, routing_strategy,
                                  health_check_interval, health_check_timeout, connection_timeout,
                                  max_retries, created_at, updated_at)
            VALUES (:id, :name, :description, :username, :password, :routing_strategy,
                    :health_check_interval, :health_check_timeout, :connection_timeout,
                    :max_retries, NOW(), NOW())
            """
        ).bindparams(
            id=DEFAULT_PROJECT_ID,
            name="Default Project",
            description="Default project created during migration for existing sources",
            username="default",
            password="changeme",
            routing_strategy="round_robin",
            health_check_interval=60,
            health_check_timeout=30,
            connection_timeout=30,
            max_retries=3,
        )
    )

    # Add project_id column to sources (nullable first for migration)
    op.add_column(
        "sources",
        sa.Column("project_id", sa.String(36), nullable=True),
    )

    # Update existing sources to use default project
    op.execute(
        sa.text(
            "UPDATE sources SET project_id = :project_id WHERE project_id IS NULL"
        ).bindparams(project_id=DEFAULT_PROJECT_ID)
    )

    # Make project_id non-nullable and add foreign key
    op.alter_column("sources", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_sources_project_id",
        "sources",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_sources_project_id", "sources", ["project_id"])


def downgrade() -> None:
    # Remove foreign key and index
    op.drop_constraint("fk_sources_project_id", "sources", type_="foreignkey")
    op.drop_index("ix_sources_project_id", table_name="sources")

    # Remove project_id column
    op.drop_column("sources", "project_id")

    # Drop projects table
    op.drop_index("ix_projects_username", table_name="projects")
    op.drop_index("ix_projects_name", table_name="projects")
    op.drop_table("projects")

