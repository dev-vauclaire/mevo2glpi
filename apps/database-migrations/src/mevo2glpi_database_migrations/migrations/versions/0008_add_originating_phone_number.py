"""Store protected originating phone numbers until GLPI confirmation.

Revision ID: 0008_originating_phone_cipher
Revises: 0007_transcription_failed_event
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_originating_phone_cipher"
down_revision: str | None = "0007_transcription_failed_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mail_ingestions"
_COLUMN = "originating_phone_number_ciphertext"
_CONSTRAINT = "ck_mail_ingestions_originating_phone_number_ciphertext_size"
_MAX_CIPHERTEXT_BYTES = 128


def upgrade() -> None:
    """Add one nullable, bounded ciphertext without backfilling old rows."""
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.LargeBinary(), nullable=True),
    )
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        f"{_COLUMN} IS NULL OR "
        f"(octet_length({_COLUMN}) > 0 AND "
        f"octet_length({_COLUMN}) <= {_MAX_CIPHERTEXT_BYTES})",
    )


def downgrade() -> None:
    """Remove the protected value only when no in-flight value would be lost."""
    connection = op.get_bind()
    protected_value_count = connection.scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL")
    )
    if protected_value_count:
        raise RuntimeError(
            "cannot downgrade while originating phone number ciphertexts remain"
        )
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
