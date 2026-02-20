# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add credentials and connectors, remove sources.

Revision ID: 003
Revises: 002
Create Date: 2024-01-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop existing proxies and sources (clean slate as per requirements)
    op.drop_table("proxy_metrics")
    op.drop_table("proxies")
    op.drop_table("sources")

    # Create credentials table
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("config", sa.JSON(), default=dict),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_credentials_project_id", "credentials", ["project_id"])
    op.create_index("ix_credentials_type", "credentials", ["type"])

    # Create connectors table
    op.create_table(
        "connectors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("config", sa.JSON(), default=dict),
        sa.Column("enabled", sa.Boolean(), default=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_connectors_project_id", "connectors", ["project_id"])
    op.create_index("ix_connectors_credential_id", "connectors", ["credential_id"])

    # Recreate proxies table with connector_id instead of source_id
    op.create_table(
        "proxies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(20), default="http"),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.String(255), nullable=True),
        sa.Column("connector_id", sa.String(36), sa.ForeignKey("connectors.id"), nullable=False),
        sa.Column("tags", sa.JSON(), default=list),
        sa.Column("metadata", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_proxies_connector_id", "proxies", ["connector_id"])
    op.create_index("ix_proxies_host_port", "proxies", ["host", "port"])

    # Recreate proxy_metrics table
    op.create_table(
        "proxy_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proxy_id", sa.String(36), sa.ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("request_count", sa.Integer(), default=0),
        sa.Column("success_count", sa.Integer(), default=0),
        sa.Column("failure_count", sa.Integer(), default=0),
        sa.Column("avg_latency_ms", sa.Float(), default=0.0),
        sa.Column("status", sa.String(20), default="unknown"),
    )


def downgrade() -> None:
    # Drop new tables
    op.drop_table("proxy_metrics")
    op.drop_table("proxies")
    op.drop_table("connectors")
    op.drop_table("credentials")

    # Recreate sources table
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("enabled", sa.Boolean(), default=True),
        sa.Column("config", sa.JSON(), default=dict),
        sa.Column("refresh_interval_seconds", sa.Integer(), default=300),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sources_name", "sources", ["name"])
    op.create_index("ix_sources_type", "sources", ["type"])
    op.create_index("ix_sources_project_id", "sources", ["project_id"])

    # Recreate proxies table with source_id
    op.create_table(
        "proxies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(20), default="http"),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.String(255), nullable=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("tags", sa.JSON(), default=list),
        sa.Column("metadata", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Recreate proxy_metrics table
    op.create_table(
        "proxy_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proxy_id", sa.String(36), sa.ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("request_count", sa.Integer(), default=0),
        sa.Column("success_count", sa.Integer(), default=0),
        sa.Column("failure_count", sa.Integer(), default=0),
        sa.Column("avg_latency_ms", sa.Float(), default=0.0),
        sa.Column("status", sa.String(20), default="unknown"),
    )

