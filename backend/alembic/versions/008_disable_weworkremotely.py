"""Disable the We Work Remotely source: its listings ask candidates to pay before they can apply.

Data only. A disabled source isn't scraped by the cron and its stored jobs are hidden from the
dashboard and the Telegram digest, /top and /today (app/tracker.enabled_source). The rows stay, so
re-enabling the source (PATCH /api/sources/<id>) or downgrading brings them back.

Revision ID: 008_disable_weworkremotely
Revises: 007_login_links
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_disable_weworkremotely"
down_revision: str | None = "007_login_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPE = "weworkremotely"


def _wwr_ids(conn) -> list[int]:
    rows = conn.execute(sa.text("SELECT id, name, config FROM sources")).all()
    ids = []
    for source_id, name, config in rows:
        if isinstance(config, str):  # SQLite hands JSON back as text
            config = json.loads(config or "null")
        if (config or {}).get("type") == SOURCE_TYPE or name == SOURCE_TYPE:
            ids.append(source_id)
    return ids


def _set_enabled(enabled: bool) -> None:
    conn = op.get_bind()
    for source_id in _wwr_ids(conn):
        conn.execute(sa.text("UPDATE sources SET enabled = :e WHERE id = :i"), {"e": enabled, "i": source_id})


def upgrade() -> None:
    _set_enabled(False)


def downgrade() -> None:
    _set_enabled(True)
