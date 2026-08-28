from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from glpi_ticketing_database import (
    AdvisoryLockConnectionLostError,
    AudioExtension,
    ErrorScope,
    JobStage,
    JobStatus,
    PendingJob,
    TicketCreationJob,
    TicketCreationJobRepository,
    WorkerServiceName,
    create_session_factory,
    try_acquire_advisory_lock,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_postgresql_advisory_lock_enforces_singleton_and_releases(
    migrated_engine: Engine,
) -> None:
    automation = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.AUTOMATION,
    )
    assert automation is not None
    retention = None
    replacement = None
    try:
        assert (
            try_acquire_advisory_lock(
                migrated_engine,
                WorkerServiceName.AUTOMATION,
            )
            is None
        )
        retention = try_acquire_advisory_lock(
            migrated_engine,
            WorkerServiceName.RETENTION,
        )
        assert retention is not None
        with automation.open_session() as locked_session:
            with locked_session.begin():
                assert locked_session.scalar(text("SELECT 1")) == 1
        automation.ensure_held()
        assert (
            try_acquire_advisory_lock(
                migrated_engine,
                WorkerServiceName.AUTOMATION,
            )
            is None
        )
        automation.release()

        replacement = try_acquire_advisory_lock(
            migrated_engine,
            WorkerServiceName.AUTOMATION,
        )
        assert replacement is not None
    finally:
        if not automation.is_released:
            automation.release()
        if retention is not None:
            retention.release()
        if replacement is not None:
            replacement.release()


def test_lock_bound_transaction_cannot_commit_after_backend_loss(
    migrated_engine: Engine,
) -> None:
    lock = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.AUTOMATION,
    )
    assert lock is not None
    job_uuid = UUID("00000000-0000-0000-0000-000000000099")
    session = lock.open_session()

    try:
        with pytest.raises(SQLAlchemyError):
            with session.begin():
                TicketCreationJobRepository(session).add_pending(
                    PendingJob(
                        uuid=job_uuid,
                        storage_key=f"{job_uuid}.wav",
                        audio_extension=AudioExtension.WAV,
                        audio_size_bytes=42,
                    )
                )
                backend_pid = session.scalar(text("SELECT pg_backend_pid()"))
                with migrated_engine.begin() as killer:
                    assert (
                        killer.scalar(
                            text("SELECT pg_terminate_backend(:backend_pid)"),
                            {"backend_pid": backend_pid},
                        )
                        is True
                    )
    finally:
        session.close()

    with pytest.raises(AdvisoryLockConnectionLostError):
        lock.ensure_held()

    session_factory = create_session_factory(migrated_engine)
    with session_factory() as verification_session:
        assert verification_session.get(TicketCreationJob, job_uuid) is None

    replacement = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.AUTOMATION,
    )
    assert replacement is not None
    replacement.release()


def test_processing_recovery_precedes_due_retry_and_pending(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    processing_uuid = UUID("00000000-0000-0000-0000-000000000001")
    retry_uuid = UUID("00000000-0000-0000-0000-000000000002")
    pending_uuid = UUID("00000000-0000-0000-0000-000000000003")
    _add_pending_jobs(session_factory, processing_uuid, retry_uuid, pending_uuid)
    with migrated_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.update()
            .where(TicketCreationJob.uuid == processing_uuid)
            .values(
                status=JobStatus.PROCESSING.value,
                stage=JobStage.GLPI_CREATION.value,
                attempt_count=1,
                first_started_at=NOW - timedelta(minutes=10),
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        connection.execute(
            TicketCreationJob.__table__.update()
            .where(TicketCreationJob.uuid == retry_uuid)
            .values(
                status=JobStatus.RETRY_SCHEDULED.value,
                stage=JobStage.TRANSCRIPTION.value,
                attempt_count=1,
                consecutive_retry_count=1,
                next_attempt_at=NOW - timedelta(seconds=1),
                last_error_scope=ErrorScope.SYSTEM.value,
                last_error_category="unavailable",
                last_error_code="http_503",
                first_started_at=NOW - timedelta(minutes=10),
                updated_at=NOW - timedelta(minutes=5),
            )
        )

    with session_factory.begin() as session:
        acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.job.uuid == processing_uuid
    assert acquisition.resumed is True
    assert acquisition.job.attempt_count == 2
    assert acquisition.job.stage is JobStage.TRANSCRIPTION


def test_due_retry_precedes_pending_and_future_system_retry_suspends_fanout(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    retry_uuid = UUID("00000000-0000-0000-0000-000000000010")
    pending_uuid = UUID("00000000-0000-0000-0000-000000000011")
    _add_pending_jobs(session_factory, retry_uuid, pending_uuid)
    with migrated_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.update()
            .where(TicketCreationJob.uuid == retry_uuid)
            .values(
                status=JobStatus.RETRY_SCHEDULED.value,
                stage=JobStage.TRANSCRIPTION.value,
                attempt_count=1,
                consecutive_retry_count=1,
                next_attempt_at=NOW,
                last_error_scope=ErrorScope.SYSTEM.value,
                last_error_category="unavailable",
                last_error_code="http_503",
                first_started_at=NOW - timedelta(minutes=1),
                updated_at=NOW - timedelta(seconds=30),
            )
        )

    with session_factory.begin() as session:
        acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)
    assert acquisition is not None
    assert acquisition.job.uuid == retry_uuid

    with session_factory.begin() as session:
        TicketCreationJobRepository(session).schedule_retry(
            retry_uuid,
            stage=JobStage.TRANSCRIPTION,
            error_scope=ErrorScope.SYSTEM,
            error_category="unavailable",
            error_code="http_503",
            next_attempt_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
    with session_factory.begin() as session:
        assert (
            TicketCreationJobRepository(session).resume_or_claim_next(
                NOW + timedelta(seconds=30)
            )
            is None
        )

    with session_factory() as session:
        pending = session.get(TicketCreationJob, pending_uuid)
        assert pending is not None
        assert pending.status == JobStatus.PENDING.value


def _add_pending_jobs(session_factory, *job_uuids: UUID) -> None:
    with session_factory.begin() as session:
        repository = TicketCreationJobRepository(session)
        for job_uuid in job_uuids:
            repository.add_pending(
                PendingJob(
                    uuid=job_uuid,
                    storage_key=f"{job_uuid}.wav",
                    audio_extension=AudioExtension.WAV,
                    audio_size_bytes=128,
                )
            )
