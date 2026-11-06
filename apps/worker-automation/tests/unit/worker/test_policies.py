from datetime import UTC, datetime, timedelta

import pytest

from mevo2glpi_worker_automation.worker import (
    RetentionSchedulePolicy,
    RetryBackoffPolicy,
)

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


def test_retry_backoff_is_exponential_and_capped() -> None:
    policy = RetryBackoffPolicy(initial_seconds=5, maximum_seconds=20)

    assert [
        policy.next_attempt_at(now=NOW, consecutive_retry_count=count)
        for count in range(5)
    ] == [
        NOW + timedelta(seconds=5),
        NOW + timedelta(seconds=10),
        NOW + timedelta(seconds=20),
        NOW + timedelta(seconds=20),
        NOW + timedelta(seconds=20),
    ]


def test_retry_backoff_handles_a_large_persisted_counter_without_overflow() -> None:
    policy = RetryBackoffPolicy(initial_seconds=5, maximum_seconds=300)

    assert policy.next_attempt_at(
        now=NOW,
        consecutive_retry_count=10_000,
    ) == NOW + timedelta(seconds=300)


def test_retention_schedule_uses_independent_terminal_ttls() -> None:
    policy = RetentionSchedulePolicy(
        completed_ttl=timedelta(days=90),
        failed_ttl=timedelta(days=30),
        blocked_ttl=timedelta(days=90),
        first_completed_check_after=timedelta(hours=6),
    )

    assert policy.completed_schedule(NOW) == (
        NOW + timedelta(days=90),
        NOW + timedelta(hours=6),
    )
    assert policy.failed_deadline(NOW) == NOW + timedelta(days=30)
    assert policy.blocked_deadline(NOW) == NOW + timedelta(days=90)


def test_first_retention_check_must_precede_hard_deadline() -> None:
    with pytest.raises(ValueError, match="must precede"):
        RetentionSchedulePolicy(
            completed_ttl=timedelta(hours=6),
            failed_ttl=timedelta(days=30),
            blocked_ttl=timedelta(days=90),
            first_completed_check_after=timedelta(hours=6),
        )
