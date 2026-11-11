from enum import StrEnum

import pytest
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import mevo2glpi_database as database_api
from mevo2glpi_database.models import (
    AudioExtension,
    AudioRetention,
    Base,
    DeletionReason,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEvent,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    WebUser,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerServiceName,
)


@pytest.mark.parametrize(
    ("enum_type", "expected_values"),
    [
        (
            JobStatus,
            {
                "pending",
                "processing",
                "retry_scheduled",
                "completed",
                "failed",
                "blocked",
            },
        ),
        (
            JobStage,
            {"transcription", "glpi_creation", "finalization"},
        ),
        (AudioExtension, {"wav", "mp3", "ogg", "m4a"}),
        (ErrorScope, {"job", "system"}),
        (
            MailIngestionStatus,
            {
                "pending",
                "processing",
                "retry_scheduled",
                "completed",
                "failed",
                "blocked",
            },
        ),
        (
            MailIngestionStage,
            {"discovery", "validation", "publication", "acknowledgement"},
        ),
        (MailIngestionDisposition, {"accepted", "rejected"}),
        (MailIngestionErrorScope, {"message", "system"}),
        (
            MailIngestionEventType,
            {
                "message_discovered",
                "processing_started",
                "processing_resumed",
                "attachment_validated",
                "audio_published",
                "message_rejected",
                "retry_scheduled",
                "message_moved",
                "ingestion_completed",
                "ingestion_failed",
                "ingestion_blocked",
                "operator_replay_requested",
            },
        ),
        (RetentionPolicy, {"ticket_or_deadline", "deadline_only"}),
        (
            RetentionStatus,
            {
                "inactive",
                "scheduled",
                "checking",
                "retry_scheduled",
                "deleting",
                "deleted",
                "blocked",
            },
        ),
        (
            DeletionReason,
            {
                "ticket_closed",
                "deadline_expired",
                "failed_ttl",
                "blocked_ttl",
            },
        ),
        (
            JobEventActor,
            {"web", "worker_publisher", "worker_automation", "worker_retention"},
        ),
        (
            JobEventType,
            {
                "job_created",
                "processing_started",
                "processing_resumed",
                "transcription_succeeded",
                "transcription_failed",
                "retry_scheduled",
                "ticket_confirmed",
                "job_completed",
                "job_failed",
                "job_blocked",
                "operator_retry_requested",
                "retention_retry_scheduled",
                "retention_blocked",
                "audio_deletion_started",
                "audio_deleted",
            },
        ),
        (
            WorkerServiceName,
            {"worker_publisher", "worker_automation", "worker_retention"},
        ),
        (
            WorkerRuntimeState,
            {"starting", "running", "degraded", "blocked", "stopped"},
        ),
    ],
)
def test_persisted_enum_values_match_contract(
    enum_type: type[StrEnum],
    expected_values: set[str],
) -> None:
    assert {member.value for member in enum_type} == expected_values


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "ticket_creation_jobs",
        "audio_retentions",
        "ticket_creation_job_events",
        "mail_ingestions",
        "mail_ingestion_events",
        "worker_runtime_status",
        "web_users",
    }


@pytest.mark.parametrize(
    ("model", "table_name"),
    [
        (TicketCreationJob, "ticket_creation_jobs"),
        (AudioRetention, "audio_retentions"),
        (TicketCreationJobEvent, "ticket_creation_job_events"),
        (WorkerRuntimeStatus, "worker_runtime_status"),
        (WebUser, "web_users"),
    ],
)
def test_public_model_imports_use_shared_metadata(
    model: type[Base],
    table_name: str,
) -> None:
    assert model.__table__ is Base.metadata.tables[table_name]
    assert getattr(database_api, model.__name__) is model


@pytest.mark.parametrize(
    ("model", "table_name"),
    [
        (MailIngestion, "mail_ingestions"),
        (MailIngestionEvent, "mail_ingestion_events"),
    ],
)
def test_mail_ingestion_model_imports_use_shared_metadata(
    model: type[Base],
    table_name: str,
) -> None:
    assert model.__table__ is Base.metadata.tables[table_name]


def test_datetime_columns_are_timezone_aware() -> None:
    datetime_columns = [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    ]

    assert datetime_columns
    assert all(column.type.timezone for column in datetime_columns)


