"""PostgreSQL contracts for the dashboard observability aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from mevo2glpi_database import (
    AudioRetention,
    AudioRetentionRepository,
    JobStatus,
    TicketCreationJob,
    TicketCreationJobRepository,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
    create_session_factory,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PENDING_BEFORE = NOW - timedelta(minutes=15)
RETRY_BEFORE = NOW - timedelta(seconds=60)
DEADLINE_BEFORE = NOW - timedelta(seconds=120)


def test_job_alert_summary_uses_updated_at_and_strict_cutoffs(
    migrated_engine: Engine,
) -> None:
    uuids = tuple(_uuid(index) for index in range(1, 9))
    with migrated_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.insert(),
            [
                _job(uuids[0], updated_at=PENDING_BEFORE),
                _job(
                    uuids[1],
                    updated_at=PENDING_BEFORE - timedelta(microseconds=1),
                ),
                _job(
                    uuids[2],
                    status=JobStatus.RETRY_SCHEDULED,
                    updated_at=NOW,
                    next_attempt_at=RETRY_BEFORE,
                ),
                _job(
                    uuids[3],
                    status=JobStatus.RETRY_SCHEDULED,
                    updated_at=NOW,
                    next_attempt_at=RETRY_BEFORE - timedelta(microseconds=1),
                ),
                _job(
                    uuids[4],
                    status=JobStatus.FAILED,
                    updated_at=NOW - timedelta(hours=2),
                ),
                _job(
                    uuids[5],
                    status=JobStatus.FAILED,
                    updated_at=NOW - timedelta(hours=1),
                ),
                _job(
                    uuids[6],
                    status=JobStatus.BLOCKED,
                    updated_at=NOW - timedelta(minutes=30),
                ),
                # An old job rearmed recently must not immediately alert pending.
                _job(
                    uuids[7],
                    updated_at=NOW - timedelta(minutes=1),
                    created_at=NOW - timedelta(days=30),
                ),
            ],
        )

    session_factory = create_session_factory(migrated_engine)
    with session_factory() as session:
        summary = TicketCreationJobRepository(session).get_alert_summary(
            pending_before=PENDING_BEFORE,
            retry_before=RETRY_BEFORE,
        )

    assert summary.pending_overdue.count == 1
    assert summary.pending_overdue.condition_since == PENDING_BEFORE - timedelta(
        microseconds=1
    )
    assert summary.retry_overdue.count == 1
    assert summary.retry_overdue.condition_since == RETRY_BEFORE - timedelta(
        microseconds=1
    )
    assert summary.failed.count == 2
    assert summary.failed.condition_since == NOW - timedelta(hours=2)
    assert summary.blocked.count == 1
    assert summary.blocked.condition_since == NOW - timedelta(minutes=30)


def test_retention_alert_summary_prioritizes_deadline_and_split_operations(
    migrated_engine: Engine,
) -> None:
    uuids = tuple(_uuid(index) for index in range(20, 29))
    with migrated_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.insert(),
            [_job(job_uuid, updated_at=NOW) for job_uuid in uuids],
        )
        connection.execute(
            AudioRetention.__table__.insert(),
            [
                _retention(
                    uuids[0],
                    status="scheduled",
                    hard_delete_at=DEADLINE_BEFORE,
                ),
                _retention(
                    uuids[1],
                    status="scheduled",
                    hard_delete_at=DEADLINE_BEFORE - timedelta(microseconds=1),
                ),
                _retention(
                    uuids[2],
                    status="blocked",
                    policy="ticket_or_deadline",
                    hard_delete_at=NOW + timedelta(days=1),
                    updated_at=NOW - timedelta(minutes=8),
                ),
                _retention(
                    uuids[3],
                    status="blocked",
                    hard_delete_at=NOW + timedelta(days=1),
                    deletion_started_at=NOW - timedelta(minutes=7),
                    deletion_reason="blocked_ttl",
                    updated_at=NOW - timedelta(minutes=7),
                ),
                _retention(
                    uuids[4],
                    status="retry_scheduled",
                    policy="ticket_or_deadline",
                    hard_delete_at=NOW + timedelta(days=1),
                    next_check_at=RETRY_BEFORE - timedelta(microseconds=1),
                ),
                _retention(
                    uuids[5],
                    status="retry_scheduled",
                    hard_delete_at=NOW + timedelta(days=1),
                    next_check_at=RETRY_BEFORE - timedelta(seconds=1),
                    deletion_started_at=NOW - timedelta(minutes=6),
                    deletion_reason="failed_ttl",
                ),
                _retention(
                    uuids[6],
                    status="retry_scheduled",
                    policy="ticket_or_deadline",
                    hard_delete_at=NOW + timedelta(days=1),
                    next_check_at=RETRY_BEFORE,
                ),
                # Deadline dominates this otherwise blocked GLPI condition.
                _retention(
                    uuids[7],
                    status="blocked",
                    policy="ticket_or_deadline",
                    hard_delete_at=DEADLINE_BEFORE - timedelta(minutes=1),
                    updated_at=NOW - timedelta(hours=1),
                ),
                _retention(uuids[8], status="deleted", hard_delete_at=NOW),
            ],
        )

    session_factory = create_session_factory(migrated_engine)
    with session_factory() as session:
        summary = AudioRetentionRepository(session).get_alert_summary(
            retry_before=RETRY_BEFORE,
            deadline_before=DEADLINE_BEFORE,
        )

    assert summary.deadline_overdue.count == 2
    assert summary.deadline_overdue.condition_since == DEADLINE_BEFORE - timedelta(
        minutes=1
    )
    assert summary.glpi_check_blocked.count == 1
    assert summary.glpi_check_blocked.condition_since == NOW - timedelta(minutes=8)
    assert summary.deletion_blocked.count == 1
    assert summary.deletion_blocked.condition_since == NOW - timedelta(minutes=7)
    assert summary.glpi_retry_overdue.count == 1
    assert summary.glpi_retry_overdue.condition_since == RETRY_BEFORE - timedelta(
        microseconds=1
    )
    assert summary.deletion_retry_overdue.count == 1
    assert summary.deletion_retry_overdue.condition_since == RETRY_BEFORE - timedelta(
        seconds=1
    )


def test_runtime_list_all_is_deterministic_and_read_only(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(
            WorkerRuntimeStatus.__table__.insert(),
            [
                _runtime(WorkerServiceName.RETENTION, WorkerRuntimeState.DEGRADED),
                _runtime(WorkerServiceName.AUTOMATION, WorkerRuntimeState.RUNNING),
            ],
        )

    session_factory = create_session_factory(migrated_engine)
    with session_factory() as session:
        snapshots = WorkerRuntimeStatusRepository(session).list_all()

    assert tuple(snapshot.service_name for snapshot in snapshots) == (
        WorkerServiceName.AUTOMATION,
        WorkerServiceName.RETENTION,
    )


def _uuid(index: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{index:012d}")


def _job(
    job_uuid: UUID,
    *,
    updated_at: datetime,
    status: JobStatus = JobStatus.PENDING,
    created_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
) -> dict[str, object]:
    terminal = status in {JobStatus.FAILED, JobStatus.BLOCKED}
    has_error = terminal or status is JobStatus.RETRY_SCHEDULED
    return {
        "uuid": job_uuid,
        "status": status.value,
        "stage": None if status is JobStatus.PENDING else "transcription",
        "storage_key": f"{job_uuid}.wav",
        "audio_extension": "wav",
        "audio_size_bytes": 128,
        "next_attempt_at": next_attempt_at,
        "last_error_scope": "job" if has_error else None,
        "last_error_category": "synthetic" if has_error else None,
        "last_error_code": "test_condition" if has_error else None,
        "created_at": created_at or NOW - timedelta(hours=1),
        "updated_at": updated_at,
        "finished_at": updated_at if terminal else None,
    }


def _retention(
    job_uuid: UUID,
    *,
    status: str,
    hard_delete_at: datetime,
    policy: str = "deadline_only",
    next_check_at: datetime | None = None,
    deletion_started_at: datetime | None = None,
    deletion_reason: str | None = None,
    updated_at: datetime = NOW,
) -> dict[str, object]:
    if status == "deleted" and deletion_started_at is None:
        deletion_started_at = NOW - timedelta(minutes=1)
        deletion_reason = "failed_ttl"
    has_error = status in {"retry_scheduled", "blocked"}
    return {
        "job_uuid": job_uuid,
        "policy": policy,
        "status": status,
        "hard_delete_at": hard_delete_at,
        "next_check_at": next_check_at,
        "consecutive_retry_count": 1 if status == "retry_scheduled" else 0,
        "deletion_reason": deletion_reason,
        "deletion_started_at": deletion_started_at,
        "audio_deleted_at": NOW if status == "deleted" else None,
        "last_error_category": "synthetic" if has_error else None,
        "last_error_code": "test_condition" if has_error else None,
        "created_at": NOW - timedelta(days=1),
        "updated_at": updated_at,
    }


def _runtime(
    service_name: WorkerServiceName,
    state: WorkerRuntimeState,
) -> dict[str, object]:
    has_error = state in {WorkerRuntimeState.DEGRADED, WorkerRuntimeState.BLOCKED}
    return {
        "service_name": service_name.value,
        "instance_id": _uuid(
            100 if service_name is WorkerServiceName.AUTOMATION else 101
        ),
        "state": state.value,
        "last_heartbeat_at": NOW,
        "last_error_category": "unavailable" if has_error else None,
        "last_error_code": "synthetic" if has_error else None,
        "updated_at": NOW,
    }
