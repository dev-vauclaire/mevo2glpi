"""Audit terminal transcription failures that continue to ticket creation.

Revision ID: 0007_transcription_failed_event
Revises: 0006_add_ogg_replay_support
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_transcription_failed_event"
down_revision: str | None = "0006_add_ogg_replay_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ticket_creation_job_events"
_CONSTRAINT = "ck_ticket_creation_job_events_event_type"
_EVENT_TYPES_BEFORE_TRANSCRIPTION_FAILURE = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested', 'retention_retry_scheduled', "
    "'retention_blocked', 'audio_deletion_started', 'audio_deleted')"
)
_EVENT_TYPES_WITH_TRANSCRIPTION_FAILURE = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'transcription_failed', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested', 'retention_retry_scheduled', "
    "'retention_blocked', 'audio_deletion_started', 'audio_deleted')"
)


def upgrade() -> None:
    """Allow the sanitized transcription failure audit event."""
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        _EVENT_TYPES_WITH_TRANSCRIPTION_FAILURE,
    )


def downgrade() -> None:
    """Restore the event contract used before fallback tickets."""
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        _EVENT_TYPES_BEFORE_TRANSCRIPTION_FAILURE,
    )
