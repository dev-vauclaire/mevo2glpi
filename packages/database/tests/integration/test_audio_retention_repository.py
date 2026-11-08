"""PostgreSQL contracts for retention acquisition and state transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    AudioExtension,
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    DeletionReason,
    PendingJob,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJobRepository,
    create_session_factory,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
ACTIVATED_AT = NOW - timedelta(days=10)


def test_candidate_priority_is_enforced_by_postgresql_queries(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    deletion_retry_uuid = _uuid(1)
    deadline_uuid = _uuid(2)
    glpi_check_uuid = _uuid(3)
    deleting_uuid = _uuid(4)

    _add_retention(
        session_factory,
        deletion_retry_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=4),
        next_check_at=None,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_deletion(
            deletion_retry_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW - timedelta(hours=3),
        )
        repository.schedule_retry(
            deletion_retry_uuid,
            error_category="storage_unavailable",
            error_code="filesystem_busy",
            next_check_at=NOW - timedelta(hours=1),
            now=NOW - timedelta(hours=2),
        )

    _add_retention(
        session_factory,
        deadline_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    _add_retention(
        session_factory,
        glpi_check_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=1),
        next_check_at=NOW - timedelta(hours=1),
    )
    _add_retention(
        session_factory,
        deleting_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=4),
        next_check_at=None,
    )
    with session_factory.begin() as session:
        AudioRetentionRepository(session).start_deletion(
            deleting_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW - timedelta(hours=3),
        )

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.RESUME_DELETION,
        job_uuid=deleting_uuid,
    )
    with session_factory.begin() as session:
        AudioRetentionRepository(session).mark_deleted(deleting_uuid, now=NOW)

    with session_factory.begin() as session:
        AudioRetentionRepository(session).start_glpi_check(
            glpi_check_uuid,
            now=NOW,
        )
    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK,
        job_uuid=glpi_check_uuid,
    )
    with session_factory.begin() as session:
        AudioRetentionRepository(session).recover_interrupted_check(
            glpi_check_uuid,
            now=NOW,
        )

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_DELETION_AT_DEADLINE,
        job_uuid=deadline_uuid,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_deletion(
            deadline_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW,
        )
        repository.mark_deleted(deadline_uuid, now=NOW + timedelta(seconds=1))

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.RESUME_DELETION_RETRY,
        job_uuid=deletion_retry_uuid,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.resume_deletion(deletion_retry_uuid, now=NOW)
        repository.mark_deleted(
            deletion_retry_uuid,
            now=NOW + timedelta(seconds=2),
        )

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_GLPI_CHECK,
        job_uuid=glpi_check_uuid,
    )


def test_deadline_selects_blocked_retention_without_deletion_context(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(10)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW,
        next_check_at=NOW - timedelta(hours=36),
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_glpi_check(job_uuid, now=NOW - timedelta(days=1))
        repository.mark_blocked(
            job_uuid,
            error_category="ambiguous_result",
            error_code="operation_unknown",
            now=NOW - timedelta(hours=12),
        )

    assert _find_candidate(session_factory, NOW - timedelta(microseconds=1)) is None
    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_DELETION_AT_DEADLINE,
        job_uuid=job_uuid,
    )
    assert candidate.retention.deletion_started_at is None


def test_deletion_retry_respects_backoff_after_hard_deadline(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(20)
    retry_at = NOW + timedelta(hours=1)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=2),
        next_check_at=None,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_deletion(
            job_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW - timedelta(hours=1),
        )
        repository.schedule_retry(
            job_uuid,
            error_category="storage_unavailable",
            error_code="filesystem_busy",
            next_check_at=retry_at,
            now=NOW - timedelta(minutes=30),
        )

    assert _find_candidate(session_factory, NOW) is None
    candidate = _find_candidate(session_factory, retry_at)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.RESUME_DELETION_RETRY,
        job_uuid=job_uuid,
    )


def test_blocked_deletion_requires_operator_even_after_hard_deadline(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(25)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=2),
        next_check_at=None,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_deletion(
            job_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW - timedelta(hours=1),
        )
        repository.mark_blocked(
            job_uuid,
            error_category="ambiguous_result",
            error_code="deletion_outcome_unknown",
            now=NOW - timedelta(minutes=30),
        )

    assert _find_candidate(session_factory, NOW) is None
    assert _find_candidate(session_factory, NOW + timedelta(days=365)) is None
    persisted = _get_retention(session_factory, job_uuid)
    assert persisted.status is RetentionStatus.BLOCKED
    assert persisted.deletion_reason is DeletionReason.FAILED_TTL
    assert persisted.deletion_started_at == NOW - timedelta(hours=1)


def test_scheduled_checking_scheduled_chain_is_durable(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(30)
    next_check_at = NOW + timedelta(hours=6)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW,
    )

    with session_factory.begin() as session:
        checking = AudioRetentionRepository(session).start_glpi_check(
            job_uuid,
            now=NOW,
        )
    assert checking.status is RetentionStatus.CHECKING
    assert checking.check_attempt_count == 1

    with session_factory.begin() as session:
        scheduled = AudioRetentionRepository(session).reschedule_after_glpi_check(
            job_uuid,
            glpi_status_id=2,
            next_check_at=next_check_at,
            now=NOW + timedelta(minutes=1),
        )
    assert scheduled.status is RetentionStatus.SCHEDULED
    assert scheduled.last_glpi_status_id == 2
    assert scheduled.next_check_at == next_check_at

    persisted = _get_retention(session_factory, job_uuid)
    assert persisted.status is RetentionStatus.SCHEDULED
    assert persisted.check_attempt_count == 1
    assert persisted.last_checked_at == NOW
    assert persisted.last_glpi_status_id == 2


def test_checking_retry_checking_chain_honours_due_boundary(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(40)
    retry_at = NOW + timedelta(minutes=5)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW,
    )
    with session_factory.begin() as session:
        AudioRetentionRepository(session).start_glpi_check(job_uuid, now=NOW)
    with session_factory.begin() as session:
        retry = AudioRetentionRepository(session).schedule_retry(
            job_uuid,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=retry_at,
            now=NOW + timedelta(seconds=30),
        )
    assert retry.status is RetentionStatus.RETRY_SCHEDULED

    assert (
        _find_candidate(session_factory, retry_at - timedelta(microseconds=1)) is None
    )
    candidate = _find_candidate(session_factory, retry_at)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_GLPI_CHECK,
        job_uuid=job_uuid,
    )
    with session_factory.begin() as session:
        checking = AudioRetentionRepository(session).start_glpi_check(
            job_uuid,
            now=retry_at,
        )

    assert checking.status is RetentionStatus.CHECKING
    assert checking.check_attempt_count == 2
    assert checking.consecutive_retry_count == 1
    assert checking.last_error_category is None
    assert checking.last_error_code is None


def test_future_glpi_retry_probe_suspends_ordinary_checks(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ordinary_job_uuid = _uuid(41)
    retry_probe_uuid = _uuid(42)
    retry_at = NOW + timedelta(minutes=5)

    _add_retention(
        session_factory,
        ordinary_job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(hours=1),
    )
    _add_retention(
        session_factory,
        retry_probe_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(minutes=1),
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_glpi_check(retry_probe_uuid, now=NOW)
        repository.schedule_retry(
            retry_probe_uuid,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=retry_at,
            now=NOW + timedelta(seconds=1),
        )

    with session_factory() as session:
        probe = AudioRetentionRepository(session).get_glpi_probe_gate()

    assert probe is not None
    assert probe.job_uuid == retry_probe_uuid
    assert probe.next_check_at == retry_at
    assert _find_candidate(session_factory, NOW) is None


def test_due_glpi_retry_probe_precedes_ordinary_checks(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ordinary_job_uuid = _uuid(43)
    retry_probe_uuid = _uuid(44)

    _add_retention(
        session_factory,
        ordinary_job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(hours=1),
    )
    _add_retention(
        session_factory,
        retry_probe_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(minutes=2),
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_glpi_check(
            retry_probe_uuid,
            now=NOW - timedelta(minutes=1),
        )
        repository.schedule_retry(
            retry_probe_uuid,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=NOW,
            now=NOW - timedelta(seconds=30),
        )

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_GLPI_CHECK,
        job_uuid=retry_probe_uuid,
    )


def test_future_deletion_retry_does_not_gate_ordinary_glpi_checks(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ordinary_job_uuid = _uuid(47)
    deletion_retry_uuid = _uuid(48)

    _add_retention(
        session_factory,
        ordinary_job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(minutes=1),
    )
    _add_retention(
        session_factory,
        deletion_retry_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=2),
        next_check_at=None,
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_deletion(
            deletion_retry_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW - timedelta(hours=1),
        )
        repository.schedule_retry(
            deletion_retry_uuid,
            error_category="storage_unavailable",
            error_code="filesystem_busy",
            next_check_at=NOW + timedelta(minutes=5),
            now=NOW - timedelta(minutes=30),
        )

    with session_factory() as session:
        assert AudioRetentionRepository(session).get_glpi_probe_gate() is None
    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_GLPI_CHECK,
        job_uuid=ordinary_job_uuid,
    )


def test_future_glpi_retry_probe_does_not_suspend_local_deadline(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    deadline_job_uuid = _uuid(45)
    retry_probe_uuid = _uuid(46)

    _add_retention(
        session_factory,
        deadline_job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    _add_retention(
        session_factory,
        retry_probe_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW - timedelta(minutes=1),
    )
    with session_factory.begin() as session:
        repository = AudioRetentionRepository(session)
        repository.start_glpi_check(retry_probe_uuid, now=NOW)
        repository.schedule_retry(
            retry_probe_uuid,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=NOW + timedelta(minutes=5),
            now=NOW + timedelta(seconds=1),
        )

    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_DELETION_AT_DEADLINE,
        job_uuid=deadline_job_uuid,
    )


def test_deadline_deleting_deleted_chain_exposes_legacy_purge_candidate(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(50)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW,
        next_check_at=None,
    )

    assert _find_candidate(session_factory, NOW - timedelta(microseconds=1)) is None
    candidate = _find_candidate(session_factory, NOW)
    _assert_candidate(
        candidate,
        kind=RetentionCandidateKind.START_DELETION_AT_DEADLINE,
        job_uuid=job_uuid,
    )

    with session_factory.begin() as session:
        deleting = AudioRetentionRepository(session).start_deletion(
            job_uuid,
            reason=DeletionReason.FAILED_TTL,
            now=NOW,
        )
    assert deleting.status is RetentionStatus.DELETING
    _assert_candidate(
        _find_candidate(session_factory, NOW),
        kind=RetentionCandidateKind.RESUME_DELETION,
        job_uuid=job_uuid,
    )

    deleted_at = NOW + timedelta(seconds=1)
    with session_factory.begin() as session:
        deleted = AudioRetentionRepository(session).mark_deleted(
            job_uuid,
            now=deleted_at,
        )
    assert deleted.status is RetentionStatus.DELETED
    assert deleted.audio_deleted_at == deleted_at
    _assert_candidate(
        _find_candidate(session_factory, deleted_at),
        kind=RetentionCandidateKind.PURGE_DELETED_AGGREGATE,
        job_uuid=job_uuid,
    )

    persisted = _get_retention(session_factory, job_uuid)
    assert persisted.deletion_reason is DeletionReason.FAILED_TTL
    assert persisted.deletion_started_at == NOW
    assert persisted.audio_deleted_at == deleted_at


def test_caller_rollback_restores_retention_before_transition(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(60)
    _add_retention(
        session_factory,
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=2),
        next_check_at=NOW,
    )

    with pytest.raises(_RollbackRequested):
        with session_factory.begin() as session:
            AudioRetentionRepository(session).start_glpi_check(job_uuid, now=NOW)
            raise _RollbackRequested

    persisted = _get_retention(session_factory, job_uuid)
    assert persisted.status is RetentionStatus.SCHEDULED
    assert persisted.next_check_at == NOW
    assert persisted.last_checked_at is None
    assert persisted.check_attempt_count == 0
    _assert_candidate(
        _find_candidate(session_factory, NOW),
        kind=RetentionCandidateKind.START_GLPI_CHECK,
        job_uuid=job_uuid,
    )


class _RollbackRequested(RuntimeError):
    """Force the caller-owned transaction to exercise its rollback path."""


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _add_retention(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
    *,
    policy: RetentionPolicy,
    hard_delete_at: datetime,
    next_check_at: datetime | None,
) -> None:
    with session_factory.begin() as session:
        TicketCreationJobRepository(session).add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        AudioRetentionRepository(session).activate(
            job_uuid,
            policy=policy,
            hard_delete_at=hard_delete_at,
            next_check_at=next_check_at,
            now=ACTIVATED_AT,
        )


def _find_candidate(
    session_factory: sessionmaker[Session],
    now: datetime,
) -> RetentionCandidate | None:
    with session_factory() as session:
        return AudioRetentionRepository(session).find_next_candidate(now)


def _get_retention(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
) -> AudioRetentionSnapshot:
    with session_factory() as session:
        retention = AudioRetentionRepository(session).get_by_job_uuid(job_uuid)
    assert retention is not None
    return retention


def _assert_candidate(
    candidate: RetentionCandidate | None,
    *,
    kind: RetentionCandidateKind,
    job_uuid: UUID,
) -> None:
    assert candidate is not None
    assert candidate.kind is kind
    assert candidate.retention.job_uuid == job_uuid
