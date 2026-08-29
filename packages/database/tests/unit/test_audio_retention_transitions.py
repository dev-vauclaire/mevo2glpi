from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    AudioRetention,
    AudioRetentionRepository,
    DeletionReason,
    InvalidRetentionTransitionError,
    RetentionNotFoundError,
    RetentionPolicy,
    RetentionStatus,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
JOB_UUID = UUID("6788a52c-6c45-4734-9be0-f049fb57a568")


@pytest.mark.parametrize(
    ("hard_delete_at", "expected_next_check_at"),
    [
        (NOW + timedelta(days=1), NOW),
        (NOW - timedelta(minutes=1), NOW - timedelta(minutes=1)),
    ],
)
def test_recover_interrupted_check_is_immediately_due_without_crossing_deadline(
    hard_delete_at: datetime,
    expected_next_check_at: datetime,
) -> None:
    session, retention = _repository_with(
        status=RetentionStatus.CHECKING,
        hard_delete_at=hard_delete_at,
    )

    snapshot = AudioRetentionRepository(session).recover_interrupted_check(
        JOB_UUID,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.SCHEDULED
    assert snapshot.next_check_at == expected_next_check_at
    assert snapshot.check_attempt_count == 3
    assert snapshot.last_checked_at == NOW - timedelta(minutes=2)
    assert retention.updated_at == NOW
    session.flush.assert_called_once_with()


def test_start_glpi_check_claims_a_due_schedule_and_clears_retry_diagnostics() -> None:
    session, retention = _repository_with(status=RetentionStatus.RETRY_SCHEDULED)
    retention.next_check_at = NOW
    retention.consecutive_retry_count = 2
    retention.last_error_category = "glpi_unavailable"
    retention.last_error_code = "http_503"

    snapshot = AudioRetentionRepository(session).start_glpi_check(
        JOB_UUID,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.CHECKING
    assert snapshot.next_check_at is None
    assert snapshot.last_checked_at == NOW
    assert snapshot.check_attempt_count == 4
    assert snapshot.consecutive_retry_count == 2
    assert snapshot.last_error_category is None
    assert snapshot.last_error_code is None


@pytest.mark.parametrize(
    ("policy", "next_check_at"),
    [
        (RetentionPolicy.DEADLINE_ONLY, NOW),
        (RetentionPolicy.TICKET_OR_DEADLINE, NOW + timedelta(seconds=1)),
        (RetentionPolicy.TICKET_OR_DEADLINE, None),
    ],
)
def test_start_glpi_check_revalidates_policy_and_due_time(
    policy: RetentionPolicy,
    next_check_at: datetime | None,
) -> None:
    session, retention = _repository_with(
        status=RetentionStatus.SCHEDULED,
        policy=policy,
    )
    retention.next_check_at = next_check_at

    with pytest.raises(InvalidRetentionTransitionError):
        AudioRetentionRepository(session).start_glpi_check(JOB_UUID, now=NOW)

    session.flush.assert_not_called()


def test_start_glpi_check_rejects_a_due_deletion_deadline() -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.SCHEDULED,
        hard_delete_at=NOW,
    )

    with pytest.raises(InvalidRetentionTransitionError, match="deletion deadline"):
        AudioRetentionRepository(session).start_glpi_check(JOB_UUID, now=NOW)

    session.flush.assert_not_called()


def test_reschedule_after_glpi_check_records_numeric_status_and_resets_backoff() -> (
    None
):
    session, retention = _repository_with(status=RetentionStatus.CHECKING)
    retention.consecutive_retry_count = 3

    snapshot = AudioRetentionRepository(session).reschedule_after_glpi_check(
        JOB_UUID,
        glpi_status_id=2,
        next_check_at=NOW + timedelta(hours=6),
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.SCHEDULED
    assert snapshot.last_glpi_status_id == 2
    assert snapshot.next_check_at == NOW + timedelta(hours=6)
    assert snapshot.consecutive_retry_count == 0


def test_reschedule_after_glpi_check_rejects_a_date_after_deadline() -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.CHECKING,
        hard_delete_at=NOW + timedelta(hours=1),
    )

    with pytest.raises(ValueError, match="hard_delete_at"):
        AudioRetentionRepository(session).reschedule_after_glpi_check(
            JOB_UUID,
            glpi_status_id=2,
            next_check_at=NOW + timedelta(hours=2),
            now=NOW,
        )

    session.flush.assert_not_called()


def test_schedule_retry_from_checking_persists_sanitized_backoff() -> None:
    session, _retention = _repository_with(status=RetentionStatus.CHECKING)

    snapshot = AudioRetentionRepository(session).schedule_retry(
        JOB_UUID,
        error_category="glpi_unavailable",
        error_code="http_503",
        next_check_at=NOW + timedelta(minutes=1),
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.RETRY_SCHEDULED
    assert snapshot.next_check_at == NOW + timedelta(minutes=1)
    assert snapshot.consecutive_retry_count == 1
    assert snapshot.deletion_reason is None
    assert snapshot.deletion_started_at is None
    assert snapshot.last_error_category == "glpi_unavailable"
    assert snapshot.last_error_code == "http_503"


def test_schedule_retry_from_checking_rejects_backoff_after_deadline() -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.CHECKING,
        hard_delete_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ValueError, match="hard_delete_at"):
        AudioRetentionRepository(session).schedule_retry(
            JOB_UUID,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=NOW + timedelta(minutes=1),
            now=NOW,
        )

    session.flush.assert_not_called()


def test_schedule_retry_from_deleting_preserves_context_past_deadline() -> None:
    started_at = NOW - timedelta(hours=1)
    session, retention = _repository_with(
        status=RetentionStatus.DELETING,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW - timedelta(hours=2),
        deletion_reason=DeletionReason.FAILED_TTL,
        deletion_started_at=started_at,
    )

    snapshot = AudioRetentionRepository(session).schedule_retry(
        JOB_UUID,
        error_category="storage_unavailable",
        error_code="filesystem_busy",
        next_check_at=NOW + timedelta(hours=1),
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.RETRY_SCHEDULED
    assert snapshot.deletion_reason is DeletionReason.FAILED_TTL
    assert snapshot.deletion_started_at == started_at
    assert retention.next_check_at == NOW + timedelta(hours=1)


@pytest.mark.parametrize("operation", ["retry", "block"])
def test_deletion_error_transition_requires_persisted_deletion_context(
    operation: str,
) -> None:
    session, _retention = _repository_with(status=RetentionStatus.DELETING)
    repository = AudioRetentionRepository(session)

    with pytest.raises(InvalidRetentionTransitionError, match="no deletion context"):
        if operation == "retry":
            repository.schedule_retry(
                JOB_UUID,
                error_category="storage_unavailable",
                error_code="filesystem_busy",
                next_check_at=NOW + timedelta(minutes=1),
                now=NOW,
            )
        else:
            repository.mark_blocked(
                JOB_UUID,
                error_category="ambiguous_result",
                error_code="operation_unknown",
                now=NOW,
            )

    session.flush.assert_not_called()


@pytest.mark.parametrize("status", [RetentionStatus.CHECKING, RetentionStatus.DELETING])
def test_mark_blocked_preserves_existing_deletion_context(
    status: RetentionStatus,
) -> None:
    deletion_reason = (
        DeletionReason.DEADLINE_EXPIRED if status is RetentionStatus.DELETING else None
    )
    deletion_started_at = NOW - timedelta(minutes=5) if deletion_reason else None
    session, _retention = _repository_with(
        status=status,
        deletion_reason=deletion_reason,
        deletion_started_at=deletion_started_at,
    )

    snapshot = AudioRetentionRepository(session).mark_blocked(
        JOB_UUID,
        error_category="ambiguous_result",
        error_code="operation_unknown",
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.BLOCKED
    assert snapshot.next_check_at is None
    assert snapshot.deletion_reason is deletion_reason
    assert snapshot.deletion_started_at == deletion_started_at


def test_start_deletion_for_closed_ticket_records_status_and_context() -> None:
    session, _retention = _repository_with(status=RetentionStatus.CHECKING)

    snapshot = AudioRetentionRepository(session).start_deletion(
        JOB_UUID,
        reason=DeletionReason.TICKET_CLOSED,
        last_glpi_status_id=6,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.DELETING
    assert snapshot.last_glpi_status_id == 6
    assert snapshot.deletion_reason is DeletionReason.TICKET_CLOSED
    assert snapshot.deletion_started_at == NOW
    assert snapshot.consecutive_retry_count == 0


def test_deadline_deletion_records_a_status_observed_during_the_glpi_check() -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.CHECKING,
        hard_delete_at=NOW,
    )

    snapshot = AudioRetentionRepository(session).start_deletion(
        JOB_UUID,
        reason=DeletionReason.DEADLINE_EXPIRED,
        last_glpi_status_id=4,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.DELETING
    assert snapshot.last_glpi_status_id == 4
    assert snapshot.deletion_reason is DeletionReason.DEADLINE_EXPIRED


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (RetentionPolicy.TICKET_OR_DEADLINE, DeletionReason.DEADLINE_EXPIRED),
        (RetentionPolicy.DEADLINE_ONLY, DeletionReason.FAILED_TTL),
        (RetentionPolicy.DEADLINE_ONLY, DeletionReason.BLOCKED_TTL),
    ],
)
def test_start_deletion_at_deadline_enforces_reason_policy_mapping(
    policy: RetentionPolicy,
    reason: DeletionReason,
) -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.SCHEDULED,
        policy=policy,
        hard_delete_at=NOW,
    )

    snapshot = AudioRetentionRepository(session).start_deletion(
        JOB_UUID,
        reason=reason,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.DELETING
    assert snapshot.deletion_reason is reason
    assert snapshot.deletion_started_at == NOW


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (RetentionPolicy.DEADLINE_ONLY, DeletionReason.DEADLINE_EXPIRED),
        (RetentionPolicy.TICKET_OR_DEADLINE, DeletionReason.FAILED_TTL),
        (RetentionPolicy.TICKET_OR_DEADLINE, DeletionReason.BLOCKED_TTL),
    ],
)
def test_start_deletion_rejects_reason_incompatible_with_policy(
    policy: RetentionPolicy,
    reason: DeletionReason,
) -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.SCHEDULED,
        policy=policy,
        hard_delete_at=NOW,
    )

    with pytest.raises(InvalidRetentionTransitionError, match="incompatible"):
        AudioRetentionRepository(session).start_deletion(
            JOB_UUID,
            reason=reason,
            now=NOW,
        )

    session.flush.assert_not_called()


