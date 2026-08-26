"""In-memory orchestration of asynchronous transcription jobs."""

import math
import time
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
)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_JOB_TIMEOUT_SECONDS = 15 * 60.0


@dataclass(slots=True)
class _TranscriptionAttempt:
    """Ephemeral state for one submission and its polling loop."""

    audio_uuid: UUID
    deadline_at: datetime
    job_uuid: JobUuid | None = None


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
        """Submit one audio, leaving it in processing when a retry is required."""
        audio_path = _validate_audio_path(audio_path)
        audio_name = audio_files.parse_audio_filename(audio_path.name)
        started_at = self._current_time()
        attempt = _TranscriptionAttempt(
            audio_uuid=audio_name.audio_uuid,
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
        except TranscriptionSubmissionRejectedError:
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_rejected",
            )
        except TranscriptionTransportError:
            raise TranscriptionRetryRequired(
                "submission_unavailable",
                audio_path,
            ) from None
        except TranscriptionHttpError as error:
            if error.retryable or _is_server_error(error.status_code):
                raise TranscriptionRetryRequired(
                    "submission_unavailable",
                    audio_path,
                ) from None
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_http_error",
            )
        except TranscriptionProtocolError:
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_protocol_error",
            )
        except TranscriptionClientError:
            self._mark_failed(
                audio_path,
                attempt,
                code="submission_failed",
            )

        attempt.job_uuid = submission.job_uuid

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
                raise TranscriptionRetryRequired(
                    "polling_deadline_exceeded",
                    audio_path,
                )

            try:
                snapshot = self._client.get_job(job_uuid)
            except TranscriptionTransportError:
                raise TranscriptionRetryRequired(
                    "polling_unavailable",
                    audio_path,
                ) from None
            except TranscriptionHttpError as error:
                if error.retryable or _is_server_error(error.status_code):
                    raise TranscriptionRetryRequired(
                        "polling_unavailable",
                        audio_path,
                    ) from None
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_http_error",
                )
            except TranscriptionProtocolError:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_protocol_error",
                )
            except TranscriptionClientError:
                self._mark_failed(
                    audio_path,
                    attempt,
                    code="polling_failed",
                )

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
    ) -> Never:
        payload: dict[str, object] = {
            "uuid": str(attempt.audio_uuid),
            "step": "transcription",
            "category": "terminal",
            "code": code,
            "timestamp": _format_utc(self._current_time()),
        }

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


def _is_server_error(status_code: int) -> bool:
    return 500 <= status_code < 600


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
