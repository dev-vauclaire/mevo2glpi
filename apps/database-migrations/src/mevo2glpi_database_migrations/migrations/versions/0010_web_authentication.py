"""Add local web users.

Revision ID: 0010_web_authentication
Revises: 0009_rejected_retention
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_web_authentication"
down_revision: str | None = "0009_rejected_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS_TABLE = "web_users"


def upgrade() -> None:
    """Create additive local-authentication storage."""
    op.create_table(
        _USERS_TABLE,
        sa.Column("uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "normalized_username",
            sa.String(length=64, collation="C"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "session_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "failed_login_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "normalized_username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
            name=op.f("ck_web_users_normalized_username"),
        ),
        sa.CheckConstraint(
            "length(password_hash) > 0 AND password_hash LIKE '$argon2id$%'",
            name=op.f("ck_web_users_password_hash_argon2id"),
        ),
        sa.CheckConstraint(
            "session_version >= 0",
            name=op.f("ck_web_users_session_version_non_negative"),
        ),
        sa.CheckConstraint(
            "failed_login_count >= 0",
            name=op.f("ck_web_users_failed_login_count_non_negative"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_web_users_updated_at"),
        ),
        sa.PrimaryKeyConstraint("uuid", name=op.f("pk_web_users")),
        sa.UniqueConstraint(
            "normalized_username",
            name=op.f("uq_web_users_normalized_username"),
        ),
    )


def downgrade() -> None:
    """Drop the auth table only when no local account would be lost."""
    connection = op.get_bind()
    persisted_rows = connection.scalar(sa.text("SELECT count(*) FROM web_users"))
    if persisted_rows:
        raise RuntimeError("cannot downgrade while web users remain")
    op.drop_table(_USERS_TABLE)
