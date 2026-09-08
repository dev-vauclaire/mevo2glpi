"""Stable, sanitized HTTP contracts for lifecycle jobs."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobState(StrEnum):
    """Stable lifecycle states exposed by the web API."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class JobStage(StrEnum):
    """Stable processing stages exposed by the web API."""

    TRANSCRIPTION = "transcription"
    GLPI_CREATION = "glpi_creation"
    FINALIZATION = "finalization"


class AudioFileExtension(StrEnum):
    """Canonical audio extensions accepted by ingestion."""

    WAV = "wav"
    MP3 = "mp3"
    OGG = "ogg"
    M4A = "m4a"


class JobErrorScope(StrEnum):
    """Sanitized scope of a lifecycle failure."""

    JOB = "job"
    SYSTEM = "system"


class RetentionState(StrEnum):
    """Stable retention states relevant to operators."""

    INACTIVE = "inactive"
    SCHEDULED = "scheduled"
    CHECKING = "checking"
    RETRY_SCHEDULED = "retry_scheduled"
    DELETING = "deleting"
    DELETED = "deleted"
    BLOCKED = "blocked"


class JobEventActor(StrEnum):
    """Applications allowed to emit dashboard events."""

    WEB = "web"
    WORKER_AUTOMATION = "worker_automation"
    WORKER_RETENTION = "worker_retention"
    WORKER_PUBLISHER = "worker_publisher"


class JobEventName(StrEnum):
    """Stable, sanitized event labels exposed in job history."""

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


class JobAcceptedResponse(BaseModel):
    """Durable ingestion acknowledgement."""

    model_config = ConfigDict(frozen=True)
    audio_uuid: UUID
    state: JobState


class JobRetryAcceptedResponse(JobAcceptedResponse):
    """Representation returned after an operator retry is committed."""


class JobSummaryResponse(BaseModel):
    """Bounded job representation used by the dashboard table."""

    model_config = ConfigDict(frozen=True)
    audio_uuid: UUID
    state: JobState
    stage: JobStage | None
    updated_at: datetime
    attempt_count: int
    consecutive_retry_count: int
    glpi_ticket_id: int | None


class JobListResponse(BaseModel):
    """One keyset page of dashboard jobs."""

    model_config = ConfigDict(frozen=True)
    items: tuple[JobSummaryResponse, ...]
    next_cursor: str | None


class JobDetailResponse(BaseModel):
    """Sanitized operational detail of one lifecycle job."""

    model_config = ConfigDict(frozen=True)
    audio_uuid: UUID
    state: JobState
    stage: JobStage | None
    audio_extension: AudioFileExtension
    audio_size_bytes: int
    glpi_ticket_id: int | None
    attempt_count: int
    consecutive_retry_count: int
    next_attempt_at: datetime | None
    last_error_scope: JobErrorScope | None
    last_error_category: str | None
    last_error_code: str | None
    created_at: datetime
    first_started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    retention_status: RetentionState | None
    audio_deleted_at: datetime | None


class JobEventResponse(BaseModel):
    """One sanitized event from a job history."""

    model_config = ConfigDict(frozen=True)
    event_id: int
    event_type: JobEventName
    occurred_at: datetime
    state: JobState
    stage: JobStage | None
    actor: JobEventActor
    error_scope: JobErrorScope | None
    error_category: str | None
    technical_code: str | None


class JobEventListResponse(BaseModel):
    """One keyset page of lifecycle events."""

    model_config = ConfigDict(frozen=True)
    items: tuple[JobEventResponse, ...]
    next_cursor: str | None


__all__ = [
    "AudioFileExtension",
    "JobAcceptedResponse",
    "JobDetailResponse",
    "JobErrorScope",
    "JobEventActor",
    "JobEventListResponse",
    "JobEventName",
    "JobEventResponse",
    "JobListResponse",
    "JobRetryAcceptedResponse",
    "JobStage",
    "JobState",
    "JobSummaryResponse",
    "RetentionState",
]
