from enum import StrEnum

import pytest
from sqlalchemy import CheckConstraint, DateTime, String, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import glpi_ticketing_database as database_api
from glpi_ticketing_database.models import (
    AudioExtension,
    AudioRetention,
    Base,
    DeletionReason,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
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
            {"web", "worker_automation", "worker_retention"},
        ),
        (
            JobEventType,
            {
                "job_created",
                "processing_started",
                "processing_resumed",
                "transcription_submitted",
                "transcription_succeeded",
                "retry_scheduled",
                "ticket_confirmed",
                "job_completed",
                "job_failed",
                "job_blocked",
            },
        ),
        (WorkerServiceName, {"worker_automation", "worker_retention"}),
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
        "worker_runtime_status",
    }


@pytest.mark.parametrize(
    ("model", "table_name"),
    [
        (TicketCreationJob, "ticket_creation_jobs"),
        (AudioRetention, "audio_retentions"),
        (TicketCreationJobEvent, "ticket_creation_job_events"),
        (WorkerRuntimeStatus, "worker_runtime_status"),
    ],
)
def test_public_model_imports_use_shared_metadata(
    model: type[Base],
    table_name: str,
) -> None:
    assert model.__table__ is Base.metadata.tables[table_name]
    assert getattr(database_api, model.__name__) is model


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
        ("worker_runtime_status", "instance_id"),
        ("worker_runtime_status", "current_job_uuid"),
    ],
)
def test_uuid_columns_use_native_uuid_mapping(
    table_name: str,
    column_name: str,
) -> None:
    column_type = Base.metadata.tables[table_name].c[column_name].type

    assert isinstance(column_type, Uuid)
    assert column_type.native_uuid is True


def test_database_schema_cannot_store_audio_or_transcription_content() -> None:
    column_names = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
    }

    assert "audio" not in column_names
    assert "audio_bytes" not in column_names
    assert "transcription" not in column_names


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
        "ck_audio_retentions_deletion_fields",
        "ck_audio_retentions_deletion_for_status",
        "ck_audio_retentions_audio_deleted_status",
        "ck_audio_retentions_error_fields",
        "ck_audio_retentions_error_for_status",
    }


def test_event_type_is_constrained_to_the_public_enum() -> None:
    events = Base.metadata.tables["ticket_creation_job_events"]
    check_names = {
        constraint.name
        for constraint in events.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_ticket_creation_job_events_event_type" in check_names


def test_audit_foreign_keys_restrict_job_deletion() -> None:
    for table_name in ("audio_retentions", "ticket_creation_job_events"):
        table = Base.metadata.tables[table_name]
        foreign_key = next(iter(table.foreign_keys))

        assert foreign_key.ondelete == "RESTRICT"


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

    assert job_index.unique is True
    assert job_index.dialect_options["postgresql"]["where"] is not None
    assert retention_index.unique is True
    assert retention_index.dialect_options["postgresql"]["where"] is not None


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


def test_metadata_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()

    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
