from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    AudioRetention,
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    InvalidRetentionTransitionError,
    RetentionNotFoundError,
    RetentionPolicy,
    RetentionStatus,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_activate_creates_only_an_audio_retention() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None
    job_uuid = uuid4()

    AudioRetentionRepository(session).activate(
        job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        hard_delete_at=NOW + timedelta(days=90),
        next_check_at=NOW + timedelta(hours=6),
        now=NOW,
    )

    retention = session.add.call_args.args[0]
    assert isinstance(retention, AudioRetention)
    assert retention.job_uuid == job_uuid
    assert retention.policy == RetentionPolicy.TICKET_OR_DEADLINE.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.hard_delete_at == NOW + timedelta(days=90)
    assert retention.next_check_at == NOW + timedelta(hours=6)
    session.flush.assert_called_once_with()


def test_activate_reactivates_only_an_inactive_audio_retention() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    retention = AudioRetention(
        job_uuid=job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
        status=RetentionStatus.INACTIVE.value,
        hard_delete_at=None,
        next_check_at=None,
        check_attempt_count=3,
        consecutive_retry_count=0,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )
    session.get.return_value = retention

    AudioRetentionRepository(session).activate(
        job_uuid,
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=None,
        now=NOW,
    )

    assert retention.policy == RetentionPolicy.DEADLINE_ONLY.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.hard_delete_at == NOW + timedelta(days=30)
    assert retention.next_check_at is None
    assert retention.check_attempt_count == 3
    assert retention.consecutive_retry_count == 0
    assert retention.last_error_category is None
    assert retention.last_error_code is None
    session.add.assert_not_called()
    session.flush.assert_called_once_with()


@pytest.mark.parametrize(
    "status",
    [
        RetentionStatus.SCHEDULED,
        RetentionStatus.CHECKING,
        RetentionStatus.RETRY_SCHEDULED,
        RetentionStatus.DELETING,
        RetentionStatus.DELETED,
        RetentionStatus.BLOCKED,
    ],
)
def test_activate_never_overwrites_an_active_or_terminal_retention(
    status: RetentionStatus,
) -> None:
    session = create_autospec(Session, instance=True)
    retention = _retention(uuid4(), status=status)
    retention.deletion_started_at = (
        NOW - timedelta(minutes=5)
        if status
        in {
            RetentionStatus.RETRY_SCHEDULED,
            RetentionStatus.DELETING,
            RetentionStatus.DELETED,
            RetentionStatus.BLOCKED,
        }
        else None
    )
    session.get.return_value = retention
    with pytest.raises(InvalidRetentionTransitionError, match="activate"):
        AudioRetentionRepository(session).activate(
            retention.job_uuid,
            policy=RetentionPolicy.DEADLINE_ONLY,
            hard_delete_at=NOW + timedelta(days=30),
            next_check_at=None,
            now=NOW,
        )

    assert retention.status == status.value
    session.flush.assert_not_called()


@pytest.mark.parametrize(
    ("policy", "next_check_at"),
    [
        (RetentionPolicy.TICKET_OR_DEADLINE, None),
        (RetentionPolicy.DEADLINE_ONLY, NOW + timedelta(hours=6)),
    ],
)
def test_activate_rejects_a_schedule_incompatible_with_its_policy(
    policy: RetentionPolicy,
    next_check_at: datetime | None,
) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError):
        AudioRetentionRepository(session).activate(
            uuid4(),
            policy=policy,
            hard_delete_at=NOW + timedelta(days=30),
            next_check_at=next_check_at,
            now=NOW,
        )

    session.get.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_get_by_job_uuid_returns_a_typed_snapshot() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    retention = _retention(job_uuid, status=RetentionStatus.SCHEDULED)
    session.get.return_value = retention

    snapshot = AudioRetentionRepository(session).get_by_job_uuid(job_uuid)

    assert snapshot == AudioRetentionSnapshot(
        job_uuid=job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE,
        status=RetentionStatus.SCHEDULED,
        hard_delete_at=NOW + timedelta(days=90),
        next_check_at=NOW + timedelta(hours=6),
        last_checked_at=NOW - timedelta(hours=1),
        last_glpi_status_id=2,
        check_attempt_count=4,
        consecutive_retry_count=2,
        deletion_reason=None,
        deletion_started_at=None,
        audio_deleted_at=None,
        last_error_category=None,
        last_error_code=None,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=5),
    )
    session.get.assert_called_once_with(AudioRetention, job_uuid)
    session.flush.assert_not_called()


