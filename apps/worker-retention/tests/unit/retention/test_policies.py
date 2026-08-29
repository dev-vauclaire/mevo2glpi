"""Unit tests for bounded retention scheduling policies."""

from datetime import timedelta

import pytest

from glpi_ticketing_database import DeletionReason, JobStatus
from glpi_ticketing_worker_retention.retention.policies import RetentionCheckPolicy

from ._fakes import HARD_DELETE_AT, NOW


def test_next_check_uses_interval_before_the_deadline() -> None:
    policy = RetentionCheckPolicy(check_interval=timedelta(hours=6))

    assert policy.next_check_at(
        now=NOW,
        hard_delete_at=HARD_DELETE_AT,
    ) == NOW + timedelta(hours=6)


def test_next_check_and_retry_are_clamped_to_the_hard_deadline() -> None:
    policy = RetentionCheckPolicy(check_interval=timedelta(days=60))

    assert (
        policy.next_check_at(
            now=NOW,
            hard_delete_at=HARD_DELETE_AT,
        )
        == HARD_DELETE_AT
    )
    assert (
        policy.clamp_retry(
            proposed_at=HARD_DELETE_AT + timedelta(days=1),
            now=NOW,
            hard_delete_at=HARD_DELETE_AT,
        )
        == HARD_DELETE_AT
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (JobStatus.COMPLETED, DeletionReason.DEADLINE_EXPIRED),
        (JobStatus.FAILED, DeletionReason.FAILED_TTL),
        (JobStatus.BLOCKED, DeletionReason.BLOCKED_TTL),
    ],
)
def test_deadline_reason_is_explicit_for_each_terminal_job(
    status: JobStatus,
    reason: DeletionReason,
) -> None:
    assert RetentionCheckPolicy.deadline_reason(status) is reason


@pytest.mark.parametrize(
    "status",
    [JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.RETRY_SCHEDULED],
)
def test_deadline_reason_rejects_non_terminal_jobs(status: JobStatus) -> None:
    with pytest.raises(ValueError, match="terminal"):
        RetentionCheckPolicy.deadline_reason(status)


def test_policy_rejects_non_positive_or_non_timedelta_intervals() -> None:
    for value in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="positive"):
            RetentionCheckPolicy(check_interval=value)
    with pytest.raises(TypeError, match="timedelta"):
        RetentionCheckPolicy(check_interval=60)  # type: ignore[arg-type]


def test_policy_rejects_elapsed_or_naive_bounds() -> None:
    policy = RetentionCheckPolicy(check_interval=timedelta(hours=6))

    with pytest.raises(ValueError, match="later than now"):
        policy.next_check_at(now=NOW, hard_delete_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.next_check_at(
            now=NOW.replace(tzinfo=None),
            hard_delete_at=HARD_DELETE_AT,
        )
    with pytest.raises(ValueError, match="proposed_at must be later"):
        policy.clamp_retry(
            proposed_at=NOW,
            now=NOW,
            hard_delete_at=HARD_DELETE_AT,
        )
