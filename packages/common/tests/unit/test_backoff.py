"""Contract tests for the shared deterministic retry backoff."""

from datetime import UTC, datetime

import pytest

from glpi_ticketing_common.backoff import ExponentialBackoffPolicy


def test_exponential_backoff_is_capped() -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=5,
        maximum_seconds=20,
    )

    assert [
        policy.delay_seconds(consecutive_retry_count=count) for count in range(5)
    ] == [5.0, 10.0, 20.0, 20.0, 20.0]


def test_exponential_backoff_handles_a_huge_counter_without_overflow() -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=5,
        maximum_seconds=300,
    )

    assert (
        policy.delay_seconds(consecutive_retry_count=10**100) == policy.maximum_seconds
    )


def test_exponential_backoff_returns_an_aware_retry_instant() -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=2.5,
        maximum_seconds=10,
    )
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    assert policy.next_attempt_at(now=now, consecutive_retry_count=2) == datetime(
        2026,
        8,
        28,
        12,
        0,
        10,
        tzinfo=UTC,
    )


@pytest.mark.parametrize(
    ("initial", "maximum", "exception"),
    [
        (True, 10, TypeError),
        ("5", 10, TypeError),
        (0, 10, ValueError),
        (-1, 10, ValueError),
        (float("nan"), 10, ValueError),
        (5, float("inf"), ValueError),
        (10**400, 10**400, ValueError),
        (11, 10, ValueError),
    ],
)
def test_exponential_backoff_rejects_invalid_bounds(
    initial: object,
    maximum: object,
    exception: type[Exception],
) -> None:
    with pytest.raises(exception):
        ExponentialBackoffPolicy(
            initial_seconds=initial,  # type: ignore[arg-type]
            maximum_seconds=maximum,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("count", "exception"),
    [
        (True, TypeError),
        (1.0, TypeError),
        ("1", TypeError),
        (-1, ValueError),
    ],
)
def test_exponential_backoff_rejects_invalid_retry_counters(
    count: object,
    exception: type[Exception],
) -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=5,
        maximum_seconds=20,
    )

    with pytest.raises(exception):
        policy.delay_seconds(
            consecutive_retry_count=count,  # type: ignore[arg-type]
        )


def test_exponential_backoff_rejects_naive_datetimes() -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=5,
        maximum_seconds=20,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        policy.next_attempt_at(
            now=datetime(2026, 8, 28, 12, 0),
            consecutive_retry_count=0,
        )


def test_exponential_backoff_reports_an_unrepresentable_instant() -> None:
    policy = ExponentialBackoffPolicy(
        initial_seconds=1,
        maximum_seconds=1,
    )

    with pytest.raises(ValueError, match="cannot be represented"):
        policy.next_attempt_at(
            now=datetime.max.replace(tzinfo=UTC),
            consecutive_retry_count=0,
        )
