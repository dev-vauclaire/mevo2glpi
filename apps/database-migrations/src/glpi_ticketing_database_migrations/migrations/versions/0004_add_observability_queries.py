"""Add invariants and index supporting observability queries.

Revision ID: 0004_add_observability_queries
Revises: 0003_add_retention_lifecycle
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_observability_queries"
down_revision: str | None = "0003_add_retention_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETENTIONS_TABLE = "audio_retentions"
_RUNTIME_TABLE = "worker_runtime_status"
_RETENTION_STATUS_INDEX = "ix_audio_retentions_status_dashboard"
_RUNTIME_ERROR_STATE_CONSTRAINT = "ck_worker_runtime_status_error_for_state"


def upgrade() -> None:
    """Add bounded alert-query support and enforce sanitized runtime states."""
    op.create_index(
        _RETENTION_STATUS_INDEX,
        _RETENTIONS_TABLE,
        ["status", sa.text("updated_at DESC"), sa.text("job_uuid DESC")],
        unique=False,
    )
    op.create_check_constraint(
        op.f(_RUNTIME_ERROR_STATE_CONSTRAINT),
        _RUNTIME_TABLE,
        "(state IN ('degraded', 'blocked') "
        "AND last_error_category IS NOT NULL) OR "
        "(state NOT IN ('degraded', 'blocked') "
        "AND last_error_category IS NULL)",
    )


def downgrade() -> None:
    """Restore the schema used before dashboard alert aggregation."""
    op.drop_constraint(
        op.f(_RUNTIME_ERROR_STATE_CONSTRAINT),
        _RUNTIME_TABLE,
        type_="check",
    )
    op.drop_index(_RETENTION_STATUS_INDEX, table_name=_RETENTIONS_TABLE)