def test_start_deletion_rejects_a_future_deadline() -> None:
    session, _retention = _repository_with(
        status=RetentionStatus.SCHEDULED,
        hard_delete_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(InvalidRetentionTransitionError, match="deadline"):
        AudioRetentionRepository(session).start_deletion(
            JOB_UUID,
            reason=DeletionReason.DEADLINE_EXPIRED,
            now=NOW,
        )

    session.flush.assert_not_called()


def test_resume_deletion_is_read_only_for_an_already_active_deletion() -> None:
    started_at = NOW - timedelta(minutes=5)
    session, _retention = _repository_with(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.DEADLINE_EXPIRED,
        deletion_started_at=started_at,
    )

    snapshot = AudioRetentionRepository(session).resume_deletion(JOB_UUID, now=NOW)

    assert snapshot.status is RetentionStatus.DELETING
    assert snapshot.deletion_started_at == started_at
    session.flush.assert_not_called()


def test_resume_deletion_claims_only_a_due_purge_retry() -> None:
    started_at = NOW - timedelta(minutes=5)
    session, _retention = _repository_with(
        status=RetentionStatus.RETRY_SCHEDULED,
        deletion_reason=DeletionReason.DEADLINE_EXPIRED,
        deletion_started_at=started_at,
    )

    snapshot = AudioRetentionRepository(session).resume_deletion(JOB_UUID, now=NOW)

    assert snapshot.status is RetentionStatus.DELETING
    assert snapshot.next_check_at is None
    assert snapshot.deletion_started_at == started_at
    assert snapshot.last_error_category is None
    session.flush.assert_called_once_with()


def test_resume_deletion_rejects_a_future_retry() -> None:
    session, retention = _repository_with(
        status=RetentionStatus.RETRY_SCHEDULED,
        deletion_reason=DeletionReason.DEADLINE_EXPIRED,
        deletion_started_at=NOW - timedelta(minutes=5),
    )
    retention.next_check_at = NOW + timedelta(seconds=1)

    with pytest.raises(InvalidRetentionTransitionError, match="not due"):
        AudioRetentionRepository(session).resume_deletion(JOB_UUID, now=NOW)

    session.flush.assert_not_called()


def test_mark_deleted_preserves_context_and_records_completion() -> None:
    started_at = NOW - timedelta(minutes=5)
    session, _retention = _repository_with(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.DEADLINE_EXPIRED,
        deletion_started_at=started_at,
    )

    snapshot = AudioRetentionRepository(session).mark_deleted(JOB_UUID, now=NOW)

    assert snapshot.status is RetentionStatus.DELETED
    assert snapshot.audio_deleted_at == NOW
    assert snapshot.deletion_started_at == started_at
    assert snapshot.consecutive_retry_count == 0


def test_mark_deleted_rejects_completion_before_deletion_started() -> None:
    session, retention = _repository_with(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.DEADLINE_EXPIRED,
        deletion_started_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="deletion_started_at"):
        AudioRetentionRepository(session).mark_deleted(JOB_UUID, now=NOW)

    assert retention.status == RetentionStatus.DELETING.value
    assert retention.audio_deleted_at is None
    session.flush.assert_not_called()


def test_transition_rejects_unknown_retention_without_owning_transaction() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None

    with pytest.raises(RetentionNotFoundError):
        AudioRetentionRepository(session).recover_interrupted_check(
            uuid4(),
            now=NOW,
        )

    session.flush.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


@pytest.mark.parametrize("unsafe_code", ["HTTP 503", "secret=value", "Uppercase"])
def test_retry_rejects_unsanitized_diagnostic_codes_before_locking(
    unsafe_code: str,
) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="safe identifier"):
        AudioRetentionRepository(session).schedule_retry(
            JOB_UUID,
            error_category="glpi_unavailable",
            error_code=unsafe_code,
            next_check_at=NOW + timedelta(minutes=1),
            now=NOW,
        )

    session.get.assert_not_called()
    session.flush.assert_not_called()