def test_metadata_uses_deterministic_constraint_names() -> None:
    for table in Base.metadata.tables.values():
        assert table.primary_key.name == f"pk_{table.name}"
        for constraint in table.constraints:
            assert constraint.name is not None

    jobs = Base.metadata.tables["ticket_creation_jobs"]
    assert {constraint.name for constraint in jobs.constraints} >= {
        "ck_ticket_creation_jobs_status",
        "ck_ticket_creation_jobs_storage_key_matches_uuid_and_extension",
        "uq_ticket_creation_jobs_storage_key",
        "uq_ticket_creation_jobs_glpi_ticket_id",
    }


@pytest.mark.parametrize(
    ("table_name", "column_names"),
    [
        (
            "ticket_creation_jobs",
            {"status", "stage", "audio_extension", "last_error_scope"},
        ),
        ("audio_retentions", {"policy", "status", "deletion_reason"}),
        (
            "ticket_creation_job_events",
            {"status", "stage", "event_type", "error_scope", "actor"},
        ),
        (
            "mail_ingestions",
            {
                "status",
                "stage",
                "disposition",
                "audio_extension",
                "last_error_scope",
            },
        ),
        (
            "mail_ingestion_events",
            {"status", "stage", "disposition", "event_type", "error_scope"},
        ),
        ("worker_runtime_status", {"service_name", "state"}),
    ],
)
def test_persisted_state_columns_use_varchar(
    table_name: str,
    column_names: set[str],
) -> None:
    table = Base.metadata.tables[table_name]

    assert all(
        isinstance(table.c[column_name].type, String) for column_name in column_names
    )


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("ticket_creation_jobs", "uuid"),
        ("audio_retentions", "job_uuid"),
        ("ticket_creation_job_events", "job_uuid"),
        ("mail_ingestions", "uuid"),
        ("mail_ingestions", "mailbox_id"),
        ("mail_ingestions", "job_uuid"),
        ("mail_ingestion_events", "ingestion_uuid"),
        ("worker_runtime_status", "instance_id"),
        ("worker_runtime_status", "current_job_uuid"),
        ("worker_runtime_status", "current_ingestion_uuid"),
        ("web_users", "uuid"),
    ],
)
def test_uuid_columns_use_native_uuid_mapping(
    table_name: str,
    column_name: str,
) -> None:
    column_type = Base.metadata.tables[table_name].c[column_name].type

    assert isinstance(column_type, Uuid)
    assert column_type.native_uuid is True


def test_glpi_status_uses_a_nullable_small_integer_identifier() -> None:
    retentions = Base.metadata.tables["audio_retentions"]

    assert "last_glpi_status" not in retentions.c
    assert isinstance(retentions.c.last_glpi_status_id.type, SmallInteger)
    assert retentions.c.last_glpi_status_id.nullable is True


def test_database_schema_cannot_store_audio_or_transcription_content() -> None:
    column_names = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
    }

    assert "audio" not in column_names
    assert "audio_bytes" not in column_names
    assert "transcription" not in column_names
    assert "subject" not in column_names
    assert "body" not in column_names
    assert "sender" not in column_names
    assert "attachment_filename" not in column_names


def test_web_auth_schema_stores_no_session_or_provider_identity() -> None:
    assert "web_sessions" not in Base.metadata.tables
    assert "web_auth_events" not in Base.metadata.tables


