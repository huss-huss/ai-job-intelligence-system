"""Web sessions: the "Log in with Telegram" dashboard sessions.

Only the SHA-256 of each session id is stored. Rows go with their user (ON DELETE CASCADE). Downgrade
drops the table, which logs everyone out of the dashboard; nothing else changes.

Revision ID: 006_web_sessions
Revises: 005_llm_budget
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_web_sessions"
down_revision: str | None = "005_llm_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent_hash", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
