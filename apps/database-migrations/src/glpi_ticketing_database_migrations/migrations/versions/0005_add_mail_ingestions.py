"""Add the durable Exchange mail-ingestion lifecycle.

Revision ID: 0005_add_mail_ingestions
Revises: 0004_add_observability_queries
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_mail_ingestions"
down_revision: str | None = "0004_add_observability_queries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INGESTIONS_TABLE = "mail_ingestions"
_INGESTION_EVENTS_TABLE = "mail_ingestion_events"
_JOB_EVENTS_TABLE = "ticket_creation_job_events"
_WORKER_RUNTIME_TABLE = "worker_runtime_status"
_JOB_EVENT_ACTOR_CONSTRAINT = "ck_ticket_creation_job_events_actor"
_WORKER_SERVICE_CONSTRAINT = "ck_worker_runtime_status_service_name"
_WORKER_CURRENT_WORK_CONSTRAINT = "ck_worker_runtime_status_current_work_for_service"
_WORKER_INGESTION_FOREIGN_KEY = (
    "fk_worker_runtime_status_current_ingestion_uuid_mail_ingestions"
)

_JOB_EVENT_ACTORS_BEFORE_PUBLISHER = (
    "actor IN ('web', 'worker_automation', 'worker_retention')"
)
_JOB_EVENT_ACTORS_WITH_PUBLISHER = (
    "actor IN ('web', 'worker_automation', 'worker_retention', 'worker_publisher')"
)
_WORKER_SERVICES_BEFORE_PUBLISHER = (
    "service_name IN ('worker_automation', 'worker_retention')"
)
_WORKER_SERVICES_WITH_PUBLISHER = (
    "service_name IN ('worker_automation', 'worker_retention', 'worker_publisher')"
)


def upgrade() -> None:
    """Create publisher persistence and extend historical actor contracts."""
    op.drop_constraint(
        op.f(_JOB_EVENT_ACTOR_CONSTRAINT),
        _JOB_EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_JOB_EVENT_ACTOR_CONSTRAINT),
        _JOB_EVENTS_TABLE,
        _JOB_EVENT_ACTORS_WITH_PUBLISHER,
    )
    op.drop_constraint(
        op.f(_WORKER_SERVICE_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_WORKER_SERVICE_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        _WORKER_SERVICES_WITH_PUBLISHER,
    )

    op.create_table(
        _INGESTIONS_TABLE,
        sa.Column("uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "message_id",
            sa.String(length=512, collation="C"),
            nullable=False,
        ),
        sa.Column(
            "attachment_id",
            sa.String(length=512, collation="C"),
            nullable=True,
        ),
        sa.Column("job_uuid", sa.Uuid(native_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "stage",
            sa.String(length=32),
            server_default=sa.text("'discovery'"),
            nullable=False,
        ),
        sa.Column("disposition", sa.String(length=16), nullable=True),
        sa.Column("attachment_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "consecutive_retry_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_category", sa.String(length=64), nullable=True),
        sa.Column("rejection_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_scope", sa.String(length=16), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("first_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(btrim(message_id)) > 0",
            name=op.f("ck_mail_ingestions_message_id_not_empty"),
        ),
        sa.CheckConstraint(
            "attachment_id IS NULL OR length(btrim(attachment_id)) > 0",
            name=op.f("ck_mail_ingestions_attachment_id_not_empty"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'completed', 'failed', 'blocked')",
            name=op.f("ck_mail_ingestions_status"),
        ),
        sa.CheckConstraint(
            "stage IN ('discovery', 'validation', 'publication', 'acknowledgement')",
            name=op.f("ck_mail_ingestions_stage"),
        ),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('accepted', 'rejected')",
            name=op.f("ck_mail_ingestions_disposition"),
        ),
        sa.CheckConstraint(
            "last_error_scope IS NULL OR last_error_scope IN ('message', 'system')",
            name=op.f("ck_mail_ingestions_last_error_scope"),
        ),
        sa.CheckConstraint(
            "attachment_size_bytes IS NULL OR attachment_size_bytes >= 0",
            name=op.f("ck_mail_ingestions_attachment_size_bytes_non_negative"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_mail_ingestions_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "consecutive_retry_count >= 0",
            name=op.f("ck_mail_ingestions_consecutive_retry_count_non_negative"),
        ),
        sa.CheckConstraint(
            "status <> 'retry_scheduled' OR consecutive_retry_count > 0",
            name=op.f("ck_mail_ingestions_retry_has_attempt"),
        ),
        sa.CheckConstraint(
            "(status = 'retry_scheduled' AND next_attempt_at IS NOT NULL) OR "
            "(status <> 'retry_scheduled' AND next_attempt_at IS NULL)",
            name=op.f("ck_mail_ingestions_retry_schedule"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NULL)",
            name=op.f("ck_mail_ingestions_finished_at"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND stage = 'discovery' "
            "AND attempt_count = 0 AND consecutive_retry_count = 0 "
            "AND first_started_at IS NULL) OR "
            "(status <> 'pending' AND attempt_count > 0 "
            "AND first_started_at IS NOT NULL)",
            name=op.f("ck_mail_ingestions_attempt_state"),
        ),
        sa.CheckConstraint(
            "(stage = 'acknowledgement' AND disposition IS NOT NULL) OR "
            "(stage <> 'acknowledgement' AND disposition IS NULL)",
            name=op.f("ck_mail_ingestions_disposition_at_acknowledgement"),
        ),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition <> 'accepted' OR "
            "(job_uuid IS NOT NULL AND attachment_id IS NOT NULL "
            "AND attachment_size_bytes IS NOT NULL "
            "AND attachment_size_bytes > 0 "
            "AND rejection_category IS NULL AND rejection_code IS NULL)",
            name=op.f("ck_mail_ingestions_accepted_disposition"),
        ),
        sa.CheckConstraint(
            "(disposition = 'rejected' AND job_uuid IS NULL "
            "AND rejection_category IS NOT NULL AND rejection_code IS NOT NULL) OR "
            "((disposition IS NULL OR disposition <> 'rejected') "
            "AND rejection_category IS NULL AND rejection_code IS NULL)",
            name=op.f("ck_mail_ingestions_rejected_disposition"),
        ),
        sa.CheckConstraint(
            "job_uuid IS NULL OR "
            "(disposition IS NOT NULL AND disposition = 'accepted')",
            name=op.f("ck_mail_ingestions_job_for_accepted_disposition"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR "
            "(disposition IS NOT NULL AND disposition = 'accepted')",
            name=op.f("ck_mail_ingestions_completed_disposition"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR "
            "(disposition IS NOT NULL AND disposition = 'rejected')",
            name=op.f("ck_mail_ingestions_failed_disposition"),
        ),
        sa.CheckConstraint(
            "(last_error_scope IS NULL AND last_error_category IS NULL "
            "AND last_error_code IS NULL) OR "
            "(last_error_scope IS NOT NULL AND last_error_category IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
            name=op.f("ck_mail_ingestions_error_fields"),
        ),
        sa.CheckConstraint(
            "(status IN ('retry_scheduled', 'blocked') "
            "AND last_error_scope IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'blocked') "
            "AND last_error_scope IS NULL)",
            name=op.f("ck_mail_ingestions_error_for_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_uuid"],
            ["ticket_creation_jobs.uuid"],
            name=op.f("fk_mail_ingestions_job_uuid_ticket_creation_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("uuid", name=op.f("pk_mail_ingestions")),
        sa.UniqueConstraint(
            "job_uuid",
            name=op.f("uq_mail_ingestions_job_uuid"),
        ),
        sa.UniqueConstraint(
            "mailbox_id",
            "message_id",
            name=op.f("uq_mail_ingestions_source"),
        ),
    )
    op.create_index(
        "ix_mail_ingestions_pending",
        _INGESTIONS_TABLE,
        ["received_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_mail_ingestions_retry_scheduled",
        _INGESTIONS_TABLE,
        ["next_attempt_at", "received_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("status = 'retry_scheduled'"),
    )
    op.create_index(
        "ix_mail_ingestions_dashboard",
        _INGESTIONS_TABLE,
        [sa.text("updated_at DESC"), sa.text("uuid DESC")],
        unique=False,
    )
    op.create_index(
        "uq_mail_ingestions_single_processing",
        _INGESTIONS_TABLE,
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.add_column(
        _WORKER_RUNTIME_TABLE,
        sa.Column(
            "current_ingestion_uuid",
            sa.Uuid(native_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        op.f(_WORKER_INGESTION_FOREIGN_KEY),
        _WORKER_RUNTIME_TABLE,
        _INGESTIONS_TABLE,
        ["current_ingestion_uuid"],
        ["uuid"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f(_WORKER_CURRENT_WORK_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        "(service_name = 'worker_publisher' AND current_job_uuid IS NULL) OR "
        "(service_name <> 'worker_publisher' AND current_ingestion_uuid IS NULL)",
    )

    op.create_table(
        _INGESTION_EVENTS_TABLE,
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("ingestion_uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("error_scope", sa.String(length=16), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("technical_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'completed', 'failed', 'blocked')",
            name=op.f("ck_mail_ingestion_events_status"),
        ),
        sa.CheckConstraint(
            "stage IN ('discovery', 'validation', 'publication', 'acknowledgement')",
            name=op.f("ck_mail_ingestion_events_stage"),
        ),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('accepted', 'rejected')",
            name=op.f("ck_mail_ingestion_events_disposition"),
        ),
        sa.CheckConstraint(
            "event_type IN ('message_discovered', 'processing_started', "
            "'processing_resumed', 'attachment_validated', 'audio_published', "
            "'message_rejected', 'retry_scheduled', 'message_moved', "
            "'ingestion_completed', 'ingestion_failed', 'ingestion_blocked')",
            name=op.f("ck_mail_ingestion_events_event_type"),
        ),
        sa.CheckConstraint(
            "error_scope IS NULL OR error_scope IN ('message', 'system')",
            name=op.f("ck_mail_ingestion_events_error_scope"),
        ),
        sa.CheckConstraint(
            "(stage = 'acknowledgement' AND disposition IS NOT NULL) OR "
            "(stage <> 'acknowledgement' AND disposition IS NULL)",
            name=op.f("ck_mail_ingestion_events_disposition_at_acknowledgement"),
        ),
        sa.CheckConstraint(
            "(error_scope IS NULL AND error_category IS NULL "
            "AND technical_code IS NULL) OR "
            "(error_scope IS NOT NULL AND error_category IS NOT NULL "
            "AND technical_code IS NOT NULL)",
            name=op.f("ck_mail_ingestion_events_error_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_uuid"],
            ["mail_ingestions.uuid"],
            name=op.f("fk_mail_ingestion_events_ingestion_uuid_mail_ingestions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "event_id",
            name=op.f("pk_mail_ingestion_events"),
        ),
    )
    op.create_index(
        "ix_mail_ingestion_events_history",
        _INGESTION_EVENTS_TABLE,
        ["ingestion_uuid", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Drop publisher storage while refusing to rewrite publisher audit rows."""
    op.drop_constraint(
        op.f(_JOB_EVENT_ACTOR_CONSTRAINT),
        _JOB_EVENTS_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_JOB_EVENT_ACTOR_CONSTRAINT),
        _JOB_EVENTS_TABLE,
        _JOB_EVENT_ACTORS_BEFORE_PUBLISHER,
    )
    op.drop_constraint(
        op.f(_WORKER_SERVICE_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        op.f(_WORKER_SERVICE_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        _WORKER_SERVICES_BEFORE_PUBLISHER,
    )

    op.drop_index(
        "ix_mail_ingestion_events_history",
        table_name=_INGESTION_EVENTS_TABLE,
    )
    op.drop_table(_INGESTION_EVENTS_TABLE)
    op.drop_constraint(
        op.f(_WORKER_CURRENT_WORK_CONSTRAINT),
        _WORKER_RUNTIME_TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f(_WORKER_INGESTION_FOREIGN_KEY),
        _WORKER_RUNTIME_TABLE,
        type_="foreignkey",
    )
    op.drop_column(_WORKER_RUNTIME_TABLE, "current_ingestion_uuid")
    op.drop_index(
        "uq_mail_ingestions_single_processing",
        table_name=_INGESTIONS_TABLE,
    )
    op.drop_index(
        "ix_mail_ingestions_dashboard",
        table_name=_INGESTIONS_TABLE,
    )
    op.drop_index(
        "ix_mail_ingestions_retry_scheduled",
        table_name=_INGESTIONS_TABLE,
    )
    op.drop_index(
        "ix_mail_ingestions_pending",
        table_name=_INGESTIONS_TABLE,
    )
    op.drop_table(_INGESTIONS_TABLE)
