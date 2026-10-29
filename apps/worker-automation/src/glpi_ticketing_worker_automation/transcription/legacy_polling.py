"""Adapter hiding the historical provider's asynchronous polling workflow."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never
from uuid import UUID

from glpi_ticketing_common import audio_files

from .contracts import (
    LegacyTranscriptionClientProtocol,
    TranscriptionBlockedError,
    TranscriptionClientError,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionRetryRequired,
    TranscriptionSubmissionCallbackError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTerminalError,
    TranscriptionTransportError,
)
from .models import (
    JobStatus,
    JobUuid,
    TranscriptionErrorCategory,
    TranscriptionResult,
)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_JOB_TIMEOUT_SECONDS = 15 * 60.0


def _never_stop() -> bool:
    return False


@dataclass(slots=True)
class _TranscriptionAttempt:
    """Ephemeral state for one submission and its polling loop."""

    audio_uuid: UUID
    deadline_at: datetime
    job_uuid: JobUuid | None = None


class LegacyPollingTranscriber:
    """Submit a new job for each call, then poll it until a terminal state.

    ``on_job_submitted`` runs synchronously after the accepted POST and before
    the first poll. A persistence callback must therefore own and close its
    short transaction before returning.
    """

    def __init__(
        self,
        *,
        client: LegacyTranscriptionClientProtocol,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] = _never_stop,
        on_job_submitted: Callable[[UUID, JobUuid], None] | None = None,
    ) -> None:
        self._client = client
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
        if not callable(should_stop):
            raise TypeError("should_stop must be callable")
        self._should_stop = should_stop
        if on_job_submitted is not None and not callable(on_job_submitted):
            raise TypeError("on_job_submitted must be callable")
        self._on_job_submitted = on_job_submitted

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Submit one audio without modifying it when a retry is required."""
        audio_path = _validate_audio_path(audio_path)
        audio_name = audio_files.parse_audio_filename(audio_path.name)
        started_at = self._current_time()
        attempt = _TranscriptionAttempt(
            audio_uuid=audio_name.audio_uuid,
            deadline_at=started_at + timedelta(seconds=self._job_timeout_seconds),
        )
        self._raise_if_shutdown_requested(audio_path)
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
            self._raise_terminal(
                audio_path,
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
            self._raise_blocked(
                audio_path,
                category=error.category,
                code=error.technical_code,
            )
        except TranscriptionProtocolError:
            self._raise_blocked(
                audio_path,
                category=TranscriptionErrorCategory.PROTOCOL,
                code="submission_protocol_error",
                automatic_probe=False,
            )
        except TranscriptionClientError as error:
            if error.retryable:
                raise TranscriptionRetryRequired(
                    "submission_unavailable",
                    audio_path,
                ) from None
            self._raise_blocked(
                audio_path,
                category=error.category,
                code=error.technical_code,
            )

        attempt.job_uuid = submission.job_uuid
        self._notify_job_submitted(attempt, audio_path)

    def _notify_job_submitted(
        self,
        attempt: _TranscriptionAttempt,
        audio_path: Path,
    ) -> None:
        callback = self._on_job_submitted
        if callback is None:
            return
        if attempt.job_uuid is None:
            raise TranscriptionBlockedError(
                category=TranscriptionErrorCategory.PROTOCOL,
                code="missing_job_uuid",
                audio_path=audio_path,
                automatic_probe=False,
            )

        callback_failed = False
        try:
            callback(attempt.audio_uuid, attempt.job_uuid)
        except Exception:
            # Raise outside the handler so the original, potentially sensitive
            # exception is not retained as ``__context__``.
            callback_failed = True
        if callback_failed:
            raise TranscriptionSubmissionCallbackError(audio_path)

    def _poll(
        self,
        audio_path: Path,
        attempt: _TranscriptionAttempt,
    ) -> TranscriptionResult:
        if attempt.job_uuid is None:
            raise TranscriptionBlockedError(
                category=TranscriptionErrorCategory.PROTOCOL,
                code="missing_job_uuid",
                audio_path=audio_path,
                automatic_probe=False,
            )
        job_uuid = attempt.job_uuid

        while True:
            self._raise_if_shutdown_requested(audio_path)
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
                self._raise_blocked(
                    audio_path,
                    category=error.category,
                    code=error.technical_code,
                    automatic_probe=False,
                )
            except TranscriptionProtocolError:
                self._raise_blocked(
                    audio_path,
                    category=TranscriptionErrorCategory.PROTOCOL,
                    code="polling_protocol_error",
                    automatic_probe=False,
                )
            except TranscriptionClientError as error:
                if error.retryable:
                    raise TranscriptionRetryRequired(
                        "polling_unavailable",
                        audio_path,
                    ) from None
                self._raise_blocked(
                    audio_path,
                    category=error.category,
                    code=error.technical_code,
                    automatic_probe=False,
                )

            self._raise_if_shutdown_requested(audio_path)

            if snapshot.job_uuid != job_uuid:
                self._raise_blocked(
                    audio_path,
                    category=TranscriptionErrorCategory.PROTOCOL,
                    code="polling_job_mismatch",
                    automatic_probe=False,
                )

            if snapshot.status is JobStatus.COMPLETED:
                if snapshot.transcription is None:
                    self._raise_blocked(
                        audio_path,
                        category=TranscriptionErrorCategory.PROTOCOL,
                        code="missing_transcription",
                        automatic_probe=False,
                    )
                return TranscriptionResult(
                    audio_path=audio_path,
                    audio_uuid=attempt.audio_uuid,
                    transcription=snapshot.transcription,
                )
            if snapshot.status is JobStatus.FAILED:
                self._raise_terminal(
                    audio_path,
                    code="remote_job_failed",
                )

            self._wait_before_retry(audio_path, attempt.deadline_at)

    def _wait_before_retry(self, audio_path: Path, deadline_at: datetime) -> None:
        remaining_seconds = (deadline_at - self._current_time()).total_seconds()
        if remaining_seconds <= 0:
            return
        self._sleep(min(self._poll_interval_seconds, remaining_seconds))
        self._raise_if_shutdown_requested(audio_path)

    def _raise_if_shutdown_requested(self, audio_path: Path) -> None:
        if self._should_stop():
            raise TranscriptionRetryRequired("shutdown_requested", audio_path)

    def _raise_terminal(
        self,
        audio_path: Path,
        *,
        code: str,
    ) -> Never:
        raise TranscriptionTerminalError(code, audio_path)

    def _raise_blocked(
        self,
        audio_path: Path,
        *,
        category: TranscriptionErrorCategory,
        code: str,
        automatic_probe: bool = True,
    ) -> Never:
        raise TranscriptionBlockedError(
            category=category,
            code=code,
            audio_path=audio_path,
            automatic_probe=automatic_probe,
        )

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