def _repository_with(
    *,
    status: RetentionStatus,
    policy: RetentionPolicy = RetentionPolicy.TICKET_OR_DEADLINE,
    hard_delete_at: datetime = NOW + timedelta(days=90),
    deletion_reason: DeletionReason | None = None,
    deletion_started_at: datetime | None = None,
) -> tuple[Session, AudioRetention]:
    session = create_autospec(Session, instance=True)
    next_check_at = (
        NOW
        if status in {RetentionStatus.SCHEDULED, RetentionStatus.RETRY_SCHEDULED}
        else None
    )
    error_category = (
        "service_unavailable"
        if status in {RetentionStatus.RETRY_SCHEDULED, RetentionStatus.BLOCKED}
        else None
    )
    error_code = "retryable_error" if error_category is not None else None
    retention = AudioRetention(
        job_uuid=JOB_UUID,
        policy=policy.value,
        status=status.value,
        hard_delete_at=hard_delete_at,
        next_check_at=next_check_at,
        last_checked_at=NOW - timedelta(minutes=2),
        last_glpi_status_id=(
            2 if policy is RetentionPolicy.TICKET_OR_DEADLINE else None
        ),
        check_attempt_count=3,
        consecutive_retry_count=(1 if status is RetentionStatus.RETRY_SCHEDULED else 0),
        deletion_reason=(
            deletion_reason.value if deletion_reason is not None else None
        ),
        deletion_started_at=deletion_started_at,
        audio_deleted_at=None,
        last_error_category=error_category,
        last_error_code=error_code,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=1),
    )
    session.get.return_value = retention
    return session, retention
