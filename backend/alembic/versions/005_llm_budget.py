"""LLM budget: the llm_usage ledger, and each user's resume text for cover letters.

llm_usage holds token counts and cost per call (no prompts or generated text); user_id is SET NULL when
a member is removed, so money already spent still counts toward the month. Downgrade drops the ledger
and every stored resume.

Revision ID: 005_llm_budget
Revises: 004_multi_user
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_llm_budget"
down_revision: str | None = "004_multi_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("feature", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_day_feature", "llm_usage", ["day", "feature"])
    op.create_index("ix_llm_usage_user_day", "llm_usage", ["user_id", "day"])
    op.add_column("users", sa.Column("resume_text", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("resume_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Not batch mode: rebuilding "users" would drop it, and with foreign keys on SQLite's DROP TABLE
        # cascades into every table that references users. SQLite 3.35+ drops a column in place.
        op.execute("ALTER TABLE users DROP COLUMN resume_updated_at")
        op.execute("ALTER TABLE users DROP COLUMN resume_text")
    else:
        op.drop_column("users", "resume_updated_at")
        op.drop_column("users", "resume_text")
    op.drop_index("ix_llm_usage_user_day", table_name="llm_usage")
    op.drop_index("ix_llm_usage_day_feature", table_name="llm_usage")
    op.drop_table("llm_usage")
