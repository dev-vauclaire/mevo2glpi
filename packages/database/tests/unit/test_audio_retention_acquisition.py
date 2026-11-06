from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from mevo2glpi_database import (
    AudioRetention,
    AudioRetentionRepository,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionPolicy,
    RetentionStatus,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
JOB_UUID = UUID("4dc17ce3-c7ce-46d4-a14d-b83f0fb7e098")


@pytest.mark.parametrize(
    ("preceding_empty_results", "status", "kind"),
    [
        (0, RetentionStatus.DELETING, RetentionCandidateKind.RESUME_DELETION),
        (
            1,
            RetentionStatus.CHECKING,
            RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK,
        ),
        (
            2,
            RetentionStatus.SCHEDULED,
            RetentionCandidateKind.START_DELETION_AT_DEADLINE,
        ),
        (
            3,
            RetentionStatus.RETRY_SCHEDULED,
            RetentionCandidateKind.RESUME_DELETION_RETRY,
        ),
        (
            4,
            RetentionStatus.SCHEDULED,
            RetentionCandidateKind.START_GLPI_CHECK,
        ),
    ],
)
def test_find_next_candidate_applies_the_global_priority_without_locking(
    preceding_empty_results: int,
    status: RetentionStatus,
    kind: RetentionCandidateKind,
) -> None:
    session = create_autospec(Session, instance=True)
    retention = _retention(status=status)
    results = [_scalar_result(None) for _index in range(preceding_empty_results)]
    results.append(_scalar_result(retention))
    session.scalars.side_effect = results

    candidate = AudioRetentionRepository(session).find_next_candidate(NOW)

    assert isinstance(candidate, RetentionCandidate)
    assert candidate.kind is kind
    assert candidate.retention.job_uuid == JOB_UUID
    assert session.scalars.call_count == preceding_empty_results + 1
    for call in session.scalars.call_args_list:
        assert call.args[0]._for_update_arg is None
    session.get.assert_not_called()
    session.flush.assert_not_called()


def test_find_next_candidate_returns_none_after_all_categories_are_empty() -> None:
    session = create_autospec(Session, instance=True)
    session.scalars.side_effect = [_scalar_result(None) for _index in range(5)]

    assert AudioRetentionRepository(session).find_next_candidate(NOW) is None

    assert session.scalars.call_count == 5
    session.flush.assert_not_called()


def test_find_next_candidate_rejects_a_naive_clock_before_reading() -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="timezone-aware"):
        AudioRetentionRepository(session).find_next_candidate(datetime(2026, 8, 28, 12))

    session.scalars.assert_not_called()


def test_get_glpi_probe_gate_returns_a_read_only_snapshot() -> None:
    session = create_autospec(Session, instance=True)
    retention = _retention(status=RetentionStatus.RETRY_SCHEDULED)
    retention.deletion_reason = None
    retention.deletion_started_at = None
    session.scalars.return_value = _scalar_result(retention)

    probe = AudioRetentionRepository(session).get_glpi_probe_gate()

    assert probe is not None
    assert probe.job_uuid == JOB_UUID
    assert probe.status is RetentionStatus.RETRY_SCHEDULED
    assert probe.deletion_started_at is None
    statement = session.scalars.call_args.args[0]
    assert statement._for_update_arg is None
    session.get.assert_not_called()
    session.flush.assert_not_called()


def test_get_glpi_probe_gate_returns_none_when_no_retry_is_persisted() -> None:
    session = create_autospec(Session, instance=True)
    session.scalars.return_value = _scalar_result(None)

    assert AudioRetentionRepository(session).get_glpi_probe_gate() is None

    session.flush.assert_not_called()


def _scalar_result(retention: AudioRetention | None) -> Mock:
    result = Mock()
    result.first.return_value = retention
    return result


def _retention(*, status: RetentionStatus) -> AudioRetention:
    deletion_started_at = (
        NOW - timedelta(minutes=2)
        if status
        in {
            RetentionStatus.DELETING,
            RetentionStatus.RETRY_SCHEDULED,
        }
        else None
    )
    return AudioRetention(
        job_uuid=JOB_UUID,
        policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
        status=status.value,
        hard_delete_at=NOW,
        next_check_at=(
            NOW - timedelta(minutes=1)
            if status in {RetentionStatus.SCHEDULED, RetentionStatus.RETRY_SCHEDULED}
            else None
        ),
        last_checked_at=NOW - timedelta(minutes=3),
        last_glpi_status_id=2,
        check_attempt_count=3,
        consecutive_retry_count=(1 if status is RetentionStatus.RETRY_SCHEDULED else 0),
        deletion_reason=(
            "deadline_expired" if deletion_started_at is not None else None
        ),
        deletion_started_at=deletion_started_at,
        audio_deleted_at=None,
        last_error_category=(
            "storage_unavailable" if status is RetentionStatus.RETRY_SCHEDULED else None
        ),
        last_error_code=(
            "filesystem_busy" if status is RetentionStatus.RETRY_SCHEDULED else None
        ),
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(minutes=1),
    )
