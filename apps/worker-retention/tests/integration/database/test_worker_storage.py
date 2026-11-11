"""Retention service integration across PostgreSQL and the real audio volume."""

from __future__ import annotations

import errno
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_common import audio_purge
from mevo2glpi_common.backoff import ExponentialBackoffPolicy
from mevo2glpi_database import (
    AudioExtension,
    AudioRetention,
    AudioRetentionRepository,
    DeletionReason,
    ErrorScope,
    JobEventType,
    JobStage,
    JobStatus,
    PendingJob,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobRepository,
    create_session_factory,
)
from mevo2glpi_glpi import GlpiTicketSnapshot, TicketId, TicketStatus
from mevo2glpi_worker_retention.retention.lifecycle import (
    RetentionJobLifecycleService,
)
from mevo2glpi_worker_retention.retention.outcomes import (
    AudioDeletedOutcome,
    GlpiCheckRescheduledOutcome,
    RetentionOperation,
    RetryScheduledOutcome,
)
from mevo2glpi_worker_retention.retention.policies import RetentionCheckPolicy
from mevo2glpi_worker_retention.retention.service import RetentionWorker
from mevo2glpi_worker_retention.retention.stages import (
    CrashSafeAudioPurger,
    GlpiCheckStage,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
ACTIVATED_AT = NOW - timedelta(days=1)
AUDIO_BYTES = b"RIFF-retention-integration-audio"
CHECK_INTERVAL = timedelta(hours=6)


def test_failed_deadline_deletes_audio_and_database_aggregate(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(301)
    storage_key = _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    canonical = _write_canonical(audio_root, storage_key)
    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        clock=_Clock(NOW, NOW + timedelta(seconds=1)),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=job_uuid,
        reason=DeletionReason.FAILED_TTL,
    )
    assert not canonical.exists()
    assert not _quarantine_path(audio_root, storage_key).exists()
    assert ticket_reader.ticket_ids == []
    _assert_aggregate_absent(session_factory, job_uuid)


@pytest.mark.parametrize(
    ("initial_storage_state", "uuid_suffix"),
    [("canonical", 311), ("quarantine", 312), ("absent", 313)],
)
def test_deleting_database_state_recovers_every_unambiguous_storage_state(
    migrated_engine: Engine,
    tmp_path: Path,
    initial_storage_state: str,
    uuid_suffix: int,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(uuid_suffix)
    storage_key = _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    lifecycle.prepare_candidate(candidate, NOW)

    if initial_storage_state == "canonical":
        _write_canonical(audio_root, storage_key)
    elif initial_storage_state == "quarantine":
        quarantine_path = _quarantine_path(audio_root, storage_key)
        quarantine_path.parent.mkdir(mode=0o700)
        quarantine_path.write_bytes(AUDIO_BYTES)
    else:
        assert initial_storage_state == "absent"

    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        clock=_Clock(
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
        ),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, AudioDeletedOutcome)
    assert outcome.job_uuid == job_uuid
    assert not (audio_root / storage_key).exists()
    assert not _quarantine_path(audio_root, storage_key).exists()
    assert ticket_reader.ticket_ids == []
    _assert_aggregate_absent(session_factory, job_uuid)


def test_legacy_deleted_retention_is_purged_without_an_audio_file(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(314)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    lifecycle.prepare_candidate(candidate, NOW)
    with session_factory.begin() as session:
        AudioRetentionRepository(session).mark_deleted(
            job_uuid,
            now=NOW + timedelta(seconds=1),
        )

    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        clock=_Clock(NOW + timedelta(seconds=2)),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=job_uuid,
        reason=DeletionReason.FAILED_TTL,
    )
    assert ticket_reader.ticket_ids == []
    _assert_aggregate_absent(session_factory, job_uuid)


def test_open_ticket_reschedules_check_and_keeps_audio_bytes(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(321)
    hard_delete_at = NOW + timedelta(days=30)
    storage_key = _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=hard_delete_at,
        next_check_at=NOW,
    )
    canonical = _write_canonical(audio_root, storage_key)
    decision_time = NOW + timedelta(seconds=1)
    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        ticket_status=TicketStatus.ASSIGNED,
        clock=_Clock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert outcome == GlpiCheckRescheduledOutcome(
        job_uuid=job_uuid,
        ticket_status=TicketStatus.ASSIGNED,
        next_check_at=decision_time + CHECK_INTERVAL,
    )
    assert canonical.read_bytes() == AUDIO_BYTES
    assert not (audio_root / audio_purge.PURGE_DIRECTORY_NAME).exists()
    assert ticket_reader.ticket_ids == [_ticket_id(job_uuid)]
    retention, job, events = _stored_state(session_factory, job_uuid)
    assert job.status == JobStatus.COMPLETED.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.last_glpi_status_id == int(TicketStatus.ASSIGNED)
    assert retention.next_check_at == decision_time + CHECK_INTERVAL
    assert events == []


def test_deadline_crossed_during_glpi_read_purges_audio_and_database(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(325)
    hard_delete_at = NOW + timedelta(milliseconds=500)
    storage_key = _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=hard_delete_at,
        next_check_at=NOW,
    )
    canonical = _write_canonical(audio_root, storage_key)
    decision_time = NOW + timedelta(seconds=1)
    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        ticket_status=TicketStatus.ASSIGNED,
        clock=_Clock(NOW, decision_time, decision_time + timedelta(seconds=1)),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=job_uuid,
        reason=DeletionReason.DEADLINE_EXPIRED,
    )
    assert not canonical.exists()
    assert ticket_reader.ticket_ids == [_ticket_id(job_uuid)]
    _assert_aggregate_absent(session_factory, job_uuid)


