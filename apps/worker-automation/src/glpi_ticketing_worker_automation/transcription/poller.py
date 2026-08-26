"""In-memory orchestration of asynchronous transcription jobs."""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never
from uuid import UUID

from glpi_ticketing_common import audio_files

from .contracts import (
    TranscriptionClientError,
    TranscriptionClientProtocol,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTransportError,
)
from .models import (
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    TranscriptionProvider,
)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_JOB_TIMEOUT_SECONDS = 15 * 60.0
MAX_RESPONSE_LOGS = 20


@dataclass(frozen=True, slots=True)
class _SanitizedResponseLog:
    """Allowlisted provider metadata suitable for an error report."""

    operation: str
    http_status: int | None
    provider_status: str | None
    job_status: str | None

    @classmethod
    def from_client_log(cls, entry: ResponseLogEntry) -> "_SanitizedResponseLog":
        """Remove unexpected status values before retaining response metadata."""
        operation = (
            entry.operation if entry.operation in {"submit", "get_job"} else "unknown"
        )
        provider_status = _allowlisted_status(
            entry.provider_status,
            allowed={"success", "error"},
            unknown="other",
        )
        job_status = _allowlisted_status(
            entry.job_status,
            allowed={"PENDING", "PROCESSING", "COMPLETED", "FAILED"},
            unknown="OTHER",
        )
        return cls(
            operation=operation,
            http_status=entry.http_status,
            provider_status=provider_status,
            job_status=job_status,
        )

    def as_payload(self) -> dict[str, object]:
        """Return the compact JSON object written only for terminal failures."""
        payload: dict[str, object] = {"operation": self.operation}
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.provider_status is not None:
            payload["provider_status"] = self.provider_status
        if self.job_status is not None:
            payload["job_status"] = self.job_status
        return payload


