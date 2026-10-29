"""Provider-neutral transcription stage and error classification."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from glpi_ticketing_database import ErrorScope, InvalidJobTransitionError, JobStage

from ...transcription import (
    TranscriberProtocol,
    TranscriptionBlockedError,
    TranscriptionResult,
    TranscriptionRetryRequired,
    TranscriptionSubmissionCallbackError,
    TranscriptionTerminalError,
)
from ..outcomes import (
    FailureDetails,
    InterruptedOutcome,
    RunOutcome,
)
from ..ports import JobLifecycleProtocol, JobSnapshotProtocol
from ..transitions import JobTransitionService

LOGGER = logging.getLogger(__name__)
_SAFE_TECHNICAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FALLBACK_TRANSCRIPTION_TEMPLATE = (
    "La transcription automatique de ce message audio n'a pas pu être produite. "
    "L'audio reste disponible dans ce ticket pour écoute par un opérateur. "
    "Code technique : {technical_code}."
)


@dataclass(frozen=True, slots=True)
class TranscriptionSuccess:
    """In-memory content passed to ticketing after a completed stage."""

    transcription: str = field(repr=False)
    degraded: bool = False
    failure_code: str | None = None


type TranscriptionStageResult = TranscriptionSuccess | RunOutcome


class TranscriptionStage:
    """Call Transcribe AI and persist the provider success boundary."""

    def __init__(
        self,
        *,
        transcriber: TranscriberProtocol,
        lifecycle: JobLifecycleProtocol,
        transitions: JobTransitionService,
        ownership_guard: Callable[[], None] | None = None,
    ) -> None:
        if not callable(getattr(transcriber, "transcribe", None)):
            raise TypeError("transcriber must expose transcribe()")
        if not callable(getattr(lifecycle, "record_transcription_succeeded", None)):
            raise TypeError("lifecycle must expose record_transcription_succeeded()")
        if not callable(getattr(lifecycle, "record_transcription_failed", None)):
            raise TypeError("lifecycle must expose record_transcription_failed()")
        if not isinstance(transitions, JobTransitionService):
            raise TypeError("transitions must be a JobTransitionService")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._transcriber = transcriber
        self._lifecycle = lifecycle
        self._transitions = transitions
        self._ownership_guard = ownership_guard or _no_op

    def execute(
        self,
        job: JobSnapshotProtocol,
        audio_path: Path,
    ) -> TranscriptionStageResult:
        """Return a valid in-memory transcription or an observable boundary."""

        audio_uuid = job.uuid
        try:
            result = self._guarded_external_call(
                lambda: self._transcriber.transcribe(audio_path)
            )
        except TranscriptionSubmissionCallbackError as error:
            technical_code = "submission_persistence_failed"
            if error.audio_path != audio_path:
                technical_code = "callback_audio_mismatch"
            return self._system_block(
                job,
                category="persistence",
                technical_code=technical_code,
                automatic_probe=False,
            )
        except TranscriptionRetryRequired as error:
            if error.audio_path != audio_path:
                return self._system_block(
                    job,
                    category="protocol",
                    technical_code="retry_audio_mismatch",
                    automatic_probe=False,
                )
            if error.code == "shutdown_requested":
                LOGGER.info(
                    "audio processing interrupted audio_uuid=%s stage=%s",
                    audio_uuid,
                    JobStage.TRANSCRIPTION.value,
                )
                return InterruptedOutcome(
                    audio_uuid=audio_uuid,
                    stage=JobStage.TRANSCRIPTION,
                )
            return self._transitions.retry(
                job,
                stage=JobStage.TRANSCRIPTION,
                category="unavailable",
                technical_code=error.code,
            )
        except TranscriptionBlockedError as error:
            technical_code = error.code
            automatic_probe = error.automatic_probe
            if error.audio_path != audio_path:
                technical_code = "blocked_audio_mismatch"
                automatic_probe = False
            return self._system_block(
                job,
                category=error.category.value,
                technical_code=technical_code,
                automatic_probe=automatic_probe,
            )
        except TranscriptionTerminalError as error:
            if error.audio_path != audio_path:
                return self._system_block(
                    job,
                    category="protocol",
                    technical_code="terminal_audio_mismatch",
                    automatic_probe=False,
                )
            return self._continue_with_fallback(job, error.code)

        invalid_result = self._validate_result(
            result,
            job=job,
            audio_path=audio_path,
        )
        if invalid_result is not None:
            return invalid_result

        # The validation above narrows the provider contract at runtime.
        assert isinstance(result, TranscriptionResult)
        try:
            self._lifecycle.record_transcription_succeeded(
                audio_uuid,
                self._transitions.current_time(),
            )
        except InvalidJobTransitionError:
            return self._system_block(
                job,
                category="protocol",
                technical_code="transcription_transition_conflict",
                automatic_probe=False,
            )
        LOGGER.info("transcription completed audio_uuid=%s", audio_uuid)
        return TranscriptionSuccess(transcription=result.transcription)

    def _validate_result(
        self,
        result: object,
        *,
        job: JobSnapshotProtocol,
        audio_path: Path,
    ) -> TranscriptionSuccess | RunOutcome | None:
        if not isinstance(result, TranscriptionResult):
            return self._system_block(
                job,
                category="protocol",
                technical_code="invalid_result",
                automatic_probe=False,
            )
        if result.audio_path != audio_path or result.audio_uuid != job.uuid:
            return self._system_block(
                job,
                category="protocol",
                technical_code="result_audio_mismatch",
                automatic_probe=False,
            )
        if not _valid_transcription(result.transcription):
            return self._continue_with_fallback(job, "invalid_transcription")
        return None

    def _continue_with_fallback(
        self,
        job: JobSnapshotProtocol,
        technical_code: str,
    ) -> TranscriptionSuccess | RunOutcome:
        if _SAFE_TECHNICAL_CODE.fullmatch(technical_code) is None:
            return self._system_block(
                job,
                category="protocol",
                technical_code="invalid_terminal_code",
                automatic_probe=False,
            )
        try:
            self._lifecycle.record_transcription_failed(
                job.uuid,
                error_category="terminal",
                error_code=technical_code,
                now=self._transitions.current_time(),
            )
        except InvalidJobTransitionError:
            return self._system_block(
                job,
                category="protocol",
                technical_code="transcription_transition_conflict",
                automatic_probe=False,
            )
        LOGGER.warning(
            "transcription fallback selected audio_uuid=%s code=%s",
            job.uuid,
            technical_code,
        )
        return TranscriptionSuccess(
            transcription=_FALLBACK_TRANSCRIPTION_TEMPLATE.format(
                technical_code=technical_code
            ),
            degraded=True,
            failure_code=technical_code,
        )

    def _system_block(
        self,
        job: JobSnapshotProtocol,
        *,
        category: str,
        technical_code: str,
        automatic_probe: bool,
    ) -> RunOutcome:
        return self._transitions.block(
            job.uuid,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category=category,
                technical_code=technical_code,
            ),
            automatic_probe=automatic_probe,
        )

    def _guarded_external_call(self, operation: Callable[[], object]) -> object:
        self._ownership_guard()
        try:
            return operation()
        finally:
            # A lock loss supersedes the provider outcome: the old instance
            # must neither persist it nor start the next external boundary.
            self._ownership_guard()


def _valid_transcription(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(
            (ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127
            for character in value
        )
    )


def _no_op() -> None:
    return None


__all__ = [
    "TranscriptionStage",
    "TranscriptionStageResult",
    "TranscriptionSuccess",
]
