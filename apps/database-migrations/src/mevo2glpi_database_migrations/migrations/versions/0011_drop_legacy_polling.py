"""Remove persistence dedicated to the retired polling transcription provider.

Revision ID: 0011_drop_legacy_polling
Revises: 0010_web_authentication
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_drop_legacy_polling"
down_revision: str | None = "0010_web_authentication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOBS_TABLE = "ticket_creation_jobs"
_EVENTS_TABLE = "ticket_creation_job_events"
_EVENT_TYPE_CONSTRAINT = "ck_ticket_creation_job_events_event_type"
_LEGACY_EVENT_TYPE = "transcription_submitted"
_EVENT_TYPES_WITHOUT_LEGACY_POLLING = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_succeeded', "
    "'transcription_failed', 'retry_scheduled', 'ticket_confirmed', "
    "'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested', 'retention_retry_scheduled', "
    "'retention_blocked', 'audio_deletion_started', 'audio_deleted')"
)
_EVENT_TYPES_WITH_LEGACY_POLLING = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'transcription_failed', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested', 'retention_retry_scheduled', "
    "'retention_blocked', 'audio_deletion_started', 'audio_deleted')"
)


def upgrade() -> None:
    """Delete obsolete polling events and remove their remote-job column."""
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM ticket_creation_job_events WHERE event_type = :event_type"
        ),
        {"event_type": _LEGACY_EVENT_TYPE},
    )
    op.drop_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        _EVENT_TYPES_WITHOUT_LEGACY_POLLING,
    )
    op.drop_column(_JOBS_TABLE, "transcription_provider_job_id")


def downgrade() -> None:
    """Restore the legacy schema without recreating deleted audit rows."""
    op.add_column(
        _JOBS_TABLE,
        sa.Column(
            "transcription_provider_job_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.drop_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        _EVENT_TYPES_WITH_LEGACY_POLLING,
    )
