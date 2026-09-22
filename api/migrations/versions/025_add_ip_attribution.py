# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add IP attribution: local IP databases, observations, accuracy aggregates and runtime settings.

* ``geo_databases`` + ``geo_database_blobs``: IP databases uploaded in the
  admin panel or downloaded from vendors. The bytes sit in their own table so
  listing databases never pulls a hundred-megabyte city file.
* ``ip_observations``: every sighting of an exit IP with what each source said
  about it. Raw rows are kept for a short retention window.
* ``location_claim_stats``: daily (connector, vendor claimed, we observed)
  counts, kept forever. This is what the provider accuracy view reads.
* ``connector_exit_ips``: every distinct exit IP per connector with first and
  last sighting and a sighting count, for unique-exit and reuse figures.
  Both aggregates cascade with their connector; the raw observations do not,
  so a deleted connector's history stays traceable for the retention window.
* ``geo_settings``: the install-wide attribution settings, one typed row.
* ``projects.location_policy`` / ``projects.location_preflight``: how strict a
  project is about contradicted vendor locations, and whether sessions are
  verified before their first request; ``location_sources`` and
  ``location_conflict_rule`` override the install's default source policy
  (null inherits).

Revision ID: 025
Revises: 024
Create Date: 2026-09-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '025'
down_revision: str | None = '024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("location_policy", sa.String(10), nullable=False, server_default="off"),
    )
    op.add_column(
        "projects",
        sa.Column("location_preflight", sa.String(10), nullable=False, server_default="off"),
    )
    op.add_column("projects", sa.Column("location_sources", sa.JSON(), nullable=True))
    op.add_column("projects", sa.Column("location_conflict_rule", sa.String(10), nullable=True))

    op.create_table(
        "geo_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_sources", sa.JSON(), nullable=False),
        sa.Column("default_conflict_rule", sa.String(10), nullable=False, server_default="consensus"),
        sa.Column("echo_url", sa.String(2048), nullable=False),
        sa.Column("echo_ip_path", sa.String(255), nullable=False, server_default="ip"),
        sa.Column("echo_country_path", sa.String(255), nullable=True),
        sa.Column("echo_timeout_seconds", sa.Float(), nullable=False, server_default="15"),
        sa.Column("health_check_attribution", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("preflight_session_ttl_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("preflight_max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("observation_retention_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("exit_ip_retention_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_geo_settings_single_row"),
    )

    op.create_table(
        "geo_databases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("vendor", sa.String(20), nullable=False, server_default="other"),
        sa.Column("kind", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("format", sa.String(20), nullable=False, server_default="mmdb"),
        sa.Column("source", sa.String(10), nullable=False, server_default="upload"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("path", sa.String(1024), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("database_type", sa.String(255), nullable=False, server_default=""),
        sa.Column("build_epoch", sa.DateTime(), nullable=True),
        sa.Column("record_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ip_version", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("attribution", sa.Text(), nullable=False, server_default=""),
        sa.Column("update_url", sa.String(2048), nullable=True),
        sa.Column("update_interval_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("update_auth", sa.JSON(), nullable=False),
        sa.Column("last_update_at", sa.DateTime(), nullable=True),
        sa.Column("last_update_error", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "geo_database_blobs",
        sa.Column(
            "database_id",
            sa.String(36),
            sa.ForeignKey("geo_databases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("data", sa.LargeBinary(), nullable=False),
    )

    op.create_table(
        "ip_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("proxy_id", sa.String(36), nullable=True),
        sa.Column("connector_id", sa.String(36), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("claimed_country", sa.String(2), nullable=True),
        sa.Column("endpoint_country", sa.String(2), nullable=True),
        sa.Column("resolved_country", sa.String(2), nullable=True),
        sa.Column("resolved_source", sa.String(20), nullable=True),
        sa.Column("conflict", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disagreement", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_ip_observations_observed_at", "ip_observations", ["observed_at"])
    op.create_index("ix_ip_observations_proxy_id", "ip_observations", ["proxy_id"])
    op.create_index("ix_ip_observations_connector_id", "ip_observations", ["connector_id"])

    op.create_table(
        "connector_exit_ips",
        sa.Column(
            "connector_id",
            sa.String(36),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ip", sa.String(45), primary_key=True),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("sightings", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("country", sa.String(2), nullable=True),
    )
    op.create_index("ix_connector_exit_ips_last_seen", "connector_exit_ips", ["last_seen"])

    op.create_table(
        "location_claim_stats",
        sa.Column(
            "connector_id",
            sa.String(36),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("claimed_country", sa.String(2), primary_key=True, server_default=""),
        sa.Column("observed_country", sa.String(2), primary_key=True, server_default=""),
        sa.Column("observations", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("location_claim_stats")
    op.drop_index("ix_connector_exit_ips_last_seen", table_name="connector_exit_ips")
    op.drop_table("connector_exit_ips")
    op.drop_index("ix_ip_observations_connector_id", table_name="ip_observations")
    op.drop_index("ix_ip_observations_proxy_id", table_name="ip_observations")
    op.drop_index("ix_ip_observations_observed_at", table_name="ip_observations")
    op.drop_table("ip_observations")
    op.drop_table("geo_database_blobs")
    op.drop_table("geo_databases")
    op.drop_table("geo_settings")
    op.drop_column("projects", "location_conflict_rule")
    op.drop_column("projects", "location_sources")
    op.drop_column("projects", "location_preflight")
    op.drop_column("projects", "location_policy")
