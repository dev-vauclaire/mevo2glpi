"""PostgreSQL enforcement tests for the shared lifecycle schema."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError

from glpi_ticketing_database import (
    AudioRetention,
    JobEventType,
    TicketCreationJob,
    TicketCreationJobEvent,
    WorkerRuntimeStatus,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)
JOBS = TicketCreationJob.__table__
RETENTIONS = AudioRetention.__table__
EVENTS = TicketCreationJobEvent.__table__
WORKER_STATUS = WorkerRuntimeStatus.__table__


def _job_values(
    *,
    status: str = "pending",
    job_uuid: UUID | None = None,
    **overrides: object,
) -> dict[str, object]:
    job_uuid = job_uuid or uuid4()
    extension = str(overrides.get("audio_extension", "wav"))
    is_terminal = status in {"completed", "failed", "blocked"}
    has_error = status in {"retry_scheduled", "failed", "blocked"}
    values: dict[str, object] = {
        "uuid": job_uuid,
        "status": status,
        "stage": None if status == "pending" else "transcription",
        "storage_key": f"{job_uuid}.{extension}",
        "audio_extension": extension,
        "audio_size_bytes": 128,
        "next_attempt_at": NOW if status == "retry_scheduled" else None,
        "finished_at": NOW if is_terminal else None,
        "last_error_scope": "job" if has_error else None,
        "last_error_category": "provider" if has_error else None,
        "last_error_code": "synthetic_error" if has_error else None,
    }
    if status == "completed":
        values["stage"] = "finalization"
        values["glpi_ticket_id"] = job_uuid.int % 9_000_000_000_000_000_000 + 1
    values.update(overrides)
    return values


def _retention_values(
    job_uuid: UUID,
    *,
    status: str = "inactive",
    **overrides: object,
) -> dict[str, object]:
    checks_glpi = status in {"scheduled", "checking", "retry_scheduled"}
    has_error = status in {"retry_scheduled", "blocked"}
    is_deleting = status in {"deleting", "deleted"}
    values: dict[str, object] = {
        "job_uuid": job_uuid,
        "policy": "ticket_or_deadline" if checks_glpi else "deadline_only",
        "status": status,
        "hard_delete_at": None if status == "inactive" else LATER,
        "next_check_at": NOW if status in {"scheduled", "retry_scheduled"} else None,
        "deletion_reason": "deadline_expired" if is_deleting else None,
        "deletion_started_at": NOW if is_deleting else None,
        "audio_deleted_at": NOW if status == "deleted" else None,
        "last_error_category": "glpi" if has_error else None,
        "last_error_code": "synthetic_error" if has_error else None,
    }
    values.update(overrides)
    return values


def _event_values(job_uuid: UUID, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "job_uuid": job_uuid,
        "status": "pending",
        "stage": None,
        "event_type": "job_created",
        "error_scope": None,
        "error_category": None,
        "technical_code": None,
        "actor": "web",
    }
    values.update(overrides)
    return values


def _worker_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "service_name": "worker_automation",
        "instance_id": uuid4(),
        "state": "running",
        "last_error_category": None,
        "last_error_code": None,
    }
    values.update(overrides)
    return values


def _insert_rows(
    engine: Engine,
    table: Table,
    *rows: Mapping[str, object],
) -> None:
    with engine.begin() as connection:
        connection.execute(table.insert(), list(rows))


def _assert_insert_rejected(
    engine: Engine,
    table: Table,
    values: Mapping[str, object],
    expected_constraints: str | set[str],
) -> None:
    expected_names = (
        {expected_constraints}
        if isinstance(expected_constraints, str)
        else expected_constraints
    )
    with pytest.raises(IntegrityError) as captured_error:
        with engine.begin() as connection:
            connection.execute(table.insert(), values)

    diagnostic = getattr(captured_error.value.orig, "diag", None)
    assert getattr(diagnostic, "constraint_name", None) in expected_names


def _assert_job_delete_rejected(
    engine: Engine,
    job_uuid: UUID,
    expected_constraint: str,
) -> None:
    with pytest.raises(IntegrityError) as captured_error:
        with engine.begin() as connection:
            connection.execute(JOBS.delete().where(JOBS.c.uuid == job_uuid))

    diagnostic = getattr(captured_error.value.orig, "diag", None)
    assert getattr(diagnostic, "constraint_name", None) == expected_constraint


def test_postgresql_session_timezone_is_utc(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.scalar(text("SHOW TIME ZONE")) == "UTC"


def test_persisted_state_checks_reject_unknown_values(
    migrated_engine: Engine,
) -> None:
    parent_uuid = uuid4()
    _insert_rows(migrated_engine, JOBS, _job_values(job_uuid=parent_uuid))

    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(status="unknown"),
        "ck_ticket_creation_jobs_status",
    )
    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(parent_uuid, status="unknown"),
        {
            "ck_audio_retentions_status",
            "ck_audio_retentions_deletion_for_status",
        },
    )
    _assert_insert_rejected(
        migrated_engine,
        EVENTS,
        _event_values(parent_uuid, status="unknown"),
        "ck_ticket_creation_job_events_status",
    )
    _assert_insert_rejected(
        migrated_engine,
        EVENTS,
        _event_values(parent_uuid, event_type="unknown"),
        "ck_ticket_creation_job_events_event_type",
    )
    _insert_rows(
        migrated_engine,
        EVENTS,
        *(
            _event_values(parent_uuid, event_type=event_type.value)
            for event_type in JobEventType
        ),
    )
    _assert_insert_rejected(
        migrated_engine,
        WORKER_STATUS,
        _worker_values(state="unknown"),
        "ck_worker_runtime_status_state",
    )


def test_job_stage_constraints_match_lifecycle_state(
    migrated_engine: Engine,
) -> None:
    invalid_jobs = (
        (
            _job_values(status="processing", stage="unknown"),
            "ck_ticket_creation_jobs_stage",
        ),
        (
            _job_values(status="pending", stage="transcription"),
            "ck_ticket_creation_jobs_stage_for_status",
        ),
        (
            _job_values(status="processing", stage=None),
            "ck_ticket_creation_jobs_stage_for_status",
        ),
    )

    for values, constraint_name in invalid_jobs:
        _assert_insert_rejected(migrated_engine, JOBS, values, constraint_name)


def test_job_storage_constraints_reject_inconsistent_audio_metadata(
    migrated_engine: Engine,
) -> None:
    invalid_jobs = (
        (
            _job_values(storage_key="wrong-key.wav"),
            "ck_ticket_creation_jobs_storage_key_matches_uuid_and_extension",
        ),
        (
            _job_values(audio_extension="flac"),
            "ck_ticket_creation_jobs_audio_extension",
        ),
        (
            _job_values(audio_size_bytes=0),
            "ck_ticket_creation_jobs_audio_size_bytes_positive",
        ),
    )

    for values, constraint_name in invalid_jobs:
        _assert_insert_rejected(migrated_engine, JOBS, values, constraint_name)


def test_job_retry_and_terminal_timestamps_are_state_dependent(
    migrated_engine: Engine,
) -> None:
    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(status="retry_scheduled", next_attempt_at=None),
        "ck_ticket_creation_jobs_retry_schedule",
    )
    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(status="processing", next_attempt_at=LATER),
        "ck_ticket_creation_jobs_retry_schedule",
    )

    for terminal_status in ("completed", "failed", "blocked"):
        _assert_insert_rejected(
            migrated_engine,
            JOBS,
            _job_values(status=terminal_status, finished_at=None),
            "ck_ticket_creation_jobs_finished_at",
        )
    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(status="processing", finished_at=NOW),
        "ck_ticket_creation_jobs_finished_at",
    )


def test_completed_job_and_error_constraints_are_enforced(
    migrated_engine: Engine,
) -> None:
    invalid_jobs = (
        (
            _job_values(status="completed", glpi_ticket_id=None),
            "ck_ticket_creation_jobs_completed",
        ),
        (
            _job_values(status="completed", stage="transcription"),
            "ck_ticket_creation_jobs_completed",
        ),
        (
            _job_values(status="retry_scheduled", last_error_code=None),
            "ck_ticket_creation_jobs_error_fields",
        ),
        (
            _job_values(
                status="retry_scheduled",
                last_error_scope=None,
                last_error_category=None,
                last_error_code=None,
            ),
            "ck_ticket_creation_jobs_error_for_status",
        ),
        (
            _job_values(
                status="processing",
                last_error_scope="job",
                last_error_category="provider",
                last_error_code="unexpected",
            ),
            "ck_ticket_creation_jobs_error_for_status",
        ),
        (
            _job_values(status="failed", last_error_scope="system"),
            "ck_ticket_creation_jobs_failed_error_scope",
        ),
        (
            _job_values(status="retry_scheduled", last_error_scope="unknown"),
            "ck_ticket_creation_jobs_last_error_scope",
        ),
    )

    for values, constraint_name in invalid_jobs:
        _assert_insert_rejected(migrated_engine, JOBS, values, constraint_name)


def test_attempt_counters_cannot_be_negative(migrated_engine: Engine) -> None:
    for column_name, constraint_name in (
        ("attempt_count", "ck_ticket_creation_jobs_attempt_count_non_negative"),
        (
            "consecutive_retry_count",
            "ck_ticket_creation_jobs_consecutive_retry_count_non_negative",
        ),
    ):
        _assert_insert_rejected(
            migrated_engine,
            JOBS,
            _job_values(**{column_name: -1}),
            constraint_name,
        )

    parent_uuid = uuid4()
    _insert_rows(migrated_engine, JOBS, _job_values(job_uuid=parent_uuid))
    for column_name, constraint_name in (
        (
            "check_attempt_count",
            "ck_audio_retentions_check_attempt_count_non_negative",
        ),
        (
            "consecutive_retry_count",
            "ck_audio_retentions_consecutive_retry_count_non_negative",
        ),
    ):
        _assert_insert_rejected(
            migrated_engine,
            RETENTIONS,
            _retention_values(parent_uuid, **{column_name: -1}),
            constraint_name,
        )


def test_unique_job_identifiers_and_single_processing_index(
    migrated_engine: Engine,
) -> None:
    unique_constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspect(migrated_engine).get_unique_constraints(JOBS.name)
    }
    expected_unique_constraints = {
        "uq_ticket_creation_jobs_storage_key": ("storage_key",),
        "uq_ticket_creation_jobs_glpi_ticket_id": ("glpi_ticket_id",),
    }
    assert expected_unique_constraints.items() <= unique_constraints.items()

    shared_glpi_ticket_id = 12345
    first_completed = _job_values(
        status="completed",
        glpi_ticket_id=shared_glpi_ticket_id,
    )
    _insert_rows(migrated_engine, JOBS, first_completed)
    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(
            status="completed",
            glpi_ticket_id=shared_glpi_ticket_id,
        ),
        "uq_ticket_creation_jobs_glpi_ticket_id",
    )

    _insert_rows(
        migrated_engine,
        JOBS,
        _job_values(),
        _job_values(),
        _job_values(status="processing"),
    )
    _assert_insert_rejected(
        migrated_engine,
        JOBS,
        _job_values(status="processing"),
        "uq_ticket_creation_jobs_single_processing",
    )


def test_retention_error_and_deletion_resume_constraints(
    migrated_engine: Engine,
) -> None:
    parent_uuids = [uuid4() for _index in range(6)]
    _insert_rows(
        migrated_engine,
        JOBS,
        *(_job_values(job_uuid=job_uuid) for job_uuid in parent_uuids),
    )

    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            parent_uuids[0],
            status="retry_scheduled",
            last_error_code=None,
        ),
        "ck_audio_retentions_error_fields",
    )
    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            parent_uuids[0],
            status="retry_scheduled",
            last_error_category=None,
            last_error_code=None,
        ),
        "ck_audio_retentions_error_for_status",
    )
    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            parent_uuids[0],
            status="scheduled",
            last_error_category="glpi",
            last_error_code="unexpected",
        ),
        "ck_audio_retentions_error_for_status",
    )

    resumable_rows = (
        _retention_values(
            parent_uuids[1],
            status="retry_scheduled",
            deletion_reason="deadline_expired",
            deletion_started_at=NOW,
        ),
        _retention_values(
            parent_uuids[2],
            status="blocked",
            deletion_reason="deadline_expired",
            deletion_started_at=NOW,
        ),
    )
    _insert_rows(migrated_engine, RETENTIONS, *resumable_rows)

    with migrated_engine.connect() as connection:
        resumed_statuses = set(
            connection.scalars(
                select(RETENTIONS.c.status).where(
                    RETENTIONS.c.job_uuid.in_(parent_uuids[1:3])
                )
            )
        )
    assert resumed_statuses == {"retry_scheduled", "blocked"}

    for job_uuid, status in zip(
        parent_uuids[3:6],
        ("inactive", "scheduled", "checking"),
        strict=True,
    ):
        _assert_insert_rejected(
            migrated_engine,
            RETENTIONS,
            _retention_values(
                job_uuid,
                status=status,
                deletion_reason="deadline_expired",
                deletion_started_at=NOW,
            ),
            "ck_audio_retentions_deletion_for_status",
        )


def test_deadline_only_retention_retry_is_due_only_with_a_schedule(
    migrated_engine: Engine,
) -> None:
    retry_job_uuid = uuid4()
    missing_schedule_job_uuid = uuid4()
    invalid_check_job_uuid = uuid4()
    _insert_rows(
        migrated_engine,
        JOBS,
        _job_values(job_uuid=retry_job_uuid),
        _job_values(job_uuid=missing_schedule_job_uuid),
        _job_values(job_uuid=invalid_check_job_uuid),
    )

    _insert_rows(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            retry_job_uuid,
            status="retry_scheduled",
            policy="deadline_only",
            next_check_at=NOW,
        ),
    )
    with migrated_engine.connect() as connection:
        due_job_uuids = set(
            connection.scalars(
                select(RETENTIONS.c.job_uuid).where(
                    RETENTIONS.c.status == "retry_scheduled",
                    RETENTIONS.c.next_check_at <= NOW,
                )
            )
        )
    assert due_job_uuids == {retry_job_uuid}

    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            missing_schedule_job_uuid,
            status="retry_scheduled",
            policy="deadline_only",
            next_check_at=None,
        ),
        "ck_audio_retentions_next_check_for_policy",
    )
    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(
            invalid_check_job_uuid,
            status="checking",
            policy="deadline_only",
        ),
        "ck_audio_retentions_glpi_check_policy",
    )


def test_single_retention_active_operation_is_a_partial_uniqueness_rule(
    migrated_engine: Engine,
) -> None:
    parent_uuids = [uuid4() for _index in range(4)]
    _insert_rows(
        migrated_engine,
        JOBS,
        *(_job_values(job_uuid=job_uuid) for job_uuid in parent_uuids),
    )
    _insert_rows(
        migrated_engine,
        RETENTIONS,
        _retention_values(parent_uuids[0], status="scheduled"),
        _retention_values(parent_uuids[1], status="scheduled"),
        _retention_values(parent_uuids[2], status="checking"),
    )

    _assert_insert_rejected(
        migrated_engine,
        RETENTIONS,
        _retention_values(parent_uuids[3], status="deleting"),
        "uq_audio_retentions_single_active_operation",
    )


def test_sanitized_event_and_worker_errors_are_atomic(
    migrated_engine: Engine,
) -> None:
    parent_uuid = uuid4()
    _insert_rows(migrated_engine, JOBS, _job_values(job_uuid=parent_uuid))

    _assert_insert_rejected(
        migrated_engine,
        EVENTS,
        _event_values(
            parent_uuid,
            error_scope="job",
            error_category="provider",
            technical_code=None,
        ),
        "ck_ticket_creation_job_events_error_fields",
    )
    _assert_insert_rejected(
        migrated_engine,
        WORKER_STATUS,
        _worker_values(
            last_error_category="database",
            last_error_code=None,
        ),
        "ck_worker_runtime_status_error_fields",
    )


def test_job_audit_foreign_keys_restrict_parent_deletion(
    migrated_engine: Engine,
) -> None:
    retention_job_uuid = uuid4()
    event_job_uuid = uuid4()
    _insert_rows(
        migrated_engine,
        JOBS,
        _job_values(job_uuid=retention_job_uuid),
        _job_values(job_uuid=event_job_uuid),
    )
    _insert_rows(
        migrated_engine,
        RETENTIONS,
        _retention_values(retention_job_uuid),
    )
    _insert_rows(
        migrated_engine,
        EVENTS,
        _event_values(event_job_uuid),
    )

    foreign_keys = {
        foreign_key["name"]: foreign_key["options"].get("ondelete")
        for table_name in (RETENTIONS.name, EVENTS.name)
        for foreign_key in inspect(migrated_engine).get_foreign_keys(table_name)
    }
    expected_restrict_foreign_keys = {
        "fk_audio_retentions_job_uuid_ticket_creation_jobs": "RESTRICT",
        "fk_ticket_creation_job_events_job_uuid_ticket_creation_jobs": "RESTRICT",
    }
    assert expected_restrict_foreign_keys.items() <= foreign_keys.items()

    _assert_job_delete_rejected(
        migrated_engine,
        retention_job_uuid,
        "fk_audio_retentions_job_uuid_ticket_creation_jobs",
    )
    _assert_job_delete_rejected(
        migrated_engine,
        event_job_uuid,
        "fk_ticket_creation_job_events_job_uuid_ticket_creation_jobs",
    )