@dataclass(slots=True)
class _TranscriptionAttempt:
    """Ephemeral state for one submission and its polling loop."""

    audio_uuid: UUID
    provider: TranscriptionProvider
    deadline_at: datetime
    job_uuid: JobUuid | None = None
    response_logs: deque[_SanitizedResponseLog] = field(
        default_factory=lambda: deque(maxlen=MAX_RESPONSE_LOGS),
    )

    def record(self, log: ResponseLogEntry) -> None:
        """Retain a bounded, sanitized summary of one provider interaction."""
        self.response_logs.append(_SanitizedResponseLog.from_client_log(log))


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Successful transcription associated with its processing audio."""

    audio_path: Path
    audio_uuid: UUID
    transcription: str = field(repr=False)


class TranscriptionProcessingFailed(Exception):
    """Raised after an audio has been isolated in the failed directory."""

    def __init__(self, code: str, failed_audio_path: Path) -> None:
        self.code = code
        self.failed_audio_path = failed_audio_path
        super().__init__(f"transcription failed with code {code}")


class TranscriptionRetryRequired(Exception):
    """Raised while leaving an audio in processing for a fresh submission."""

    def __init__(self, code: str, audio_path: Path) -> None:
        self.code = code
        self.audio_path = audio_path
        super().__init__(f"transcription retry required with code {code}")


class TranscriptionPoller:
    """Submit a new job for each call, then poll it until a terminal state."""

    def __init__(
        self,
        *,
        client: TranscriptionClientProtocol,
        failed_directory: Path,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._failed_directory = Path(failed_directory)
        self._poll_interval_seconds = _validate_positive_seconds(
            poll_interval_seconds,
            "poll_interval_seconds",
        )
        self._job_timeout_seconds = _validate_positive_seconds(
            job_timeout_seconds,
            "job_timeout_seconds",
        )
        self._sleep = sleep
        self._now = now or _utc_now

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Submit one audio, leaving it in processing if result retrieval is ambiguous."""
        audio_path = _validate_audio_path(audio_path)
        audio_name = audio_files.parse_audio_filename(audio_path.name)
        started_at = self._current_time()
        attempt = _TranscriptionAttempt(
            audio_uuid=audio_name.audio_uuid,
            provider=self._client.provider,
            deadline_at=started_at + timedelta(seconds=self._job_timeout_seconds),
        )
        self._submit_once(audio_path, attempt)
        return self._poll(audio_path, attempt)

    def _submit_once(
        self,
        audio_path: Path,
        attempt: _TranscriptionAttempt,
    ) -> None:
        try:
            submission = self._client.submit(audio_path)
        except TranscriptionSubmissionRejectedError as error:
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_rejected",
                log=error.response_log,
            )
        except TranscriptionHttpError as error:
            code = (
                "ambiguous_submission"
                if error.retryable or error.status_code >= 500
                else "submission_http_error"
            )
            self._mark_failed(
                audio_path,
                attempt,
                code=code,
                log=_log_from_client_error(error),
            )
        except (TranscriptionTransportError, TranscriptionProtocolError) as error:
            self._mark_failed(
                audio_path,
                attempt,
                code="ambiguous_submission",
                log=_log_from_client_error(error),
            )
        except TranscriptionClientError as error:
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_failed",
                log=_log_from_client_error(error),
            )

        attempt.job_uuid = submission.job_uuid
        attempt.record(submission.response_log)

    def _poll(
        self,
        audio_path: Path,
        attempt: _TranscriptionAttempt,
    ) -> TranscriptionResult:
        if attempt.job_uuid is None:
            raise RuntimeError("transcription attempt does not contain a job_uuid")
        job_uuid = attempt.job_uuid

        while True:
            if self._current_time() >= attempt.deadline_at:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_deadline_exceeded",
                )

            try:
                snapshot = self._client.get_job(job_uuid)
            except TranscriptionTransportError:
                raise TranscriptionRetryRequired(
                    "ambiguous_polling_response",
                    audio_path,
                ) from None
            except TranscriptionHttpError as error:
                if error.retryable:
                    raise TranscriptionRetryRequired(
                        "ambiguous_polling_response",
                        audio_path,
                    ) from None
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_http_error",
                    log=_log_from_client_error(error),
                )
            except TranscriptionClientError as error:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_protocol_error",
                    log=_log_from_client_error(error),
                )

            attempt.record(snapshot.response_log)
            if snapshot.job_uuid != job_uuid:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_job_mismatch",
                )

            if snapshot.status is JobStatus.COMPLETED:
                if snapshot.transcription is None:
                    self._mark_failed(
                        audio_path,
                        attempt,
                        code="missing_transcription",
                    )
                return TranscriptionResult(
                    audio_path=audio_path,
                    audio_uuid=attempt.audio_uuid,
                    transcription=snapshot.transcription,
                )
            if snapshot.status is JobStatus.FAILED:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="remote_job_failed",
                )

            self._wait_before_retry(attempt.deadline_at)

    def _wait_before_retry(self, deadline_at: datetime) -> None:
        remaining_seconds = (deadline_at - self._current_time()).total_seconds()
        if remaining_seconds <= 0:
            return
        self._sleep(min(self._poll_interval_seconds, remaining_seconds))

    def _mark_failed(
        self,
        audio_path: Path,
        attempt: _TranscriptionAttempt,
        *,
        code: str,
        log: ResponseLogEntry | None = None,
    ) -> Never:
        if log is not None:
            attempt.record(log)
        payload: dict[str, object] = {
            "uuid": str(attempt.audio_uuid),
            "step": "transcription",
            "category": "terminal",
            "code": code,
            "timestamp": _format_utc(self._current_time()),
            "provider": attempt.provider.value,
            "response_logs": [log.as_payload() for log in attempt.response_logs],
        }
        if attempt.job_uuid is not None:
            payload["job_uuid"] = str(attempt.job_uuid)

        transition = audio_files.move_to_failed(
            audio_path,
            self._failed_directory,
            payload,
        )
        raise TranscriptionProcessingFailed(code, transition.audio_path)

    def _current_time(self) -> datetime:
        current_time = self._now()
        if current_time.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return current_time


def _validate_audio_path(audio_path: Path) -> Path:
    audio_path = Path(audio_path)
    audio_files.parse_audio_filename(audio_path.name)
    if audio_path.is_symlink():
        raise ValueError("audio_path must not be a symbolic link")
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    if not audio_path.is_file():
        raise ValueError("audio_path must be a regular file")
    return audio_path


def _validate_positive_seconds(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return value


def _log_from_client_error(error: TranscriptionClientError) -> ResponseLogEntry:
    status_code = error.status_code if isinstance(error, TranscriptionHttpError) else 0
    operation = getattr(error, "operation", "unknown")
    return ResponseLogEntry(
        operation=operation,
        http_status=status_code or None,
        provider_status=None,
        job_status=None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _allowlisted_status(
    value: str | None,
    *,
    allowed: set[str],
    unknown: str,
) -> str | None:
    if value is None:
        return None
    return value if value in allowed else unknown
