"""HTTP-independent observability values exposed by the web use case."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from glpi_ticketing_database import WorkerRuntimeState, WorkerServiceName


class ObservabilityUnavailableError(RuntimeError):
    """Report a sanitized failure to read the operational dashboard state."""


class HeartbeatStatus(StrEnum):
    """Freshness of the latest checkpoint emitted by an expected worker."""

    ABSENT = "absent"
    FRESH = "fresh"
    STALE = "stale"


class AlertSeverity(StrEnum):
    """Small, stable severity scale used by the MVP dashboard."""

    WARNING = "warning"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    """Deduplicated operational conditions understood by the dashboard."""

    WORKER_ABSENT = "worker_absent"
    WORKER_STOPPED = "worker_stopped"
    WORKER_STALE = "worker_stale"
    WORKER_BLOCKED = "worker_blocked"
    WORKER_DEGRADED = "worker_degraded"
    JOB_BLOCKED = "job_blocked"
    JOB_FAILED = "job_failed"
    JOB_PENDING_OVERDUE = "job_pending_overdue"
    JOB_RETRY_OVERDUE = "job_retry_overdue"
    RETENTION_DEADLINE_OVERDUE = "retention_deadline_overdue"
    RETENTION_GLPI_CHECK_BLOCKED = "retention_glpi_check_blocked"
    RETENTION_AUDIO_DELETION_BLOCKED = "retention_audio_deletion_blocked"
    RETENTION_GLPI_RETRY_OVERDUE = "retention_glpi_retry_overdue"
    RETENTION_AUDIO_DELETION_RETRY_OVERDUE = "retention_audio_deletion_retry_overdue"


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservabilityThresholds:
    """Validated durations used by one internally consistent observation."""

    worker_stale_after: timedelta
    pending_alert_after: timedelta
    retry_overdue_grace: timedelta
    retention_deadline_grace: timedelta

    @classmethod
    def from_seconds(
        cls,
        *,
        worker_stale_after: float,
        pending_alert_after: float,
        retry_overdue_grace: float,
        retention_deadline_grace: float,
    ) -> ObservabilityThresholds:
        """Build thresholds while rejecting booleans, infinities and overflow."""
        return cls(
            worker_stale_after=_positive_timedelta(
                worker_stale_after,
                "worker_stale_after",
            ),
            pending_alert_after=_positive_timedelta(
                pending_alert_after,
                "pending_alert_after",
            ),
            retry_overdue_grace=_positive_timedelta(
                retry_overdue_grace,
                "retry_overdue_grace",
            ),
            retention_deadline_grace=_positive_timedelta(
                retention_deadline_grace,
                "retention_deadline_grace",
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerObservation:
    """Sanitized current state of one expected singleton worker."""

    service_name: WorkerServiceName
    heartbeat_status: HeartbeatStatus
    runtime_state: WorkerRuntimeState | None
    last_heartbeat_at: datetime | None
    current_job_uuid: UUID | None
    last_error_category: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationalAlert:
    """One current, aggregated condition requiring operator attention."""

    key: str
    kind: AlertKind
    severity: AlertSeverity
    count: int
    condition_since: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservabilitySnapshot:
    """Consistent worker and lifecycle observation produced from one clock tick."""

    observed_at: datetime
    workers: tuple[WorkerObservation, ...]
    alerts: tuple[OperationalAlert, ...]


def _positive_timedelta(value: float, field_name: str) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
    try:
        return timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError(f"{field_name} is too large") from None


__all__ = [
    "AlertKind",
    "AlertSeverity",
    "HeartbeatStatus",
    "ObservabilitySnapshot",
    "ObservabilityThresholds",
    "ObservabilityUnavailableError",
    "OperationalAlert",
    "WorkerObservation",
]
