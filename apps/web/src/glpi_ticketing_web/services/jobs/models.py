"""HTTP-independent values and errors exposed by job use cases."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO
from uuid import UUID

from glpi_ticketing_database import (
    AudioExtension,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    RetentionStatus,
)


class UploadTooLargeError(ValueError):
    """Report an upload exceeding the configured byte limit."""


class UnsupportedAudioUploadError(ValueError):
    """Report an upload filename whose extension is not supported."""


class EmptyAudioError(ValueError):
    """Report an empty upload, which cannot become a lifecycle job."""


class AudioStorageUnavailableError(RuntimeError):
    """Report a sanitized failure to publish or clean audio storage."""


class AudioStorageConflictError(RuntimeError):
    """Report that a generated canonical storage destination already exists."""


class AudioStorageInvariantError(RuntimeError):
    """Report that persisted metadata and canonical storage are inconsistent."""


class AudioPurgedError(LookupError):
    """Report an audio whose retention lifecycle no longer permits access."""


class JobPersistenceUnavailableError(RuntimeError):
    """Report a sanitized failure at an application persistence boundary."""

    def __init__(self, technical_code: str) -> None:
        super().__init__("Job persistence is unavailable")
        self.technical_code = technical_code


class JobQueryUnavailableError(RuntimeError):
    """Report a sanitized failure to read lifecycle state."""


class InvalidCursorError(ValueError):
    """Report a malformed or incompatible opaque pagination cursor."""


class JobRetryConflictError(RuntimeError):
    """Report that a job cannot safely be rearmed by an operator."""


@dataclass(frozen=True, slots=True)
class SubmittedJob:
    """Stable result returned after accepting a new audio job."""

    audio_uuid: UUID
    state: JobStatus = JobStatus.PENDING


@dataclass(frozen=True, slots=True)
class RetriedJob:
    """Stable result returned after an atomic operator retry."""

    audio_uuid: UUID
    state: JobStatus


@dataclass(frozen=True, slots=True)
class DownloadableAudio:
    """Validated audio descriptor whose stream stays open until consumption."""

    uuid: UUID
    extension: AudioExtension
    size_bytes: int
    _stream: BinaryIO = field(repr=False, compare=False)

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield the validated audio and always close its descriptor."""
        try:
            while chunk := self._stream.read(64 * 1024):
                yield chunk
        finally:
            self._stream.close()

    def close(self) -> None:
        """Close an audio that could not be handed to the response iterator."""
        self._stream.close()


@dataclass(frozen=True, slots=True, kw_only=True)
class JobView:
    """Sanitized lifecycle state suitable for the web boundary."""

    audio_uuid: UUID
    state: JobStatus
    stage: JobStage | None
    audio_extension: AudioExtension
    audio_size_bytes: int
    glpi_ticket_id: int | None
    attempt_count: int
    consecutive_retry_count: int
    next_attempt_at: datetime | None
    last_error_scope: ErrorScope | None
    last_error_category: str | None
    last_error_code: str | None
    created_at: datetime
    first_started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    retention_status: RetentionStatus | None = None
    audio_deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobListPage:
    """One sanitized keyset page of jobs."""

    items: tuple[JobView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEventView:
    """One sanitized, append-only lifecycle event."""

    event_id: int
    event_type: JobEventType
    occurred_at: datetime
    state: JobStatus
    stage: JobStage | None
    actor: JobEventActor
    error_scope: ErrorScope | None
    error_category: str | None
    technical_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEventListPage:
    """One sanitized keyset page of a job's history."""

    items: tuple[JobEventView, ...]
    next_cursor: str | None


__all__ = [
    "AudioPurgedError",
    "AudioStorageConflictError",
    "AudioStorageInvariantError",
    "AudioStorageUnavailableError",
    "DownloadableAudio",
    "EmptyAudioError",
    "InvalidCursorError",
    "JobEventListPage",
    "JobEventView",
    "JobListPage",
    "JobPersistenceUnavailableError",
    "JobQueryUnavailableError",
    "JobRetryConflictError",
    "JobView",
    "RetriedJob",
    "SubmittedJob",
    "UnsupportedAudioUploadError",
    "UploadTooLargeError",
]