def test_filesystem_failure_schedules_retry_with_deletion_context(
    migrated_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_root = _audio_root(tmp_path)
    job_uuid = _uuid(331)
    storage_key = _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    canonical = _write_canonical(audio_root, storage_key)

    def fail_link(
        _source: Path,
        _destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        assert follow_symlinks is False
        raise OSError(errno.EIO, "synthetic filesystem failure")

    monkeypatch.setattr(audio_purge.os, "link", fail_link)
    decision_time = NOW + timedelta(seconds=1)
    worker, ticket_reader = _worker(
        session_factory,
        audio_root,
        job_uuid=job_uuid,
        clock=_Clock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.job_uuid == job_uuid
    assert outcome.operation is RetentionOperation.AUDIO_PURGE
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert outcome.failure.category == "storage_unavailable"
    assert outcome.failure.technical_code == "purge_move_failed"
    assert outcome.next_attempt_at == decision_time + timedelta(seconds=5)
    assert canonical.read_bytes() == AUDIO_BYTES
    assert not _quarantine_path(audio_root, storage_key).exists()
    assert ticket_reader.ticket_ids == []
    retention, job, events = _stored_state(session_factory, job_uuid)
    assert job.status == JobStatus.FAILED.value
    assert retention.status == RetentionStatus.RETRY_SCHEDULED.value
    assert retention.deletion_reason == DeletionReason.FAILED_TTL.value
    assert retention.deletion_started_at == NOW
    assert retention.audio_deleted_at is None
    assert retention.consecutive_retry_count == 1
    assert retention.next_check_at == decision_time + timedelta(seconds=5)
    assert events == [
        JobEventType.AUDIO_DELETION_STARTED.value,
        JobEventType.RETENTION_RETRY_SCHEDULED.value,
    ]


class _Clock:
    def __init__(self, *instants: datetime) -> None:
        self._instants = deque(instants)

    def __call__(self) -> datetime:
        if not self._instants:
            raise AssertionError("test clock was called more often than expected")
        return self._instants.popleft()


class _TicketReader:
    def __init__(self, status: TicketStatus, *, external_id: UUID) -> None:
        self._status = status
        self._external_id = external_id
        self.ticket_ids: list[int] = []

    def get_ticket(self, ticket_id: TicketId) -> GlpiTicketSnapshot:
        normalized_id = int(ticket_id)
        self.ticket_ids.append(normalized_id)
        return GlpiTicketSnapshot(
            ticket_id=TicketId(normalized_id),
            external_id=str(self._external_id),
            status=self._status,
        )


def _worker(
    session_factory: sessionmaker[Session],
    audio_root: Path,
    *,
    job_uuid: UUID,
    clock: _Clock,
    ticket_status: TicketStatus = TicketStatus.ASSIGNED,
) -> tuple[RetentionWorker, _TicketReader]:
    check_policy = RetentionCheckPolicy(check_interval=CHECK_INTERVAL)
    ticket_reader = _TicketReader(ticket_status, external_id=job_uuid)
    worker = RetentionWorker(
        lifecycle=RetentionJobLifecycleService(
            session_factory=session_factory,
            check_policy=check_policy,
        ),
        glpi_check_stage=GlpiCheckStage(ticket_reader),
        audio_purger=CrashSafeAudioPurger(audio_root),
        retry_policy=ExponentialBackoffPolicy(
            initial_seconds=5,
            maximum_seconds=300,
        ),
        check_policy=check_policy,
        poll_interval_seconds=60,
        system_probe_interval_seconds=60,
        now=clock,
    )
    return worker, ticket_reader


def _lifecycle(
    session_factory: sessionmaker[Session],
) -> RetentionJobLifecycleService:
    return RetentionJobLifecycleService(
        session_factory=session_factory,
        check_policy=RetentionCheckPolicy(check_interval=CHECK_INTERVAL),
    )


def _add_terminal_retention(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
    *,
    status: JobStatus,
    hard_delete_at: datetime,
    next_check_at: datetime | None,
) -> str:
    storage_key = f"{job_uuid}.wav"
    with session_factory.begin() as session:
        job_repository = TicketCreationJobRepository(session)
        job_repository.add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=storage_key,
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=len(AUDIO_BYTES),
            )
        )
        assert job_repository.resume_or_claim_next(ACTIVATED_AT) is not None
        if status is JobStatus.COMPLETED:
            job_repository.advance_after_transcription(
                job_uuid,
                ACTIVATED_AT,
            )
            job_repository.record_ticket_confirmed(
                job_uuid,
                _ticket_id(job_uuid),
                ACTIVATED_AT,
            )
            job_repository.mark_completed(job_uuid, now=ACTIVATED_AT)
            policy = RetentionPolicy.TICKET_OR_DEADLINE
        elif status is JobStatus.FAILED:
            job_repository.mark_failed(
                job_uuid,
                stage=JobStage.TRANSCRIPTION,
                error_category="terminal",
                error_code="remote_failure",
                now=ACTIVATED_AT,
            )
            policy = RetentionPolicy.DEADLINE_ONLY
        else:
            raise AssertionError("test setup supports completed and failed jobs")

        AudioRetentionRepository(session).activate(
            job_uuid,
            policy=policy,
            hard_delete_at=hard_delete_at,
            next_check_at=next_check_at,
            now=ACTIVATED_AT,
        )
    return storage_key


def _stored_state(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
) -> tuple[AudioRetention, TicketCreationJob, list[str]]:
    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
        job = session.get(TicketCreationJob, job_uuid)
        events = list(
            session.scalars(
                select(TicketCreationJobEvent.event_type)
                .where(TicketCreationJobEvent.job_uuid == job_uuid)
                .order_by(TicketCreationJobEvent.event_id)
            ).all()
        )
        assert retention is not None
        assert job is not None
        session.expunge(retention)
        session.expunge(job)
    return retention, job, events


def _assert_aggregate_absent(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
) -> None:
    with session_factory() as session:
        assert session.get(AudioRetention, job_uuid) is None
        assert session.get(TicketCreationJob, job_uuid) is None
        assert (
            session.scalars(
                select(TicketCreationJobEvent).where(
                    TicketCreationJobEvent.job_uuid == job_uuid
                )
            ).first()
            is None
        )


def _audio_root(tmp_path: Path) -> Path:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    return audio_root


def _write_canonical(audio_root: Path, storage_key: str) -> Path:
    canonical = audio_root / storage_key
    canonical.write_bytes(AUDIO_BYTES)
    return canonical


def _quarantine_path(audio_root: Path, storage_key: str) -> Path:
    return audio_root / audio_purge.PURGE_DIRECTORY_NAME / storage_key


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _ticket_id(job_uuid: UUID) -> int:
    return job_uuid.int % 1_000_000 + 1
