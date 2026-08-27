"""Stable values persisted by the lifecycle models."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Persisted state of an audio-to-ticket job."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class JobStage(StrEnum):
    """Last pipeline stage reached by a job."""

    TRANSCRIPTION = "transcription"
    GLPI_CREATION = "glpi_creation"
    FINALIZATION = "finalization"


class AudioExtension(StrEnum):
    """Audio extensions accepted by the MVP, without a leading dot."""

    WAV = "wav"
    MP3 = "mp3"
    OGG = "ogg"
    M4A = "m4a"


class ErrorScope(StrEnum):
    """Scope of a sanitized error recorded for a job."""

    JOB = "job"
    SYSTEM = "system"


class RetentionPolicy(StrEnum):
    """Strategy used to decide when an audio may be deleted."""

    TICKET_OR_DEADLINE = "ticket_or_deadline"
    DEADLINE_ONLY = "deadline_only"


class RetentionStatus(StrEnum):
    """Persisted state of an audio-retention policy."""

    INACTIVE = "inactive"
    SCHEDULED = "scheduled"
    CHECKING = "checking"
    RETRY_SCHEDULED = "retry_scheduled"
    DELETING = "deleting"
    DELETED = "deleted"
    BLOCKED = "blocked"


class DeletionReason(StrEnum):
    """Stable reason explaining why audio bytes were deleted."""

    TICKET_CLOSED = "ticket_closed"
    DEADLINE_EXPIRED = "deadline_expired"
    FAILED_TTL = "failed_ttl"
    BLOCKED_TTL = "blocked_ttl"


class JobEventActor(StrEnum):
    """Application that emitted a lifecycle event."""

    WEB = "web"
    WORKER_AUTOMATION = "worker_automation"
    WORKER_RETENTION = "worker_retention"


class WorkerServiceName(StrEnum):
    """Singleton worker services represented in the runtime table."""

    AUTOMATION = "worker_automation"
    RETENTION = "worker_retention"


class WorkerRuntimeState(StrEnum):
    """Health state exposed to the MVP dashboard."""

    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    STOPPED = "stopped"


def sql_values(enum_type: type[StrEnum]) -> str:
    """Render trusted enum literals for static check constraints."""

    return ", ".join(f"'{member.value}'" for member in enum_type)


__all__ = [
    "AudioExtension",
    "DeletionReason",
    "ErrorScope",
    "JobEventActor",
    "JobStage",
    "JobStatus",
    "RetentionPolicy",
    "RetentionStatus",
    "WorkerRuntimeState",
    "WorkerServiceName",
]
