"""Deterministic retry and retention schedules owned by the worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mevo2glpi_common.backoff import (
    ExponentialBackoffPolicy as RetryBackoffPolicy,
)


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


def _aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["RetentionSchedulePolicy", "RetryBackoffPolicy"]
