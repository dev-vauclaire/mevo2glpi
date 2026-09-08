"""Stable, sanitized HTTP contracts for dashboard observability."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WorkerService(StrEnum):
    """Singleton workers expected by the MVP deployment."""

    PUBLISHER = "worker_publisher"
    AUTOMATION = "worker_automation"
    RETENTION = "worker_retention"


class WorkerHeartbeatStatus(StrEnum):
    """Freshness of a worker checkpoint exposed to operators."""

    ABSENT = "absent"
    FRESH = "fresh"
    STALE = "stale"


class WorkerRuntimeState(StrEnum):
    """Stable runtime states emitted by the three workers."""

    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class OperationalAlertKind(StrEnum):
    """Current conditions rendered by the internal dashboard."""

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


class OperationalAlertSeverity(StrEnum):
    """Bounded alert severities understood by the frontend."""

    WARNING = "warning"
    CRITICAL = "critical"


class WorkerObservationResponse(BaseModel):
    """Sanitized dashboard projection of one expected worker."""

    model_config = ConfigDict(frozen=True)
    service_name: WorkerService
    heartbeat_status: WorkerHeartbeatStatus
    runtime_state: WorkerRuntimeState | None
    last_heartbeat_at: datetime | None
    current_job_uuid: UUID | None
    current_ingestion_uuid: UUID | None
    last_error_category: str | None
    last_error_code: str | None


class OperationalAlertResponse(BaseModel):
    """One deduplicated and aggregated active alert."""

    model_config = ConfigDict(frozen=True)
    key: str
    kind: OperationalAlertKind
    severity: OperationalAlertSeverity
    count: int
    condition_since: datetime | None


class ObservabilityResponse(BaseModel):
    """Complete bounded response generated from one UTC observation."""

    model_config = ConfigDict(frozen=True)
    observed_at: datetime
    workers: tuple[WorkerObservationResponse, ...]
    alerts: tuple[OperationalAlertResponse, ...]


__all__ = [
    "ObservabilityResponse",
    "OperationalAlertKind",
    "OperationalAlertResponse",
    "OperationalAlertSeverity",
    "WorkerHeartbeatStatus",
    "WorkerObservationResponse",
    "WorkerRuntimeState",
    "WorkerService",
]
