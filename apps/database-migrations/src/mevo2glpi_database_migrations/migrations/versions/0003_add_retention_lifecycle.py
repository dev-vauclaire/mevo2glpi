"""Add the retention worker lifecycle invariants.

Revision ID: 0003_add_retention_lifecycle
Revises: 0002_add_operator_retry_event
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_retention_lifecycle"
down_revision: str | None = "0002_add_operator_retry_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETENTIONS_TABLE = "audio_retentions"
_EVENTS_TABLE = "ticket_creation_job_events"
_EVENT_TYPE_CONSTRAINT = "ck_ticket_creation_job_events_event_type"
_EVENT_TYPES_BEFORE_RETENTION = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested')"
)
_EVENT_TYPES_WITH_RETENTION = (
    "event_type IN ('job_created', 'processing_started', "
    "'processing_resumed', 'transcription_submitted', "
    "'transcription_succeeded', 'retry_scheduled', "
    "'ticket_confirmed', 'job_completed', 'job_failed', 'job_blocked', "
    "'operator_retry_requested', 'retention_retry_scheduled', "
    "'retention_blocked', 'audio_deletion_started', 'audio_deleted')"
)
_RETENTION_CONSTRAINTS = (
    (
        "retry_has_attempt",
        "status <> 'retry_scheduled' OR consecutive_retry_count > 0",
    ),
    (
        "checking_has_timestamp",
        "status <> 'checking' OR last_checked_at IS NOT NULL",
    ),
    (
        "last_glpi_status_id_positive",
        "last_glpi_status_id IS NULL OR last_glpi_status_id > 0",
    ),
    (
        "last_glpi_status_for_policy",
        "policy = 'ticket_or_deadline' OR last_glpi_status_id IS NULL",
    ),
    (
        "deletion_reason_for_policy",
        "deletion_reason IS NULL OR "
        "(policy = 'ticket_or_deadline' AND "
        "deletion_reason IN ('ticket_closed', 'deadline_expired')) OR "
        "(policy = 'deadline_only' AND "
        "deletion_reason IN ('failed_ttl', 'blocked_ttl'))",
    ),
    (
        "deadline_retry_has_deletion_context",
        "policy <> 'deadline_only' OR "
        "status NOT IN ('retry_scheduled', 'blocked') OR "
        "(deletion_reason IS NOT NULL AND deletion_started_at IS NOT NULL)",
    ),
    (
        "audio_deleted_after_deletion_started",
        "audio_deleted_at IS NULL OR "
        "(deletion_started_at IS NOT NULL AND "
        "audio_deleted_at >= deletion_started_at)",
    ),
    (
        "next_check_before_deadline",
        "deletion_started_at IS NOT NULL OR next_check_at IS NULL OR "
        "next_check_at <= hard_delete_at",
    ),
)


def upgrade() -> None:
    """Migrate retention status storage and enforce the worker state machine."""
    op.alter_column(
        _RETENTIONS_TABLE,
        "last_glpi_status",
        new_column_name="last_glpi_status_id",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        _RETENTIONS_TABLE,
        "last_glpi_status_id",
        type_=sa.SmallInteger(),
        existing_type=sa.String(length=64),
        existing_nullable=True,
        postgresql_using="NULLIF(btrim(last_glpi_status_id), '')::smallint",
    )

    for constraint_name, condition in _RETENTION_CONSTRAINTS:
        op.create_check_constraint(
            op.f(f"ck_{_RETENTIONS_TABLE}_{constraint_name}"),
            _RETENTIONS_TABLE,
            condition,
        )

    op.drop_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        _EVENT_TYPES_WITH_RETENTION,
    )


def downgrade() -> None:
    """Restore the pre-retention event and status-column contracts."""
    op.drop_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_EVENT_TYPE_CONSTRAINT),
        _EVENTS_TABLE,
        _EVENT_TYPES_BEFORE_RETENTION,
    )

    for constraint_name, _condition in reversed(_RETENTION_CONSTRAINTS):
        op.drop_constraint(
            op.f(f"ck_{_RETENTIONS_TABLE}_{constraint_name}"),
            _RETENTIONS_TABLE,
            type_="check",
        )

    op.alter_column(
        _RETENTIONS_TABLE,
        "last_glpi_status_id",
        type_=sa.String(length=64),
        existing_type=sa.SmallInteger(),
        existing_nullable=True,
        postgresql_using="last_glpi_status_id::text",
    )
    op.alter_column(
        _RETENTIONS_TABLE,
        "last_glpi_status_id",
        new_column_name="last_glpi_status",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )
