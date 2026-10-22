"""Login links: one-time links the bot sends with /login to log in to the dashboard.

Only the SHA-256 of each link token is stored; using a link deletes its row. Rows go with their user
(ON DELETE CASCADE). Downgrade drops the table, which only voids links not used yet.

Revision ID: 007_login_links
Revises: 006_web_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_login_links"
down_revision: str | None = "006_web_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_login_links_user_id", "login_links", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_login_links_user_id", table_name="login_links")
    op.drop_table("login_links")
