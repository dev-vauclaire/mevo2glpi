"""Stateful orchestration of one audio through the complete worker pipeline."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from glpi_ticketing_common import audio_files

from ..glpi import (
    GlpiClientError,
    GlpiClientProtocol,
    GlpiDuplicateExternalIdError,
    GlpiTicketInput,
    GlpiTicketRejectedError,
    TicketId,
)
from ..transcription import (
    TranscriberProtocol,
    TranscriptionResult,
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)
from .models import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    ProcessingStage,
    RetryOutcome,
    RunOutcome,
    WorkerDirectories,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AwaitingTranscription:
    audio_path: Path
    audio_uuid: UUID


@dataclass(frozen=True, slots=True)
class _AwaitingTicket:
    audio_path: Path
    audio_uuid: UUID
    transcription: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AwaitingFinalization:
    audio_path: Path
    audio_uuid: UUID
    ticket_id: TicketId


@dataclass(frozen=True, slots=True)
class _AwaitingFailurePublication:
    audio_path: Path
    audio_uuid: UUID
    stage: ProcessingStage
    failure: FailureDetails
    occurred_at: datetime


type _ActiveWork = (
    _AwaitingTranscription
    | _AwaitingTicket
    | _AwaitingFinalization
    | _AwaitingFailurePublication
)


class AudioProcessor:
    """Advance at most one audio until an externally observable boundary.

    The instance deliberately retains one successful transcription in memory while
    GLPI is unavailable. It is intended for the POC's single-worker execution model
    and is not thread-safe.
    """

    def __init__(
        self,
        *,
        directories: WorkerDirectories,
        transcriber: TranscriberProtocol,
        glpi_client: GlpiClientProtocol,
        audio_url_for: Callable[[UUID], str],
        idle_wait_seconds: float = 1.0,
        transcription_retry_interval_seconds: float = 2.0,
        glpi_retry_interval_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(directories, WorkerDirectories):
            raise TypeError("directories must be WorkerDirectories")
        if not callable(getattr(transcriber, "transcribe", None)):
            raise TypeError("transcriber must expose transcribe()")
        if not callable(getattr(glpi_client, "ensure_ticket", None)):
            raise TypeError("glpi_client must expose ensure_ticket()")
        if not callable(audio_url_for):
            raise TypeError("audio_url_for must be callable")
        if now is not None and not callable(now):
            raise TypeError("now must be callable")

        self._directories = directories
        self._transcriber = transcriber
        self._glpi_client = glpi_client
        self._audio_url_for = audio_url_for
        self._idle_wait_seconds = _validate_positive_seconds(
            idle_wait_seconds,
            "idle_wait_seconds",
        )
        self._transcription_retry_interval_seconds = _validate_positive_seconds(
            transcription_retry_interval_seconds,
            "transcription_retry_interval_seconds",
        )
        self._glpi_retry_interval_seconds = _validate_positive_seconds(
            glpi_retry_interval_seconds,
            "glpi_retry_interval_seconds",
        )
        self._now = now or _utc_now
        self._active_work: _ActiveWork | None = None

    def run_once(self) -> RunOutcome:
        """Advance one audio and return its observable pipeline outcome."""
        if self._active_work is None:
            audio_path = claim_next_audio(
                pending_directory=self._directories.pending,
                processing_directory=self._directories.processing,
            )
            if audio_path is None:
                return IdleOutcome(wait_seconds=self._idle_wait_seconds)

            audio_uuid = audio_files.parse_audio_filename(audio_path.name).audio_uuid
            self._active_work = _AwaitingTranscription(audio_path, audio_uuid)
            LOGGER.info("audio processing started audio_uuid=%s", audio_uuid)

        while True:
            active_work = self._active_work
            if isinstance(active_work, _AwaitingTranscription):
                outcome = self._transcribe(active_work)
            elif isinstance(active_work, _AwaitingTicket):
                outcome = self._ensure_ticket(active_work)
            elif isinstance(active_work, _AwaitingFinalization):
                return self._finalize(active_work)
            elif isinstance(active_work, _AwaitingFailurePublication):
                return self._publish_failed(active_work)
            else:
                raise RuntimeError("worker active state is invalid")

            if outcome is not None:
                return outcome

    def _transcribe(
        self,
        active_work: _AwaitingTranscription,
    ) -> RunOutcome | None:
        try:
            result = self._transcriber.transcribe(active_work.audio_path)
        except TranscriptionRetryRequired as error:
            if error.audio_path != active_work.audio_path:
                return self._blocked(
                    active_work,
                    stage=ProcessingStage.TRANSCRIPTION,
                    category="protocol",
                    technical_code="retry_audio_mismatch",
                )
            return self._retry(
                active_work,
                stage=ProcessingStage.TRANSCRIPTION,
                category="unavailable",
                technical_code=error.code,
                retry_after_seconds=self._transcription_retry_interval_seconds,
            )
        except TranscriptionTerminalError as error:
            if error.audio_path != active_work.audio_path:
                return self._blocked(
                    active_work,
                    stage=ProcessingStage.TRANSCRIPTION,
                    category="protocol",
                    technical_code="terminal_audio_mismatch",
                )
            return self._schedule_failure(
                active_work,
                stage=ProcessingStage.TRANSCRIPTION,
                failure=FailureDetails(
                    category="terminal",
                    technical_code=error.code,
                ),
            )

        if not isinstance(result, TranscriptionResult):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TRANSCRIPTION,
                category="protocol",
                technical_code="invalid_result",
            )
        if (
            result.audio_path != active_work.audio_path
            or result.audio_uuid != active_work.audio_uuid
        ):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TRANSCRIPTION,
                category="protocol",
                technical_code="result_audio_mismatch",
            )
        if not _is_valid_transcription(result.transcription):
            return self._schedule_failure(
                active_work,
                stage=ProcessingStage.TRANSCRIPTION,
                failure=FailureDetails(
                    category="terminal",
                    technical_code="invalid_transcription",
                ),
            )

        self._active_work = _AwaitingTicket(
            audio_path=active_work.audio_path,
            audio_uuid=active_work.audio_uuid,
            transcription=result.transcription,
        )
        LOGGER.info(
            "transcription completed audio_uuid=%s",
            active_work.audio_uuid,
        )
        return None

    def _ensure_ticket(
        self,
        active_work: _AwaitingTicket,
    ) -> RunOutcome | None:
        try:
            audio_url = self._audio_url_for(active_work.audio_uuid)
        except (TypeError, ValueError):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TICKETING,
                category="configuration",
                technical_code="audio_url_generation_failed",
            )
        if not isinstance(audio_url, str):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TICKETING,
                category="configuration",
                technical_code="invalid_audio_url",
            )

        try:
            ticket_input = GlpiTicketInput(
                audio_uuid=active_work.audio_uuid,
                transcription=active_work.transcription,
                audio_url=audio_url,
            )
        except (TypeError, ValueError):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TICKETING,
                category="configuration",
                technical_code="invalid_audio_url",
            )

        try:
            ticket_id = self._glpi_client.ensure_ticket(ticket_input)
        except GlpiClientError as error:
            failure = FailureDetails(
                category=error.category.value,
                technical_code=error.technical_code,
            )
            if error.retryable or error.outcome_unknown:
                return self._retry(
                    active_work,
                    stage=ProcessingStage.TICKETING,
                    category=failure.category,
                    technical_code=failure.technical_code,
                    retry_after_seconds=self._glpi_retry_interval_seconds,
                )
            if _is_terminal_ticket_error(error):
                return self._schedule_failure(
                    active_work,
                    stage=ProcessingStage.TICKETING,
                    failure=failure,
                )
            return self._blocked(
                active_work,
                stage=ProcessingStage.TICKETING,
                category=failure.category,
                technical_code=failure.technical_code,
            )

        if (
            isinstance(ticket_id, bool)
            or not isinstance(ticket_id, int)
            or ticket_id <= 0
        ):
            return self._blocked(
                active_work,
                stage=ProcessingStage.TICKETING,
                category="protocol",
                technical_code="invalid_ticket_id",
            )

        self._active_work = _AwaitingFinalization(
            audio_path=active_work.audio_path,
            audio_uuid=active_work.audio_uuid,
            ticket_id=ticket_id,
        )
        LOGGER.info(
            "ticket ensured audio_uuid=%s ticket_id=%s",
            active_work.audio_uuid,
            ticket_id,
        )
        return None

    def _finalize(
        self,
        active_work: _AwaitingFinalization,
    ) -> RunOutcome:
        expected_completed_path = (
            self._directories.completed / active_work.audio_path.name
        )
        try:
            completed_path = audio_files.move_audio(
                active_work.audio_path,
                self._directories.completed,
            )
        except (OSError, ValueError):
            if _is_absent(active_work.audio_path) and _is_regular_file(
                expected_completed_path
            ):
                self._active_work = None
                LOGGER.warning(
                    "completed transition visible after sync error "
                    "audio_uuid=%s ticket_id=%s",
                    active_work.audio_uuid,
                    active_work.ticket_id,
                )
                return CompletedOutcome(
                    audio_uuid=active_work.audio_uuid,
                    completed_path=expected_completed_path,
                    ticket_id=active_work.ticket_id,
                )
            return self._blocked(
                active_work,
                stage=ProcessingStage.FINALIZATION,
                category="filesystem",
                technical_code="completed_publish_failed",
            )

        self._active_work = None
        LOGGER.info(
            "audio processing completed audio_uuid=%s ticket_id=%s",
            active_work.audio_uuid,
            active_work.ticket_id,
        )
        return CompletedOutcome(
            audio_uuid=active_work.audio_uuid,
            completed_path=completed_path,
            ticket_id=active_work.ticket_id,
        )

    def _schedule_failure(
        self,
        active_work: _ActiveWork,
        *,
        stage: ProcessingStage,
        failure: FailureDetails,
    ) -> RunOutcome:
        failure_publication = _AwaitingFailurePublication(
            audio_path=active_work.audio_path,
            audio_uuid=active_work.audio_uuid,
            stage=stage,
            failure=failure,
            occurred_at=self._current_time(),
        )
        self._active_work = failure_publication
        return self._publish_failed(failure_publication)

    def _publish_failed(
        self,
        active_work: _AwaitingFailurePublication,
    ) -> RunOutcome:
        sidecar_payload = _failure_sidecar_payload(active_work)
        try:
            transition = audio_files.move_to_failed(
                active_work.audio_path,
                self._directories.failed,
                sidecar_payload,
            )
        except (OSError, ValueError):
            reconciled_outcome = self._reconcile_failed_publication(active_work)
            if reconciled_outcome is not None:
                return reconciled_outcome
            return self._blocked(
                active_work,
                stage=active_work.stage,
                category="filesystem",
                technical_code="failed_publish_failed",
            )

        self._active_work = None
        LOGGER.error(
            "audio processing failed audio_uuid=%s stage=%s code=%s",
            active_work.audio_uuid,
            active_work.stage.value,
            active_work.failure.technical_code,
        )
        return FailedOutcome(
            audio_uuid=active_work.audio_uuid,
            failed_path=transition.audio_path,
            error_sidecar_path=transition.sidecar_path,
            stage=active_work.stage,
            failure=active_work.failure,
        )

    def _reconcile_failed_publication(
        self,
        active_work: _AwaitingFailurePublication,
    ) -> FailedOutcome | None:
        failed_path = self._directories.failed / active_work.audio_path.name
        error_sidecar_path = (
            self._directories.failed / f"{active_work.audio_uuid}.error.json"
        )
        if (
            not _is_absent(active_work.audio_path)
            or not _is_regular_file(failed_path)
            or not _is_regular_file(error_sidecar_path)
            or not _sidecar_matches(
                error_sidecar_path,
                _failure_sidecar_payload(active_work),
            )
        ):
            return None

        self._active_work = None
        LOGGER.error(
            "failed transition visible after sync error audio_uuid=%s stage=%s code=%s",
            active_work.audio_uuid,
            active_work.stage.value,
            active_work.failure.technical_code,
        )
        return FailedOutcome(
            audio_uuid=active_work.audio_uuid,
            failed_path=failed_path,
            error_sidecar_path=error_sidecar_path,
            stage=active_work.stage,
            failure=active_work.failure,
        )

    def _retry(
        self,
        active_work: _ActiveWork,
        *,
        stage: ProcessingStage,
        category: str,
        technical_code: str,
        retry_after_seconds: float,
    ) -> RetryOutcome:
        LOGGER.warning(
            "audio processing retry requested audio_uuid=%s stage=%s code=%s",
            active_work.audio_uuid,
            stage.value,
            technical_code,
        )
        return RetryOutcome(
            audio_uuid=active_work.audio_uuid,
            processing_path=active_work.audio_path,
            stage=stage,
            failure=FailureDetails(
                category=category,
                technical_code=technical_code,
            ),
            retry_after_seconds=retry_after_seconds,
        )

    def _blocked(
        self,
        active_work: _ActiveWork,
        *,
        stage: ProcessingStage,
        category: str,
        technical_code: str,
    ) -> BlockedOutcome:
        LOGGER.error(
            "audio processing blocked audio_uuid=%s stage=%s code=%s",
            active_work.audio_uuid,
            stage.value,
            technical_code,
        )
        processing_path = (
            active_work.audio_path if not _is_absent(active_work.audio_path) else None
        )
        return BlockedOutcome(
            audio_uuid=active_work.audio_uuid,
            processing_path=processing_path,
            stage=stage,
            failure=FailureDetails(
                category=category,
                technical_code=technical_code,
            ),
            ticket_id=(
                active_work.ticket_id
                if isinstance(active_work, _AwaitingFinalization)
                else None
            ),
            inspection_paths=self._inspection_paths(active_work),
        )

    def _inspection_paths(self, active_work: _ActiveWork) -> tuple[Path, ...]:
        if isinstance(active_work, _AwaitingFailurePublication):
            return (
                self._directories.failed / active_work.audio_path.name,
                self._directories.failed / f"{active_work.audio_uuid}.error.json",
            )
        if isinstance(active_work, _AwaitingFinalization):
            return (self._directories.completed / active_work.audio_path.name,)
        if _is_absent(active_work.audio_path):
            return (active_work.audio_path,)
        return ()

    def _current_time(self) -> datetime:
        current_time = self._now()
        if not isinstance(current_time, datetime):
            raise TypeError("now must return a datetime")
        if current_time.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return current_time


def claim_next_audio(
    *,
    pending_directory: Path,
    processing_directory: Path,
) -> Path | None:
    """Return existing processing work or claim the next pending audio."""
    processing_files = audio_files.list_ready_audio_files(processing_directory)
    if processing_files:
        return processing_files[0]

    pending_files = audio_files.list_ready_audio_files(pending_directory)
    if not pending_files:
        return None

    pending_path = pending_files[0]
    expected_processing_path = processing_directory / pending_path.name
    try:
        return audio_files.move_audio(pending_path, processing_directory)
    except (OSError, ValueError):
        if _is_absent(pending_path) and _is_regular_file(expected_processing_path):
            LOGGER.warning(
                "claimed audio visible after sync error audio_uuid=%s",
                audio_files.parse_audio_filename(pending_path.name).audio_uuid,
            )
            return expected_processing_path
        raise


def _validate_positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized_value = float(value)
    if not math.isfinite(normalized_value) or normalized_value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized_value


def _is_valid_transcription(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(
            (ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127
            for character in value
        )
    )


def _is_terminal_ticket_error(error: GlpiClientError) -> bool:
    """Return whether a GLPI failure is explicitly scoped to one ticket input."""
    return isinstance(
        error,
        GlpiTicketRejectedError | GlpiDuplicateExternalIdError,
    )


def _failure_sidecar_payload(
    active_work: _AwaitingFailurePublication,
) -> dict[str, object]:
    return {
        "uuid": str(active_work.audio_uuid),
        "step": active_work.stage.value,
        "category": active_work.failure.category,
        "code": active_work.failure.technical_code,
        "timestamp": _format_utc(active_work.occurred_at),
    }


def _sidecar_matches(path: Path, expected_payload: dict[str, object]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload == expected_payload


def _is_absent(path: Path) -> bool:
    try:
        return not path.exists() and not path.is_symlink()
    except OSError:
        return False


def _is_regular_file(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
