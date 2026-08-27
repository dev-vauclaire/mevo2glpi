"""Provider-independent transcription contracts."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NewType
from uuid import UUID

JobUuid = NewType("JobUuid", str)


class TranscriptionProvider(StrEnum):
    """Transcription backend used by the worker."""

    LEGACY = "legacy"


class JobStatus(StrEnum):
    """Provider-independent lifecycle of a transcription job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TranscriptionOperation(StrEnum):
    """Operation performed against a transcription provider."""

    SUBMIT = "submit"
    GET_JOB = "get_job"
    UNKNOWN = "unknown"


class TranscriptionErrorCategory(StrEnum):
    """Sanitized classification shared by client and orchestration errors."""

    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    PROTOCOL = "protocol"


@dataclass(frozen=True, slots=True)
class ResponseLogEntry:
    """Small sanitized summary of one provider response."""

    operation: TranscriptionOperation
    http_status: int | None
    provider_status: str | None
    job_status: str | None


@dataclass(frozen=True, slots=True)
class Submission:
    """A job accepted by a transcription provider."""

    job_uuid: JobUuid
    response_log: ResponseLogEntry

    def __post_init__(self) -> None:
        if not isinstance(self.job_uuid, str) or not self.job_uuid:
            raise ValueError("job_uuid must be a non-empty string")


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """Normalized result of polling a transcription job once."""

    job_uuid: JobUuid
    status: JobStatus
    transcription: str | None = field(repr=False)
    response_log: ResponseLogEntry

    def __post_init__(self) -> None:
        if not isinstance(self.job_uuid, str) or not self.job_uuid:
            raise ValueError("job_uuid must be a non-empty string")
        if self.status is JobStatus.COMPLETED and self.transcription is None:
            raise ValueError("a completed job must contain a transcription")
        if self.status is not JobStatus.COMPLETED and self.transcription is not None:
            raise ValueError("only a completed job may contain a transcription")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Successful transcription associated with its processing audio."""

    audio_path: Path
    audio_uuid: UUID
    transcription: str = field(repr=False)
