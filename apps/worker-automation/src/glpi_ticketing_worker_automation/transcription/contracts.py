"""Stable interface and errors for transcription providers."""

from pathlib import Path
from typing import Protocol

from .models import (
    JobSnapshot,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionOperation,
    TranscriptionProvider,
)


class TranscriptionClientProtocol(Protocol):
    """Translate a provider-specific API into the worker's stable contract."""

    @property
    def provider(self) -> TranscriptionProvider:
        """Return the provider implemented by this client."""
        ...

    def submit(self, audio_path: Path) -> Submission:
        """Submit an audio file exactly once and return its remote job."""
        ...

    def get_job(self, job_uuid: JobUuid) -> JobSnapshot:
        """Fetch one snapshot of a previously submitted job."""
        ...


class TranscriptionClientError(Exception):
    """Base class for safe, provider-independent client failures."""


class TranscriptionTransportError(TranscriptionClientError):
    """Raised when no usable HTTP response was received."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
    ) -> None:
        self.provider = provider
        self.operation = operation
        super().__init__(
            f"{provider.value} transcription {operation.value} request failed"
        )


class TranscriptionHttpError(TranscriptionClientError):
    """Raised when the provider returns an unexpected HTTP status."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
        status_code: int,
        *,
        retryable: bool,
    ) -> None:
        self.provider = provider
        self.operation = operation
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(
            f"{provider.value} transcription {operation.value} "
            f"returned HTTP {status_code}"
        )


class TranscriptionProtocolError(TranscriptionClientError):
    """Raised when a response does not satisfy the provider contract."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
        reason: str,
    ) -> None:
        self.provider = provider
        self.operation = operation
        self.reason = reason
        super().__init__(
            f"{provider.value} transcription {operation.value} "
            f"response is invalid: {reason}"
        )


class TranscriptionSubmissionRejectedError(TranscriptionClientError):
    """Raised when a valid response explicitly rejects a submission."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        *,
        response_log: ResponseLogEntry,
    ) -> None:
        self.provider = provider
        self.response_log = response_log
        super().__init__(f"{provider.value} transcription submission rejected")
