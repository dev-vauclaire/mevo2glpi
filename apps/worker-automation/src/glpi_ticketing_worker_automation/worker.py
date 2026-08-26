"""Polling worker that claims and transcribes one audio at a time."""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Never

from glpi_ticketing_common import audio_files

from .transcription import (
    TranscriptionClientProtocol,
    TranscriptionPoller,
    TranscriptionProcessingFailed,
    TranscriptionResult,
    TranscriptionRetryRequired,
)
from .transcription.poller import (
    DEFAULT_JOB_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_SCAN_INTERVAL_SECONDS = 1.0


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

    return audio_files.move_audio(pending_files[0], processing_directory)


def run_once(
    *,
    pending_directory: Path,
    processing_directory: Path,
    failed_directory: Path,
    transcription_client: TranscriptionClientProtocol,
    transcription_poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    transcription_job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> TranscriptionResult | None:
    """Claim and transcribe one audio, retrying temporary unavailability."""
    audio_path = claim_next_audio(
        pending_directory=pending_directory,
        processing_directory=processing_directory,
    )
    if audio_path is None:
        return None

    audio_uuid = audio_files.parse_audio_filename(audio_path.name).audio_uuid
    transcriber = TranscriptionPoller(
        client=transcription_client,
        failed_directory=failed_directory,
        poll_interval_seconds=transcription_poll_interval_seconds,
        job_timeout_seconds=transcription_job_timeout_seconds,
        sleep=sleep,
        now=now,
    )
    LOGGER.info("transcription started audio_uuid=%s", audio_uuid)

    while True:
        try:
            result = transcriber.transcribe(audio_path)
        except TranscriptionRetryRequired as error:
            LOGGER.warning(
                "transcription unavailable; retrying audio_uuid=%s code=%s",
                audio_uuid,
                error.code,
            )
            sleep(transcription_poll_interval_seconds)
            continue
        except TranscriptionProcessingFailed as error:
            LOGGER.error(
                "transcription failed audio_uuid=%s code=%s",
                audio_uuid,
                error.code,
            )
            raise

        LOGGER.info("transcription completed audio_uuid=%s", result.audio_uuid)
        return result


def run_forever(
    *,
    pending_directory: Path,
    processing_directory: Path,
    failed_directory: Path,
    transcription_client: TranscriptionClientProtocol,
    handle_transcription: Callable[[TranscriptionResult], None],
    poll_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
    transcription_poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    transcription_job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> Never:
    """Process forever and hand each transcription to its next pipeline stage.

    The result handler must advance the successful audio lifecycle before returning,
    because the audio deliberately remains in ``processing`` after transcription.
    """
    while True:
        try:
            result = run_once(
                pending_directory=pending_directory,
                processing_directory=processing_directory,
                failed_directory=failed_directory,
                transcription_client=transcription_client,
                transcription_poll_interval_seconds=transcription_poll_interval_seconds,
                transcription_job_timeout_seconds=transcription_job_timeout_seconds,
                sleep=sleep,
                now=now,
            )
        except TranscriptionProcessingFailed:
            continue

        if result is None:
            sleep(poll_interval_seconds)
            continue

        handle_transcription(result)
        if result.audio_path.exists():
            raise RuntimeError(
                "the transcription result handler must move the successful audio "
                "out of processing before returning"
            )
