"""Allow the operator retry lifecycle event.

Revision ID: 0002_add_operator_retry_event
Revises: 0001_initial_schema
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_add_operator_retry_event"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ticket_creation_job_events"
_CONSTRAINT = "ck_ticket_creation_job_events_event_type"
_INITIAL_EVENT_TYPES = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked')"
)
_EVENT_TYPES_WITH_OPERATOR_RETRY = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested')"
)


def upgrade() -> None:
    """Extend the event type check without rewriting the initial migration."""
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        _EVENT_TYPES_WITH_OPERATOR_RETRY,
    )


def downgrade() -> None:
    """Restore the initial event type check."""
    op.drop_constraint(op.f(_CONSTRAINT), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        _INITIAL_EVENT_TYPES,
    )
