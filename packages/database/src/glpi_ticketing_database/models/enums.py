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


class MailIngestionStatus(StrEnum):
    """Persisted state of one Exchange message ingestion."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MailIngestionStage(StrEnum):
    """Last publisher stage reached while ingesting a message."""

    DISCOVERY = "discovery"
    VALIDATION = "validation"
    PUBLICATION = "publication"
    ACKNOWLEDGEMENT = "acknowledgement"


class MailIngestionDisposition(StrEnum):
    """Durable local decision made before acknowledging a message."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class MailIngestionErrorScope(StrEnum):
    """Scope of a sanitized publisher error."""

    MESSAGE = "message"
    SYSTEM = "system"


class MailIngestionEventType(StrEnum):
    """Stable audit events emitted by the publisher lifecycle."""

    MESSAGE_DISCOVERED = "message_discovered"
    PROCESSING_STARTED = "processing_started"
    PROCESSING_RESUMED = "processing_resumed"
    ATTACHMENT_VALIDATED = "attachment_validated"
    AUDIO_PUBLISHED = "audio_published"
    MESSAGE_REJECTED = "message_rejected"
    RETRY_SCHEDULED = "retry_scheduled"
    MESSAGE_MOVED = "message_moved"
    INGESTION_COMPLETED = "ingestion_completed"
    INGESTION_FAILED = "ingestion_failed"
    INGESTION_BLOCKED = "ingestion_blocked"


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
    WORKER_PUBLISHER = "worker_publisher"


class JobEventType(StrEnum):
    """Stable event labels emitted by application lifecycle services."""

    JOB_CREATED = "job_created"
    PROCESSING_STARTED = "processing_started"
    PROCESSING_RESUMED = "processing_resumed"
    TRANSCRIPTION_SUBMITTED = "transcription_submitted"
    TRANSCRIPTION_SUCCEEDED = "transcription_succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    TICKET_CONFIRMED = "ticket_confirmed"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_BLOCKED = "job_blocked"
    OPERATOR_RETRY_REQUESTED = "operator_retry_requested"
    RETENTION_RETRY_SCHEDULED = "retention_retry_scheduled"
    RETENTION_BLOCKED = "retention_blocked"
    AUDIO_DELETION_STARTED = "audio_deletion_started"
    AUDIO_DELETED = "audio_deleted"


class WorkerServiceName(StrEnum):
    """Singleton worker services represented in the runtime table."""

    AUTOMATION = "worker_automation"
    RETENTION = "worker_retention"
    PUBLISHER = "worker_publisher"


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
    "JobEventType",
    "JobStage",
    "JobStatus",
    "MailIngestionDisposition",
    "MailIngestionErrorScope",
    "MailIngestionEventType",
    "MailIngestionStage",
    "MailIngestionStatus",
    "RetentionPolicy",
    "RetentionStatus",
    "WorkerRuntimeState",
    "WorkerServiceName",
]
