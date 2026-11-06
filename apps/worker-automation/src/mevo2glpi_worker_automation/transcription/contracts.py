"""Stable interface and errors for transcription providers."""

from pathlib import Path
from typing import Protocol

from .models import (
    JobSnapshot,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionErrorCategory,
    TranscriptionOperation,
    TranscriptionProvider,
    TranscriptionResult,
)


class LegacyTranscriptionClientProtocol(Protocol):
    """Translate the historical polling API into normalized job snapshots."""

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


class TranscriberProtocol(Protocol):
    """Complete one provider-independent transcription attempt for an audio."""

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Return a result or raise a sanitized retry, terminal or blocked error."""
        ...


class TranscriptionTerminalError(Exception):
    """Report a terminal transcription failure without modifying the audio."""

    def __init__(self, code: str, audio_path: Path) -> None:
        self.code = code
        self.audio_path = audio_path
        super().__init__(f"transcription terminal error with code {code}")


class TranscriptionRetryRequired(Exception):
    """Request a fresh submission while leaving the source audio unchanged."""

    def __init__(self, code: str, audio_path: Path) -> None:
        self.code = code
        self.audio_path = audio_path
        super().__init__(f"transcription retry required with code {code}")


class TranscriptionSubmissionCallbackError(Exception):
    """Report that the caller could not record an accepted provider job.

    The callback's original exception is deliberately excluded so that a
    database driver message or another implementation detail cannot leak
    through this provider-independent boundary.
    """

    def __init__(self, audio_path: Path) -> None:
        self.audio_path = audio_path
        super().__init__("transcription submission callback failed")


class TranscriptionBlockedError(Exception):
    """Report a systemic failure for which failing one audio would be unsafe."""

    def __init__(
        self,
        *,
        category: TranscriptionErrorCategory,
        code: str,
        audio_path: Path,
        automatic_probe: bool = True,
    ) -> None:
        if not isinstance(automatic_probe, bool):
            raise TypeError("automatic_probe must be a boolean")
        self.category = category
        self.code = code
        self.audio_path = audio_path
        self.automatic_probe = automatic_probe
        super().__init__(
            f"transcription blocked with category {category.value} and code {code}"
        )


class TranscriptionClientError(Exception):
    """Base class for safe, provider-independent client failures."""

    def __init__(
        self,
        *,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
        category: TranscriptionErrorCategory,
        technical_code: str,
        retryable: bool,
        message: str,
    ) -> None:
        self.provider = provider
        self.operation = operation
        self.category = category
        self.technical_code = technical_code
        self.retryable = retryable
        super().__init__(message)


class TranscriptionTransportError(TranscriptionClientError):
    """Raised when no usable HTTP response was received."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
    ) -> None:
        super().__init__(
            provider=provider,
            operation=operation,
            category=TranscriptionErrorCategory.UNAVAILABLE,
            technical_code="transport_error",
            retryable=True,
            message=(
                f"{provider.value} transcription {operation.value} request failed"
            ),
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
        self.status_code = status_code
        super().__init__(
            provider=provider,
            operation=operation,
            category=_http_error_category(status_code, retryable=retryable),
            technical_code=f"http_{status_code}",
            retryable=retryable,
            message=(
                f"{provider.value} transcription {operation.value} "
                f"returned HTTP {status_code}"
            ),
        )


class TranscriptionProtocolError(TranscriptionClientError):
    """Raised when a response does not satisfy the provider contract."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        operation: TranscriptionOperation,
        reason: str,
    ) -> None:
        self.reason = reason
        super().__init__(
            provider=provider,
            operation=operation,
            category=TranscriptionErrorCategory.PROTOCOL,
            technical_code="protocol_error",
            retryable=False,
            message=(
                f"{provider.value} transcription {operation.value} "
                f"response is invalid: {reason}"
            ),
        )


class TranscriptionSubmissionRejectedError(TranscriptionClientError):
    """Raised when a valid response explicitly rejects a submission."""

    def __init__(
        self,
        provider: TranscriptionProvider,
        *,
        response_log: ResponseLogEntry,
    ) -> None:
        self.response_log = response_log
        super().__init__(
            provider=provider,
            operation=TranscriptionOperation.SUBMIT,
            category=TranscriptionErrorCategory.INVALID_REQUEST,
            technical_code="submission_rejected",
            retryable=False,
            message=f"{provider.value} transcription submission rejected",
        )


def _http_error_category(
    status_code: int,
    *,
    retryable: bool,
) -> TranscriptionErrorCategory:
    if retryable:
        return TranscriptionErrorCategory.UNAVAILABLE
    if status_code == 401:
        return TranscriptionErrorCategory.AUTHENTICATION
    if status_code == 403:
        return TranscriptionErrorCategory.AUTHORIZATION
    return TranscriptionErrorCategory.INVALID_REQUEST
