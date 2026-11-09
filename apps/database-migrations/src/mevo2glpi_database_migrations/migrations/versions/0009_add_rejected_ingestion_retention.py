"""Index terminal rejected ingestions for dashboard and bounded retention.

Revision ID: 0009_rejected_retention
Revises: 0008_originating_phone_cipher
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_rejected_retention"
down_revision: str | None = "0008_originating_phone_cipher"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mail_ingestions"
_INDEX = "ix_mail_ingestions_rejected_retention"


def upgrade() -> None:
    """Add a partial index without rewriting or deleting existing data."""
    op.create_index(
        op.f(_INDEX),
        _TABLE,
        ["finished_at", "uuid"],
        unique=False,
        postgresql_where=sa.text(
            "status = 'failed' AND "
            "stage = 'acknowledgement' AND "
            "disposition = 'rejected' AND "
            "job_uuid IS NULL"
        ),
    )


def downgrade() -> None:
    """Remove only the access-path index and preserve all ingestion data."""
    op.drop_index(op.f(_INDEX), table_name=_TABLE)
