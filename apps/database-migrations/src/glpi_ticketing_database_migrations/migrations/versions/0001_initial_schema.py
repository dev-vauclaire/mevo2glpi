"""Create the persistent MVP job lifecycle.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial schema and its database-owned invariants."""
    op.create_table(
        "ticket_creation_jobs",
        sa.Column("uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("storage_key", sa.String(length=41), nullable=False),
        sa.Column("audio_extension", sa.String(length=4), nullable=False),
        sa.Column("audio_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("glpi_ticket_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "transcription_provider_job_id",
            sa.String(length=255),
            nullable=True,
        ),
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
        sa.Column("last_error_scope", sa.String(length=16), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'completed', 'failed', 'blocked')",
            name=op.f("ck_ticket_creation_jobs_status"),
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN "
            "('transcription', 'glpi_creation', 'finalization')",
            name=op.f("ck_ticket_creation_jobs_stage"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND stage IS NULL) OR "
            "(status <> 'pending' AND stage IS NOT NULL)",
            name=op.f("ck_ticket_creation_jobs_stage_for_status"),
        ),
        sa.CheckConstraint(
            "audio_extension IN ('wav', 'mp3', 'ogg', 'm4a')",
            name=op.f("ck_ticket_creation_jobs_audio_extension"),
        ),
        sa.CheckConstraint(
            "audio_size_bytes > 0",
            name=op.f("ck_ticket_creation_jobs_audio_size_bytes_positive"),
        ),
        sa.CheckConstraint(
            "glpi_ticket_id IS NULL OR glpi_ticket_id > 0",
            name=op.f("ck_ticket_creation_jobs_glpi_ticket_id_positive"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_ticket_creation_jobs_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "consecutive_retry_count >= 0",
            name=op.f("ck_ticket_creation_jobs_consecutive_retry_count_non_negative"),
        ),
        sa.CheckConstraint(
            "storage_key = (uuid::text || '.' || audio_extension)",
            name=op.f("ck_ticket_creation_jobs_storage_key_matches_uuid_and_extension"),
        ),
        sa.CheckConstraint(
            "(status = 'retry_scheduled' AND next_attempt_at IS NOT NULL) OR "
            "(status <> 'retry_scheduled' AND next_attempt_at IS NULL)",
            name=op.f("ck_ticket_creation_jobs_retry_schedule"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NULL)",
            name=op.f("ck_ticket_creation_jobs_finished_at"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR "
            "(glpi_ticket_id IS NOT NULL AND stage = 'finalization')",
            name=op.f("ck_ticket_creation_jobs_completed"),
        ),
        sa.CheckConstraint(
            "last_error_scope IS NULL OR last_error_scope IN ('job', 'system')",
            name=op.f("ck_ticket_creation_jobs_last_error_scope"),
        ),
        sa.CheckConstraint(
            "(last_error_scope IS NULL AND last_error_category IS NULL "
            "AND last_error_code IS NULL) OR "
            "(last_error_scope IS NOT NULL AND last_error_category IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
            name=op.f("ck_ticket_creation_jobs_error_fields"),
        ),
        sa.CheckConstraint(
            "(status IN ('retry_scheduled', 'failed', 'blocked') "
            "AND last_error_scope IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'failed', 'blocked') "
            "AND last_error_scope IS NULL)",
            name=op.f("ck_ticket_creation_jobs_error_for_status"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error_scope = 'job'",
            name=op.f("ck_ticket_creation_jobs_failed_error_scope"),
        ),
        sa.PrimaryKeyConstraint("uuid", name="pk_ticket_creation_jobs"),
        sa.UniqueConstraint("storage_key", name="uq_ticket_creation_jobs_storage_key"),
        sa.UniqueConstraint(
            "glpi_ticket_id",
            name="uq_ticket_creation_jobs_glpi_ticket_id",
        ),
    )
    op.create_index(
        "ix_ticket_creation_jobs_pending",
        "ticket_creation_jobs",
        ["created_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_ticket_creation_jobs_retry_scheduled",
        "ticket_creation_jobs",
        ["next_attempt_at", "created_at", "uuid"],
        unique=False,
        postgresql_where=sa.text("status = 'retry_scheduled'"),
    )
    op.create_index(
        "ix_ticket_creation_jobs_dashboard",
        "ticket_creation_jobs",
        [sa.text("updated_at DESC"), sa.text("uuid DESC")],
        unique=False,
    )
    op.create_index(
        "ix_ticket_creation_jobs_status_dashboard",
        "ticket_creation_jobs",
        ["status", sa.text("updated_at DESC"), sa.text("uuid DESC")],
        unique=False,
    )
    op.create_index(
        "uq_ticket_creation_jobs_single_processing",
        "ticket_creation_jobs",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "audio_retentions",
        sa.Column("job_uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'inactive'"),
            nullable=False,
        ),
        sa.Column("hard_delete_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_glpi_status", sa.String(length=64), nullable=True),
        sa.Column(
            "check_attempt_count",
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
        sa.Column("deletion_reason", sa.String(length=32), nullable=True),
        sa.Column("deletion_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audio_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "policy IN ('ticket_or_deadline', 'deadline_only')",
            name=op.f("ck_audio_retentions_policy"),
        ),
        sa.CheckConstraint(
            "status IN ('inactive', 'scheduled', 'checking', "
            "'retry_scheduled', 'deleting', 'deleted', 'blocked')",
            name=op.f("ck_audio_retentions_status"),
        ),
        sa.CheckConstraint(
            "deletion_reason IS NULL OR deletion_reason IN "
            "('ticket_closed', 'deadline_expired', 'failed_ttl', 'blocked_ttl')",
            name=op.f("ck_audio_retentions_deletion_reason"),
        ),
        sa.CheckConstraint(
            "check_attempt_count >= 0",
            name=op.f("ck_audio_retentions_check_attempt_count_non_negative"),
        ),
        sa.CheckConstraint(
            "consecutive_retry_count >= 0",
            name=op.f("ck_audio_retentions_consecutive_retry_count_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'inactive' AND hard_delete_at IS NULL) OR "
            "(status <> 'inactive' AND hard_delete_at IS NOT NULL)",
            name=op.f("ck_audio_retentions_active_hard_delete_at"),
        ),
        sa.CheckConstraint(
            "((status = 'retry_scheduled' OR "
            "(policy = 'ticket_or_deadline' AND status = 'scheduled')) "
            "AND next_check_at IS NOT NULL) OR "
            "((status <> 'retry_scheduled' AND "
            "(policy <> 'ticket_or_deadline' OR status <> 'scheduled')) "
            "AND next_check_at IS NULL)",
            name=op.f("ck_audio_retentions_next_check_for_policy"),
        ),
        sa.CheckConstraint(
            "status <> 'checking' OR policy = 'ticket_or_deadline'",
            name=op.f("ck_audio_retentions_glpi_check_policy"),
        ),
        sa.CheckConstraint(
            "(deletion_reason IS NULL AND deletion_started_at IS NULL) OR "
            "(deletion_reason IS NOT NULL AND deletion_started_at IS NOT NULL)",
            name=op.f("ck_audio_retentions_deletion_fields"),
        ),
        sa.CheckConstraint(
            "(status IN ('deleting', 'deleted') "
            "AND deletion_reason IS NOT NULL) OR "
            "status IN ('retry_scheduled', 'blocked') OR "
            "(status IN ('inactive', 'scheduled', 'checking') "
            "AND deletion_reason IS NULL)",
            name=op.f("ck_audio_retentions_deletion_for_status"),
        ),
        sa.CheckConstraint(
            "(status = 'deleted' AND audio_deleted_at IS NOT NULL) OR "
            "(status <> 'deleted' AND audio_deleted_at IS NULL)",
            name=op.f("ck_audio_retentions_audio_deleted_status"),
        ),
        sa.CheckConstraint(
            "(last_error_category IS NULL AND last_error_code IS NULL) OR "
            "(last_error_category IS NOT NULL AND last_error_code IS NOT NULL)",
            name=op.f("ck_audio_retentions_error_fields"),
        ),
        sa.CheckConstraint(
            "(status IN ('retry_scheduled', 'blocked') "
            "AND last_error_category IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'blocked') "
            "AND last_error_category IS NULL)",
            name=op.f("ck_audio_retentions_error_for_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_uuid"],
            ["ticket_creation_jobs.uuid"],
            name="fk_audio_retentions_job_uuid_ticket_creation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_uuid", name="pk_audio_retentions"),
    )
    op.create_index(
        "ix_audio_retentions_deadline",
        "audio_retentions",
        ["hard_delete_at", "job_uuid"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('inactive', 'deleted')"),
    )
    op.create_index(
        "ix_audio_retentions_due_check",
        "audio_retentions",
        ["next_check_at", "job_uuid"],
        unique=False,
        postgresql_where=sa.text(
            "((policy = 'ticket_or_deadline' AND status = 'scheduled') "
            "OR status = 'retry_scheduled') AND "
            "next_check_at IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_audio_retentions_single_active_operation",
        "audio_retentions",
        [sa.literal_column("(1)")],
        unique=True,
        postgresql_where=sa.text("status IN ('checking', 'deleting')"),
    )

    op.create_table(
        "ticket_creation_job_events",
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("job_uuid", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("error_scope", sa.String(length=16), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("technical_code", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'completed', 'failed', 'blocked')",
            name=op.f("ck_ticket_creation_job_events_status"),
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN "
            "('transcription', 'glpi_creation', 'finalization')",
            name=op.f("ck_ticket_creation_job_events_stage"),
        ),
        sa.CheckConstraint(
            "error_scope IS NULL OR error_scope IN ('job', 'system')",
            name=op.f("ck_ticket_creation_job_events_error_scope"),
        ),
        sa.CheckConstraint(
            "actor IN ('web', 'worker_automation', 'worker_retention')",
            name=op.f("ck_ticket_creation_job_events_actor"),
        ),
        sa.CheckConstraint(
            "(error_scope IS NULL AND error_category IS NULL "
            "AND technical_code IS NULL) OR "
            "(error_scope IS NOT NULL AND error_category IS NOT NULL "
            "AND technical_code IS NOT NULL)",
            name=op.f("ck_ticket_creation_job_events_error_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["job_uuid"],
            ["ticket_creation_jobs.uuid"],
            name="fk_ticket_creation_job_events_job_uuid_ticket_creation_jobs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_ticket_creation_job_events"),
    )
    op.create_index(
        "ix_ticket_creation_job_events_history",
        "ticket_creation_job_events",
        ["job_uuid", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
        unique=False,
    )

    op.create_table(
        "worker_runtime_status",
        sa.Column("service_name", sa.String(length=32), nullable=False),
        sa.Column("instance_id", sa.Uuid(native_uuid=True), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'starting'"),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("current_job_uuid", sa.Uuid(native_uuid=True), nullable=True),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "service_name IN ('worker_automation', 'worker_retention')",
            name=op.f("ck_worker_runtime_status_service_name"),
        ),
        sa.CheckConstraint(
            "state IN ('starting', 'running', 'degraded', 'blocked', 'stopped')",
            name=op.f("ck_worker_runtime_status_state"),
        ),
        sa.CheckConstraint(
            "(last_error_category IS NULL AND last_error_code IS NULL) OR "
            "(last_error_category IS NOT NULL AND last_error_code IS NOT NULL)",
            name=op.f("ck_worker_runtime_status_error_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["current_job_uuid"],
            ["ticket_creation_jobs.uuid"],
            name="fk_worker_runtime_status_current_job_uuid_ticket_creation_jobs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("service_name", name="pk_worker_runtime_status"),
    )
    op.create_index(
        "ix_worker_runtime_status_heartbeat",
        "worker_runtime_status",
        ["last_heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the complete initial schema in reverse dependency order."""
    op.drop_index(
        "ix_worker_runtime_status_heartbeat",
        table_name="worker_runtime_status",
    )
    op.drop_table("worker_runtime_status")
    op.drop_index(
        "ix_ticket_creation_job_events_history",
        table_name="ticket_creation_job_events",
    )
    op.drop_table("ticket_creation_job_events")
    op.drop_index(
        "uq_audio_retentions_single_active_operation",
        table_name="audio_retentions",
    )
    op.drop_index("ix_audio_retentions_due_check", table_name="audio_retentions")
    op.drop_index("ix_audio_retentions_deadline", table_name="audio_retentions")
    op.drop_table("audio_retentions")
    op.drop_index(
        "ix_ticket_creation_jobs_dashboard",
        table_name="ticket_creation_jobs",
    )
    op.drop_index(
        "uq_ticket_creation_jobs_single_processing",
        table_name="ticket_creation_jobs",
    )
    op.drop_index(
        "ix_ticket_creation_jobs_status_dashboard",
        table_name="ticket_creation_jobs",
    )
    op.drop_index(
        "ix_ticket_creation_jobs_retry_scheduled",
        table_name="ticket_creation_jobs",
    )
    op.drop_index(
        "ix_ticket_creation_jobs_pending",
        table_name="ticket_creation_jobs",
    )
    op.drop_table("ticket_creation_jobs")
