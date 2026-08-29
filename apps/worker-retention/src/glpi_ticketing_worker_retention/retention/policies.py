"""Deterministic scheduling policies for retention checks and probes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from glpi_ticketing_database import DeletionReason, JobStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionCheckPolicy:
    """Compute bounded GLPI checks and deadline deletion reasons."""

    check_interval: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.check_interval, timedelta):
            raise TypeError("check_interval must be a timedelta")
        if self.check_interval <= timedelta(0):
            raise ValueError("check_interval must be positive")

    def next_check_at(self, *, now: datetime, hard_delete_at: datetime) -> datetime:
        """Return the next check, never later than the absolute deadline."""
        _require_aware(now, "now")
        _require_aware(hard_delete_at, "hard_delete_at")
        if hard_delete_at <= now:
            raise ValueError("hard_delete_at must be later than now")
        return min(now + self.check_interval, hard_delete_at)

    def clamp_retry(
        self,
        *,
        proposed_at: datetime,
        now: datetime,
        hard_delete_at: datetime,
    ) -> datetime:
        """Bound a GLPI retry by its deadline while preserving a future time."""
        _require_aware(proposed_at, "proposed_at")
        _require_aware(now, "now")
        _require_aware(hard_delete_at, "hard_delete_at")
        if proposed_at <= now:
            raise ValueError("proposed_at must be later than now")
        if hard_delete_at <= now:
            raise ValueError("hard_delete_at must be later than now")
        return min(proposed_at, hard_delete_at)

    @staticmethod
    def deadline_reason(job_status: JobStatus) -> DeletionReason:
        """Map one terminal job state to its auditable TTL reason."""
        if not isinstance(job_status, JobStatus):
            raise TypeError("job_status must be a JobStatus")
        if job_status is JobStatus.COMPLETED:
            return DeletionReason.DEADLINE_EXPIRED
        if job_status is JobStatus.FAILED:
            return DeletionReason.FAILED_TTL
        if job_status is JobStatus.BLOCKED:
            return DeletionReason.BLOCKED_TTL
        raise ValueError("retention deadlines require a terminal job")


def _require_aware(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["RetentionCheckPolicy"]
