"""Multi-user: users, invites, invite throttle, per-user job scores; tracker rows belong to a user.

Data migration: one owner row is created and every existing applications / talent_networks row,
plus the Telegram pause flag (app_settings "telegram"), moves to it.

* telegram_chat_id comes from TELEGRAM_CHAT_ID (NULL if unset; the bot fills it in on first use).
* profile comes from PROFILE_YAML, else the file at PROFILE_PATH. When neither is available in the
  shell running the migration, it stays NULL, which means "the deployment's profile" at runtime
  (the same PROFILE_YAML / PROFILE_PATH / profile.example.yaml chain the worker uses), so a
  migration run from a laptop never freezes the wrong profile.

Downgrade keeps the owner's rows and drops every member's data (there is nowhere to keep it).

Revision ID: 004_multi_user
Revises: 003_tracker
"""

import json
import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "004_multi_user"
down_revision: str | None = "003_tracker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _owner_profile() -> dict | None:
    import yaml

    text = os.environ.get("PROFILE_YAML", "")
    if not text:
        path = Path(os.environ.get("PROFILE_PATH", "profile.yaml"))
        if not path.is_file():
            return None
        text = path.read_text()
    data = yaml.safe_load(text) or {}
    try:  # validate and normalize when the worker package is importable (it is from the repo root)
        from worker.profile import CandidateProfile

        return CandidateProfile.model_validate(data).model_dump(mode="json")
    except ImportError:
        return data


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_chat_id", sa.String(32), nullable=True),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("profile", sa.JSON(), nullable=True),
        sa.Column("digest_size", sa.Integer(), nullable=True),
        sa.Column("digest_min_score", sa.Float(), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dashboard_token_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id"),
        sa.UniqueConstraint("dashboard_token_hash"),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index("ix_invites_created_by", "invites", ["created_by"])
    op.create_table(
        "auth_throttle",
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "user_job_scores",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("hire_probability", sa.Integer(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=True),
        sa.Column("match", sa.JSON(), nullable=False),
        sa.Column("hire", sa.JSON(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs_raw.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "job_id"),
    )
    op.create_index("ix_user_job_scores_job_id", "user_job_scores", ["job_id"])
    op.create_index("ix_user_job_scores_user_rank", "user_job_scores", ["user_id", "rank"])

    # ---- the owner, and everything that exists today moves to them
    conn = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("telegram_chat_id", sa.String),
        sa.column("display_name", sa.String),
        sa.column("role", sa.String),
        sa.column("profile", sa.JSON),
        sa.column("paused", sa.Boolean),
    )
    settings = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
    raw = conn.execute(sa.select(settings.c.value).where(settings.c.key == "telegram")).scalar()
    telegram = (json.loads(raw) if isinstance(raw, str) else raw) or {}
    conn.execute(
        users.insert().values(
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None,
            display_name="Owner",
            role="owner",
            profile=_owner_profile(),
            paused=bool(telegram.get("paused")),
        )
    )
    owner_id = conn.execute(sa.select(users.c.id).where(users.c.role == "owner")).scalar_one()
    conn.execute(settings.delete().where(settings.c.key == "telegram"))

    op.add_column("applications", sa.Column("user_id", sa.Integer(), nullable=True))
    conn.execute(sa.text("UPDATE applications SET user_id = :owner"), {"owner": owner_id})
    with op.batch_alter_table("applications") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key("fk_applications_user_id_users", "users", ["user_id"], ["id"], ondelete="CASCADE")
        batch.drop_index("ix_applications_job_id")
        batch.create_index("ix_applications_job_id", ["job_id"])
        batch.create_index("ix_applications_user_id", ["user_id"])
        batch.create_unique_constraint("uq_applications_user_job", ["user_id", "job_id"])

    # The primary key changes from (slug) to (user_id, slug): rebuild the table.
    op.create_table(
        "talent_networks_new",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="not_started"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "slug", name="pk_talent_networks"),
    )
    conn.execute(
        sa.text(
            "INSERT INTO talent_networks_new (user_id, slug, status, updated_at) "
            "SELECT :owner, slug, status, updated_at FROM talent_networks"
        ),
        {"owner": owner_id},
    )
    op.drop_table("talent_networks")
    op.rename_table("talent_networks_new", "talent_networks")


def downgrade() -> None:
    conn = op.get_bind()
    owner = conn.execute(sa.text("SELECT id, paused FROM users WHERE role = 'owner' ORDER BY id LIMIT 1")).first()
    owner_id = owner[0] if owner else -1

    op.create_table(
        "talent_networks_old",
        sa.Column("slug", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="not_started"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("slug", name="talent_networks_pkey"),
    )
    conn.execute(
        sa.text(
            "INSERT INTO talent_networks_old (slug, status, updated_at) "
            "SELECT slug, status, updated_at FROM talent_networks WHERE user_id = :owner"
        ),
        {"owner": owner_id},
    )
    op.drop_table("talent_networks")
    op.rename_table("talent_networks_old", "talent_networks")

    conn.execute(sa.text("DELETE FROM applications WHERE user_id <> :owner"), {"owner": owner_id})
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("uq_applications_user_job", type_="unique")
        batch.drop_index("ix_applications_user_id")
        batch.drop_index("ix_applications_job_id")
        batch.create_index("ix_applications_job_id", ["job_id"], unique=True)
        batch.drop_constraint("fk_applications_user_id_users", type_="foreignkey")
        batch.drop_column("user_id")

    if owner is not None and owner[1]:
        settings = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
        conn.execute(settings.insert().values(key="telegram", value={"paused": True}))

    op.drop_index("ix_user_job_scores_user_rank", table_name="user_job_scores")
    op.drop_index("ix_user_job_scores_job_id", table_name="user_job_scores")
    op.drop_table("user_job_scores")
    op.drop_table("auth_throttle")
    op.drop_index("ix_invites_created_by", table_name="invites")
    op.drop_table("invites")
    op.drop_table("users")