def test_deactivate_for_operator_retry_clears_transient_retention_state() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    retention = _retention(job_uuid, status=RetentionStatus.BLOCKED)
    retention.next_check_at = None
    retention.last_error_category = "glpi_unavailable"
    retention.last_error_code = "http_503"
    session.get.return_value = retention

    snapshot = AudioRetentionRepository(session).deactivate_for_operator_retry(
        job_uuid,
        now=NOW,
    )

    assert snapshot.status is RetentionStatus.INACTIVE
    assert snapshot.policy is RetentionPolicy.TICKET_OR_DEADLINE
    assert snapshot.hard_delete_at is None
    assert snapshot.next_check_at is None
    assert snapshot.last_checked_at is None
    assert snapshot.last_glpi_status_id is None
    assert snapshot.check_attempt_count == 4
    assert snapshot.consecutive_retry_count == 0
    assert snapshot.deletion_reason is None
    assert snapshot.deletion_started_at is None
    assert snapshot.audio_deleted_at is None
    assert snapshot.last_error_category is None
    assert snapshot.last_error_code is None
    assert snapshot.created_at == NOW - timedelta(days=2)
    assert snapshot.updated_at == NOW
    session.get.assert_called_once_with(
        AudioRetention,
        job_uuid,
        with_for_update=True,
    )
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_deactivate_for_operator_retry_rejects_a_missing_retention() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None

    with pytest.raises(RetentionNotFoundError):
        AudioRetentionRepository(session).deactivate_for_operator_retry(
            uuid4(),
            now=NOW,
        )

    session.flush.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [
        RetentionStatus.INACTIVE,
        RetentionStatus.CHECKING,
        RetentionStatus.DELETING,
        RetentionStatus.DELETED,
    ],
)
def test_deactivate_for_operator_retry_rejects_an_unsafe_status(
    status: RetentionStatus,
) -> None:
    session = create_autospec(Session, instance=True)
    retention = _retention(uuid4(), status=status)
    session.get.return_value = retention

    with pytest.raises(InvalidRetentionTransitionError):
        AudioRetentionRepository(session).deactivate_for_operator_retry(
            retention.job_uuid,
            now=NOW,
        )

    session.flush.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [RetentionStatus.RETRY_SCHEDULED, RetentionStatus.BLOCKED],
)
def test_deactivate_for_operator_retry_rejects_started_deletion(
    status: RetentionStatus,
) -> None:
    session = create_autospec(Session, instance=True)
    retention = _retention(uuid4(), status=status)
    retention.next_check_at = (
        NOW + timedelta(hours=1) if status is RetentionStatus.RETRY_SCHEDULED else None
    )
    retention.deletion_reason = "deadline_expired"
    retention.deletion_started_at = NOW - timedelta(minutes=5)
    retention.last_error_category = "storage_unavailable"
    retention.last_error_code = "filesystem_busy"
    session.get.return_value = retention

    with pytest.raises(InvalidRetentionTransitionError, match="started deletion"):
        AudioRetentionRepository(session).deactivate_for_operator_retry(
            retention.job_uuid,
            now=NOW,
        )

    assert retention.status == status.value
    assert retention.deletion_started_at == NOW - timedelta(minutes=5)
    session.flush.assert_not_called()


def test_retention_repository_never_owns_transaction_or_session() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None

    AudioRetentionRepository(session).activate(
        uuid4(),
        policy=RetentionPolicy.DEADLINE_ONLY,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=None,
        now=NOW,
    )

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def _retention(job_uuid: UUID, *, status: RetentionStatus) -> AudioRetention:
    return AudioRetention(
        job_uuid=job_uuid,
        policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
        status=status.value,
        hard_delete_at=NOW + timedelta(days=90),
        next_check_at=NOW + timedelta(hours=6),
        last_checked_at=NOW - timedelta(hours=1),
        last_glpi_status_id=2,
        check_attempt_count=4,
        consecutive_retry_count=2,
        deletion_reason=None,
        deletion_started_at=None,
        audio_deleted_at=None,
        last_error_category=None,
        last_error_code=None,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=5),
    )