def test_web_auth_invariants_are_enforced_by_database_checks() -> None:
    users = Base.metadata.tables["web_users"]
    user_check_names = {
        constraint.name
        for constraint in users.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert user_check_names >= {
        "ck_web_users_normalized_username",
        "ck_web_users_password_hash_argon2id",
        "ck_web_users_session_version_non_negative",
        "ck_web_users_failed_login_count_non_negative",
        "ck_web_users_updated_at",
    }


def test_mail_source_identity_is_unique_and_case_sensitive() -> None:
    ingestions = Base.metadata.tables["mail_ingestions"]
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in ingestions.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert unique_constraints["uq_mail_ingestions_source"] == (
        "mailbox_id",
        "message_id",
    )
    assert ingestions.c.message_id.type.collation == "C"
    assert ingestions.c.attachment_id.type.collation == "C"


def test_originating_phone_number_is_only_stored_as_nullable_ciphertext() -> None:
    ingestions = Base.metadata.tables["mail_ingestions"]
    check_names = {
        constraint.name
        for constraint in ingestions.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "originating_phone_number" not in ingestions.c
    assert ingestions.c.originating_phone_number_ciphertext.nullable is True
    assert "ck_mail_ingestions_originating_phone_number_ciphertext_size" in check_names


def test_storage_key_and_state_invariants_have_database_checks() -> None:
    jobs = Base.metadata.tables["ticket_creation_jobs"]
    check_names = {
        constraint.name
        for constraint in jobs.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names >= {
        "ck_ticket_creation_jobs_storage_key_matches_uuid_and_extension",
        "ck_ticket_creation_jobs_retry_schedule",
        "ck_ticket_creation_jobs_finished_at",
        "ck_ticket_creation_jobs_completed",
        "ck_ticket_creation_jobs_stage_for_status",
        "ck_ticket_creation_jobs_error_fields",
        "ck_ticket_creation_jobs_error_for_status",
        "ck_ticket_creation_jobs_failed_error_scope",
    }


def test_retention_invariants_have_database_checks() -> None:
    retentions = Base.metadata.tables["audio_retentions"]
    check_names = {
        constraint.name
        for constraint in retentions.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names >= {
        "ck_audio_retentions_active_hard_delete_at",
        "ck_audio_retentions_next_check_for_policy",
        "ck_audio_retentions_glpi_check_policy",
        "ck_audio_retentions_checking_has_timestamp",
        "ck_audio_retentions_last_glpi_status_id_positive",
        "ck_audio_retentions_last_glpi_status_for_policy",
        "ck_audio_retentions_deletion_fields",
        "ck_audio_retentions_deletion_for_status",
        "ck_audio_retentions_deletion_reason_for_policy",
        "ck_audio_retentions_deadline_retry_has_deletion_context",
        "ck_audio_retentions_audio_deleted_status",
        "ck_audio_retentions_audio_deleted_after_deletion_started",
        "ck_audio_retentions_next_check_before_deadline",
        "ck_audio_retentions_retry_has_attempt",
        "ck_audio_retentions_error_fields",
        "ck_audio_retentions_error_for_status",
    }


def test_mail_ingestion_invariants_have_database_checks() -> None:
    ingestions = Base.metadata.tables["mail_ingestions"]
    check_names = {
        constraint.name
        for constraint in ingestions.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names >= {
        "ck_mail_ingestions_message_id_not_empty",
        "ck_mail_ingestions_attachment_id_not_empty",
        "ck_mail_ingestions_status",
        "ck_mail_ingestions_stage",
        "ck_mail_ingestions_disposition",
        "ck_mail_ingestions_audio_extension",
        "ck_mail_ingestions_last_error_scope",
        "ck_mail_ingestions_attachment_size_bytes_non_negative",
        "ck_mail_ingestions_attempt_count_non_negative",
        "ck_mail_ingestions_consecutive_retry_count_non_negative",
        "ck_mail_ingestions_retry_has_attempt",
        "ck_mail_ingestions_retry_schedule",
        "ck_mail_ingestions_finished_at",
        "ck_mail_ingestions_attempt_state",
        "ck_mail_ingestions_pending_state",
        "ck_mail_ingestions_audio_extension_has_attachment",
        "ck_mail_ingestions_publication_has_audio_extension",
        "ck_mail_ingestions_disposition_at_acknowledgement",
        "ck_mail_ingestions_accepted_disposition",
        "ck_mail_ingestions_rejected_disposition",
        "ck_mail_ingestions_job_for_accepted_disposition",
        "ck_mail_ingestions_completed_disposition",
        "ck_mail_ingestions_failed_disposition",
        "ck_mail_ingestions_error_fields",
        "ck_mail_ingestions_error_for_status",
    }


def test_event_type_is_constrained_to_the_public_enum() -> None:
    events = Base.metadata.tables["ticket_creation_job_events"]
    check_names = {
        constraint.name
        for constraint in events.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_ticket_creation_job_events_event_type" in check_names

    ingestion_events = Base.metadata.tables["mail_ingestion_events"]
    ingestion_check_names = {
        constraint.name
        for constraint in ingestion_events.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert ingestion_check_names >= {
        "ck_mail_ingestion_events_status",
        "ck_mail_ingestion_events_stage",
        "ck_mail_ingestion_events_disposition",
        "ck_mail_ingestion_events_event_type",
        "ck_mail_ingestion_events_error_scope",
        "ck_mail_ingestion_events_disposition_at_acknowledgement",
        "ck_mail_ingestion_events_error_fields",
    }


def test_worker_runtime_current_work_is_constrained_by_service() -> None:
    runtimes = Base.metadata.tables["worker_runtime_status"]
    check_names = {
        constraint.name
        for constraint in runtimes.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_worker_runtime_status_current_work_for_service" in check_names


def test_audit_foreign_keys_restrict_job_deletion() -> None:
    for table_name in ("audio_retentions", "ticket_creation_job_events"):
        table = Base.metadata.tables[table_name]
        foreign_key = next(iter(table.foreign_keys))

        assert foreign_key.ondelete == "RESTRICT"

    ingestion_events = Base.metadata.tables["mail_ingestion_events"]
    ingestion_foreign_key = next(iter(ingestion_events.foreign_keys))

    assert ingestion_foreign_key.ondelete == "RESTRICT"


def test_worker_singleton_assumptions_are_enforced_by_partial_indexes() -> None:
    jobs = Base.metadata.tables["ticket_creation_jobs"]
    retentions = Base.metadata.tables["audio_retentions"]
    job_index = next(
        index
        for index in jobs.indexes
        if index.name == "uq_ticket_creation_jobs_single_processing"
    )
    retention_index = next(
        index
        for index in retentions.indexes
        if index.name == "uq_audio_retentions_single_active_operation"
    )
    ingestion_index = next(
        index
        for index in Base.metadata.tables["mail_ingestions"].indexes
        if index.name == "uq_mail_ingestions_single_processing"
    )

    assert job_index.unique is True
    assert job_index.dialect_options["postgresql"]["where"] is not None
    assert retention_index.unique is True
    assert retention_index.dialect_options["postgresql"]["where"] is not None
    assert ingestion_index.unique is True
    assert ingestion_index.dialect_options["postgresql"]["where"] is not None


def test_query_indexes_match_the_documented_access_paths() -> None:
    index_names = {
        index.name for table in Base.metadata.tables.values() for index in table.indexes
    }

    assert index_names >= {
        "ix_ticket_creation_jobs_pending",
        "ix_ticket_creation_jobs_retry_scheduled",
        "ix_ticket_creation_jobs_dashboard",
        "ix_ticket_creation_jobs_status_dashboard",
        "ix_audio_retentions_deadline",
        "ix_audio_retentions_due_check",
        "ix_ticket_creation_job_events_history",
        "ix_mail_ingestions_pending",
        "ix_mail_ingestions_retry_scheduled",
        "ix_mail_ingestions_dashboard",
        "ix_mail_ingestions_rejected_retention",
        "ix_mail_ingestion_events_history",
        "ix_worker_runtime_status_heartbeat",
    }


def test_retention_due_index_includes_retries_for_both_policies() -> None:
    table = Base.metadata.tables["audio_retentions"]
    due_index = next(
        index
        for index in table.indexes
        if index.name == "ix_audio_retentions_due_check"
    )
    ddl = str(CreateIndex(due_index).compile(dialect=postgresql.dialect()))

    assert "policy = 'ticket_or_deadline' AND status = 'scheduled'" in ddl
    assert "OR status = 'retry_scheduled'" in ddl


def test_rejected_ingestion_retention_index_matches_terminal_candidates() -> None:
    table = Base.metadata.tables["mail_ingestions"]
    index = next(
        index
        for index in table.indexes
        if index.name == "ix_mail_ingestions_rejected_retention"
    )
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert "finished_at, uuid" in ddl
    assert "status = 'failed'" in ddl
    assert "stage = 'acknowledgement'" in ddl
    assert "disposition = 'rejected'" in ddl
    assert "job_uuid IS NULL" in ddl


def test_metadata_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()

    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
