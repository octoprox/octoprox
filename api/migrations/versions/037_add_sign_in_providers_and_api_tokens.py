# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add sign-in through identity providers and personal API tokens.

* ``auth_providers``: one row per sign-in provider the admin has set up
  (Google, Microsoft, GitHub), keyed by kind: the OAuth client this install
  registered with the provider and whether the button shows on the login
  page. Shared by every instance, so a login that starts on one instance
  and returns on another finds the same client.
* ``auth_identities``: which provider account a user has signed in with.
  The subject is the provider's stable id for the account; the email is what
  the provider reported at the time, kept for display. A user may have one
  identity per provider. Rows go with the user.
* ``api_tokens``: personal access tokens. Only a hash of the secret is
  stored; the prefix is the first few characters, so a token can be told
  apart in a list without revealing it. Rows go with the user, so disabling
  or deleting a user ends their tokens too.

Revision ID: 037
Revises: 036
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_providers",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("client_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("client_secret", sa.Text(), nullable=False, server_default=""),
        sa.Column("tenant_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "auth_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_auth_identities_provider_subject_unique",
        "auth_identities",
        ["provider", "subject"],
        unique=True,
    )
    # Unique per user and provider; its leading column also serves lookups by user alone.
    op.create_index(
        "ix_auth_identities_user_provider_unique",
        "auth_identities",
        ["user_id", "provider"],
        unique=True,
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_index("ix_auth_identities_user_provider_unique", table_name="auth_identities")
    op.drop_index("ix_auth_identities_provider_subject_unique", table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_table("auth_providers")
