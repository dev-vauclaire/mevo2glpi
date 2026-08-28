"""Deterministic retry and retention schedules owned by the worker."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryBackoffPolicy:
    """Compute a capped exponential retry without sleeping in the worker."""

    initial_seconds: float
    maximum_seconds: float

    def __post_init__(self) -> None:
        initial = _positive_seconds(self.initial_seconds, "initial_seconds")
        maximum = _positive_seconds(self.maximum_seconds, "maximum_seconds")
        if initial > maximum:
            raise ValueError("initial_seconds must not exceed maximum_seconds")
        object.__setattr__(self, "initial_seconds", initial)
        object.__setattr__(self, "maximum_seconds", maximum)

    def next_attempt_at(
        self,
        *,
        now: datetime,
        consecutive_retry_count: int,
    ) -> datetime:
        """Return the deadline for the next failure in the consecutive series.

        ``consecutive_retry_count`` is the value already persisted before the new
        failure. Zero therefore yields the initial delay.
        """

        _aware_datetime(now, "now")
        if isinstance(consecutive_retry_count, bool) or not isinstance(
            consecutive_retry_count, int
        ):
            raise TypeError("consecutive_retry_count must be an integer")
        if consecutive_retry_count < 0:
            raise ValueError("consecutive_retry_count must be non-negative")

        delay = self.initial_seconds
        for _ in range(consecutive_retry_count):
            if delay >= self.maximum_seconds / 2:
                delay = self.maximum_seconds
                break
            delay *= 2
        return now + timedelta(seconds=min(delay, self.maximum_seconds))


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionSchedulePolicy:
    """Compute the absolute deadlines persisted with terminal job states."""

    completed_ttl: timedelta
    failed_ttl: timedelta
    blocked_ttl: timedelta
    first_completed_check_after: timedelta

    def __post_init__(self) -> None:
        for field_name in (
            "completed_ttl",
            "failed_ttl",
            "blocked_ttl",
            "first_completed_check_after",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, timedelta):
                raise TypeError(f"{field_name} must be a timedelta")
            if value <= timedelta(0):
                raise ValueError(f"{field_name} must be positive")
        if self.first_completed_check_after >= self.completed_ttl:
            raise ValueError(
                "first_completed_check_after must precede the completed deadline"
            )

    def completed_schedule(self, now: datetime) -> tuple[datetime, datetime]:
        """Return ``(hard_delete_at, next_check_at)`` for a completed job."""

        _aware_datetime(now, "now")
        return now + self.completed_ttl, now + self.first_completed_check_after

    def failed_deadline(self, now: datetime) -> datetime:
        """Return the absolute deletion deadline for a failed job."""

        _aware_datetime(now, "now")
        return now + self.failed_ttl

    def blocked_deadline(self, now: datetime) -> datetime:
        """Return the absolute deletion deadline for a blocked job."""

        _aware_datetime(now, "now")
        return now + self.blocked_ttl


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return normalized


def _aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["RetentionSchedulePolicy", "RetryBackoffPolicy"]
