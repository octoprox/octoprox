# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Keep what clients report about exits and connectors on each site, for exit reputation.

* ``projects.exit_reputation``: ``off`` for every existing project, which records nothing and routes as before. Its
  settings get the defaults a project starts from when it turns exit reputation on: a first avoid lasts 60 minutes
  (``exit_reputation_avoid_minutes``, doubled for each repeat), an exit IP's reports count half after 24 hours
  (``exit_reputation_half_life_hours``), a connector's evidence counts half after 100 more requests
  (``exit_reputation_connector_half_life_requests``), one fresh bad report avoids an exit
  (``exit_reputation_bad_reports``), and at least half of a site's recent reports must be bad
  (``exit_reputation_bad_share``, a percentage).
* ``exit_ip_reputation``: one row per (project, exit IP, site), made by the first report for it and gone with its
  project.
* ``connector_site_reputation``: one row per (project, connector, site, requested place), made by the first report
  naming the connector and gone with the connector or its project; the metrics flush adds the reports and the
  requests it carried there every minute.

Both tables are updated in place as evidence arrives, so new pages keep a fifth free: an update then rewrites a row in
its own page. Besides the key, each has an index on ``last_seen_day``, which the hourly retention sweep selects by:
it changes at most once a day per row, so the other updates of the day write no index entry, where an index on the
last sighting itself would add one to every update. ``exit_ip_reputation`` also has an index on ``avoid_until``, which
the controller's read of the avoids in force selects by: the end changes only when an avoid begins or lifts, so a
report that leaves it as it is still writes no index entry. ``connector_site_reputation`` also has an index on
``connector_id``, which the controller's factor read and a connector delete's cascade select by: the column never
changes, so its updates stay in place too.

Each step is safe to run again.

Revision ID: 060
Revises: 059
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "060"
down_revision: str | None = "059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADD_PROJECT_SETTINGS = """
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS exit_reputation varchar(16) NOT NULL DEFAULT 'off',
    ADD COLUMN IF NOT EXISTS exit_reputation_avoid_minutes integer NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS exit_reputation_half_life_hours integer NOT NULL DEFAULT 24,
    ADD COLUMN IF NOT EXISTS exit_reputation_connector_half_life_requests integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS exit_reputation_bad_reports integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS exit_reputation_bad_share integer NOT NULL DEFAULT 50
"""
CREATE_EXIT_IP_REPUTATION = """
CREATE TABLE IF NOT EXISTS exit_ip_reputation (
    project_id varchar(36) NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    ip varchar(45) NOT NULL,
    site varchar(253) NOT NULL,
    good_reports integer NOT NULL DEFAULT 0,
    bad_reports integer NOT NULL DEFAULT 0,
    weighted_good double precision NOT NULL DEFAULT 0,
    weighted_bad double precision NOT NULL DEFAULT 0,
    weighted_avoids double precision NOT NULL DEFAULT 0,
    weighted_at timestamp NOT NULL,
    avoid_until timestamp,
    first_seen_at timestamp NOT NULL,
    last_seen_at timestamp NOT NULL,
    last_seen_day date NOT NULL,
    last_report_at timestamp,
    last_reason varchar(64),
    PRIMARY KEY (project_id, ip, site)
) WITH (fillfactor = 80)
"""
CREATE_EXIT_IP_REPUTATION_DAY = """
CREATE INDEX IF NOT EXISTS ix_exit_ip_reputation_last_seen_day ON exit_ip_reputation (last_seen_day)
"""
CREATE_EXIT_IP_REPUTATION_AVOID = """
CREATE INDEX IF NOT EXISTS ix_exit_ip_reputation_avoid_until ON exit_ip_reputation (avoid_until)
"""
CREATE_CONNECTOR_SITE_REPUTATION = """
CREATE TABLE IF NOT EXISTS connector_site_reputation (
    project_id varchar(36) NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    connector_id varchar(36) NOT NULL REFERENCES connectors (id) ON DELETE CASCADE,
    site varchar(253) NOT NULL,
    place varchar(128) NOT NULL,
    blocked_reports integer NOT NULL DEFAULT 0,
    good_reports integer NOT NULL DEFAULT 0,
    requests bigint NOT NULL DEFAULT 0,
    weighted_blocked double precision NOT NULL DEFAULT 0,
    weighted_requests double precision NOT NULL DEFAULT 0,
    weighted_at timestamp NOT NULL,
    factor double precision,
    first_seen_at timestamp NOT NULL,
    last_seen_at timestamp NOT NULL,
    last_seen_day date NOT NULL,
    PRIMARY KEY (project_id, connector_id, site, place)
) WITH (fillfactor = 80)
"""
CREATE_CONNECTOR_SITE_REPUTATION_DAY = """
CREATE INDEX IF NOT EXISTS ix_connector_site_reputation_last_seen_day ON connector_site_reputation (last_seen_day)
"""
CREATE_CONNECTOR_SITE_REPUTATION_CONNECTOR = """
CREATE INDEX IF NOT EXISTS ix_connector_site_reputation_connector_id ON connector_site_reputation (connector_id)
"""


def upgrade() -> None:
    op.execute(ADD_PROJECT_SETTINGS)
    op.execute(CREATE_EXIT_IP_REPUTATION)
    op.execute(CREATE_EXIT_IP_REPUTATION_DAY)
    op.execute(CREATE_EXIT_IP_REPUTATION_AVOID)
    op.execute(CREATE_CONNECTOR_SITE_REPUTATION)
    op.execute(CREATE_CONNECTOR_SITE_REPUTATION_DAY)
    op.execute(CREATE_CONNECTOR_SITE_REPUTATION_CONNECTOR)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connector_site_reputation")
    op.execute("DROP TABLE IF EXISTS exit_ip_reputation")
    op.execute(
        "ALTER TABLE projects DROP COLUMN IF EXISTS exit_reputation, "
        "DROP COLUMN IF EXISTS exit_reputation_avoid_minutes, "
        "DROP COLUMN IF EXISTS exit_reputation_half_life_hours, "
        "DROP COLUMN IF EXISTS exit_reputation_connector_half_life_requests, "
        "DROP COLUMN IF EXISTS exit_reputation_bad_reports, "
        "DROP COLUMN IF EXISTS exit_reputation_bad_share"
    )
