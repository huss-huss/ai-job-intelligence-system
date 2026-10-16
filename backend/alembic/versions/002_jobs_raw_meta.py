"""Add jobs_raw.meta for source metadata (remote flag, tags, salary).

Revision ID: 002
Revises: 001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs_raw", sa.Column("meta", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs_raw", "meta")
