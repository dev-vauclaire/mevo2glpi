import json
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_common import audio_files
from glpi_ticketing_worker_automation.glpi import GlpiTicketInput, TicketId
from glpi_ticketing_worker_automation.transcription import (
    TranscriberProtocol,
    TranscriptionBlockedError,
    TranscriptionErrorCategory,
    TranscriptionResult,
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)
from glpi_ticketing_worker_automation.worker import (
    AudioProcessor,
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    ProcessingStage,
    RetryOutcome,
    WorkerDirectories,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"
SYNTHETIC_AUDIO = b"synthetic-audio"
SYNTHETIC_TRANSCRIPTION = "Coucou je suis une transcription synthétique"
SYNTHETIC_SECRET = "synthetic-api-key-that-must-never-be-persisted"
FIXED_NOW = datetime(2026, 8, 27, 10, 15, 30, tzinfo=UTC)


class SimulatedProcessCrash(BaseException):
    """Interrupt processing without executing a controlled transition."""


class ScriptedTranscriber:
    def __init__(self, events: list[TranscriptionResult | BaseException]) -> None:
        self._events = deque(events)
        self.audio_paths: list[Path] = []

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        self.audio_paths.append(audio_path)
        if not self._events:
            raise AssertionError("unexpected transcription call")
        event = self._events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event


class SuccessfulGlpiClient:
    def __init__(self) -> None:
        self.tickets: list[GlpiTicketInput] = []

    def ensure_ticket(self, ticket: GlpiTicketInput) -> TicketId:
        self.tickets.append(ticket)
        return TicketId(17)


class InvalidResultTranscriber:
    def transcribe(self, _audio_path: Path) -> TranscriptionResult:
        return None  # type: ignore[return-value]


@pytest.fixture
def audio_directories(tmp_path: Path) -> WorkerDirectories:
    directories = WorkerDirectories(
        pending=tmp_path / "pending",
        processing=tmp_path / "processing",
        completed=tmp_path / "completed",
        failed=tmp_path / "failed",
    )
    for directory in (
        directories.pending,
        directories.processing,
        directories.completed,
        directories.failed,
    ):
        directory.mkdir()
    return directories


def test_transcription_unavailable_returns_retry_and_keeps_processing(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = ScriptedTranscriber(
        [
            TranscriptionRetryRequired("submission_unavailable", audio_path),
            _transcription_result(audio_path),
        ]
    )
    processor = _processor(audio_directories, transcriber)

    first_outcome = processor.run_once()

    assert isinstance(first_outcome, RetryOutcome)
    assert first_outcome.stage is ProcessingStage.TRANSCRIPTION
    assert first_outcome.failure.technical_code == "submission_unavailable"
    assert first_outcome.processing_path == audio_path
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())

    second_outcome = processor.run_once()

    assert isinstance(second_outcome, CompletedOutcome)
    assert transcriber.audio_paths == [audio_path, audio_path]
    assert not list(audio_directories.failed.iterdir())


@pytest.mark.parametrize(
    ("category", "technical_code"),
    [
        (TranscriptionErrorCategory.AUTHENTICATION, "http_401"),
        (TranscriptionErrorCategory.AUTHORIZATION, "http_403"),
        (TranscriptionErrorCategory.INVALID_REQUEST, "http_422"),
        (TranscriptionErrorCategory.PROTOCOL, "polling_protocol_error"),
    ],
)
def test_systemic_transcription_error_returns_blocked_and_keeps_processing(
    audio_directories: WorkerDirectories,
    category: TranscriptionErrorCategory,
    technical_code: str,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [
                TranscriptionBlockedError(
                    category=category,
                    code=technical_code,
                    audio_path=audio_path,
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is ProcessingStage.TRANSCRIPTION
    assert outcome.failure.category == category.value
    assert outcome.failure.technical_code == technical_code
    assert outcome.processing_path == audio_path
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_terminal_transcription_returns_failed_outcome(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [TranscriptionTerminalError("remote_job_failed", audio_path)]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert outcome.stage is ProcessingStage.TRANSCRIPTION
    assert outcome.failure.category == "terminal"
    assert outcome.failure.technical_code == "remote_job_failed"
    assert not audio_path.exists()
    assert outcome.failed_path.read_bytes() == SYNTHETIC_AUDIO
    assert outcome.error_sidecar_path.is_file()


def test_terminal_transcription_removes_source_only_after_failed_is_complete(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    failed_audio = audio_directories.failed / AUDIO_FILENAME
    failed_sidecar = audio_directories.failed / f"{AUDIO_UUID}.error.json"
    real_unlink_source = audio_files._unlink_source
    deletion_observed = False

    def tracked_unlink_source(source: Path) -> None:
        nonlocal deletion_observed
        assert source == audio_path
        assert source.exists()
        assert failed_audio.read_bytes() == SYNTHETIC_AUDIO
        assert json.loads(failed_sidecar.read_text(encoding="utf-8"))["uuid"] == (
            str(AUDIO_UUID)
        )
        assert not list(audio_directories.failed.glob("*.part"))
        deletion_observed = True
        real_unlink_source(source)

    monkeypatch.setattr(audio_files, "_unlink_source", tracked_unlink_source)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [TranscriptionTerminalError("remote_job_failed", audio_path)]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert deletion_observed
    assert not audio_path.exists()


def test_failed_publication_retry_does_not_repeat_transcription(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = ScriptedTranscriber(
        [TranscriptionTerminalError("remote_job_failed", audio_path)]
    )
    real_move_to_failed = audio_files.move_to_failed
    publication_calls = 0

    def fail_once(*args, **kwargs):
        nonlocal publication_calls
        publication_calls += 1
        if publication_calls == 1:
            raise OSError("synthetic failed volume outage")
        return real_move_to_failed(*args, **kwargs)

    monkeypatch.setattr(audio_files, "move_to_failed", fail_once)
    processor = _processor(audio_directories, transcriber)

    first_outcome = processor.run_once()

    assert isinstance(first_outcome, BlockedOutcome)
    assert first_outcome.failure.technical_code == "failed_publish_failed"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert transcriber.audio_paths == [audio_path]

    second_outcome = processor.run_once()

    assert isinstance(second_outcome, FailedOutcome)
    assert second_outcome.failure.technical_code == "remote_job_failed"
    assert transcriber.audio_paths == [audio_path]
    assert publication_calls == 2
    assert not list(audio_directories.failed.glob("*.part"))


def test_failed_publication_is_reconciled_after_source_unlink_sync_error(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    transcriber = ScriptedTranscriber(
        [TranscriptionTerminalError("remote_job_failed", audio_path)]
    )

    def unlink_then_raise(source: Path) -> None:
        source.unlink()
        raise OSError("synthetic processing directory fsync failure")

    monkeypatch.setattr(audio_files, "_unlink_source", unlink_then_raise)
    processor = _processor(audio_directories, transcriber)

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert not audio_path.exists()
    assert outcome.failed_path.read_bytes() == SYNTHETIC_AUDIO
    assert outcome.error_sidecar_path.is_file()
    assert transcriber.audio_paths == [audio_path]


def test_ambiguous_failed_publication_exposes_real_inspection_paths(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    failed_audio = audio_directories.failed / AUDIO_FILENAME
    failed_sidecar = audio_directories.failed / f"{AUDIO_UUID}.error.json"
    transcriber = ScriptedTranscriber(
        [TranscriptionTerminalError("remote_job_failed", audio_path)]
    )
    real_unlink_source = audio_files._unlink_source

    def unlink_corrupt_sidecar_then_raise(source: Path) -> None:
        real_unlink_source(source)
        failed_sidecar.write_text("corrupted", encoding="utf-8")
        raise OSError("synthetic ambiguous failed publication")

    monkeypatch.setattr(
        audio_files,
        "_unlink_source",
        unlink_corrupt_sidecar_then_raise,
    )
    processor = _processor(audio_directories, transcriber)

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.processing_path is None
    assert outcome.inspection_paths == (failed_audio, failed_sidecar)
    assert not audio_path.exists()
    assert failed_audio.read_bytes() == SYNTHETIC_AUDIO
    assert failed_sidecar.read_text(encoding="utf-8") == "corrupted"
    assert transcriber.audio_paths == [audio_path]


def test_transcription_error_sidecar_is_strictly_sanitized(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [TranscriptionTerminalError("remote_job_failed", audio_path)]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    serialized_sidecar = outcome.error_sidecar_path.read_text(encoding="utf-8")
    payload = json.loads(serialized_sidecar)
    assert payload == {
        "uuid": str(AUDIO_UUID),
        "step": "transcription",
        "category": "terminal",
        "code": "remote_job_failed",
        "timestamp": "2026-08-27T10:15:30Z",
    }
    assert set(payload) == {"uuid", "step", "category", "code", "timestamp"}
    assert SYNTHETIC_TRANSCRIPTION not in serialized_sidecar
    assert SYNTHETIC_SECRET not in serialized_sidecar


def test_invalid_transcription_is_a_terminal_transcription_failure(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [
                TranscriptionResult(
                    audio_path=audio_path,
                    audio_uuid=AUDIO_UUID,
                    transcription=" \n\t ",
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert outcome.stage is ProcessingStage.TRANSCRIPTION
    assert outcome.failure.technical_code == "invalid_transcription"
    assert not audio_path.exists()


def test_transcription_result_mismatch_blocks_and_keeps_processing(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber(
            [
                TranscriptionResult(
                    audio_path=audio_path,
                    audio_uuid=UUID("88ac95f5-e207-4aa4-a824-2bd4a3269814"),
                    transcription=SYNTHETIC_TRANSCRIPTION,
                )
            ]
        ),
    )

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.category == "protocol"
    assert outcome.failure.technical_code == "result_audio_mismatch"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_invalid_transcription_result_blocks_and_keeps_processing(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(audio_directories, InvalidResultTranscriber())

    outcome = processor.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.category == "protocol"
    assert outcome.failure.technical_code == "invalid_result"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_processor_logs_uuid_but_not_transcription(
    audio_directories: WorkerDirectories,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    processor = _processor(
        audio_directories,
        ScriptedTranscriber([_transcription_result(audio_path)]),
    )

    with caplog.at_level(
        logging.INFO,
        logger="glpi_ticketing_worker_automation.worker.processor",
    ):
        outcome = processor.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert str(AUDIO_UUID) in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


def test_restart_retranscribes_existing_processing_file(
    audio_directories: WorkerDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    interrupted_transcriber = ScriptedTranscriber([SimulatedProcessCrash()])
    interrupted_processor = _processor(audio_directories, interrupted_transcriber)

    with pytest.raises(SimulatedProcessCrash):
        interrupted_processor.run_once()

    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())

    restarted_transcriber = ScriptedTranscriber([_transcription_result(audio_path)])
    restarted_processor = _processor(audio_directories, restarted_transcriber)
    outcome = restarted_processor.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert restarted_transcriber.audio_paths == [audio_path]
    assert outcome.completed_path.read_bytes() == SYNTHETIC_AUDIO


def _processor(
    directories: WorkerDirectories,
    transcriber: TranscriberProtocol,
) -> AudioProcessor:
    return AudioProcessor(
        directories=directories,
        transcriber=transcriber,
        glpi_client=SuccessfulGlpiClient(),
        audio_url_for=lambda audio_uuid: (
            f"https://audio.example.test/audios/{audio_uuid}"
        ),
        transcription_retry_interval_seconds=0.25,
        glpi_retry_interval_seconds=0.5,
        now=lambda: FIXED_NOW,
    )


def _write_processing_audio(directories: WorkerDirectories) -> Path:
    audio_path = directories.processing / AUDIO_FILENAME
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    return audio_path


def _transcription_result(audio_path: Path) -> TranscriptionResult:
    return TranscriptionResult(
        audio_path=audio_path,
        audio_uuid=AUDIO_UUID,
        transcription=SYNTHETIC_TRANSCRIPTION,
    )
