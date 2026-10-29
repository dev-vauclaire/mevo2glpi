"""Tests for Transcribe AI stage classification and persistence."""

from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_database import ErrorScope, JobStage
from glpi_ticketing_worker_automation.transcription import (
    TranscriptionBlockedError,
    TranscriptionErrorCategory,
    TranscriptionResult,
    TranscriptionRetryRequired,
    TranscriptionSubmissionCallbackError,
    TranscriptionTerminalError,
)
from glpi_ticketing_worker_automation.worker import (
    CompletedOutcome,
    InterruptedOutcome,
    SystemBlockedOutcome,
)
from glpi_ticketing_worker_automation.worker.stages import TranscriptionSuccess

from ._pipeline_fakes import (
    AUDIO_UUID,
    SYNTHETIC_AUDIO,
    TRANSCRIPTION,
    FakeGlpiClient,
    FakeJob,
    FakeState,
    FakeTranscriber,
    build_worker,
    repository_call_names,
    terminal_repository_calls,
    transcription_result,
    write_audio,
)


def test_system_block_keeps_processing_then_probes_same_job(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(
        state,
        [
            TranscriptionBlockedError(
                category=TranscriptionErrorCategory.AUTHENTICATION,
                code="http_401",
                audio_path=audio_path,
            ),
            transcription_result(audio_path),
        ],
    )
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    first_outcome = worker.run_once()
    second_outcome = worker.run_once()

    assert isinstance(first_outcome, SystemBlockedOutcome)
    assert first_outcome.probe_after_seconds == 60
    assert isinstance(second_outcome, CompletedOutcome)
    assert transcriber.calls == [audio_path, audio_path]
    assert [call[0] for call in state.repository_calls].count("mark_blocked") == 0


def test_invalid_transcription_result_type_is_system_blocked(
    tmp_path: Path,
) -> None:
    state = FakeState()
    write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [object()]),
        FakeGlpiClient(state, []),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.failure.technical_code == "invalid_result"
    assert terminal_repository_calls(state) == []
    assert state.eligible is True


def test_submission_callback_error_latches_processing_without_probe(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(
            state,
            [TranscriptionSubmissionCallbackError(audio_path)],
        ),
        FakeGlpiClient(state, []),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.failure.category == "persistence"
    assert outcome.failure.technical_code == "submission_persistence_failed"
    assert outcome.automatic_probe is False
    assert terminal_repository_calls(state) == []
    assert state.eligible is True


@pytest.mark.parametrize("mismatch", ["uuid", "path"])
def test_transcription_result_identity_mismatch_blocks_the_system(
    tmp_path: Path,
    mismatch: str,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    mismatched_uuid = (
        UUID("223e4567-e89b-42d3-a456-426614174000")
        if mismatch == "uuid"
        else AUDIO_UUID
    )
    mismatched_path = tmp_path / "another.wav" if mismatch == "path" else audio_path
    result = TranscriptionResult(
        audio_path=mismatched_path,
        audio_uuid=mismatched_uuid,
        transcription=TRANSCRIPTION,
    )
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [result]),
        FakeGlpiClient(state, []),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert outcome.failure.technical_code == "result_audio_mismatch"
    assert outcome.automatic_probe is False
    assert terminal_repository_calls(state) == []
    assert state.eligible is True


def test_shutdown_during_transcription_keeps_processing_without_retry(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(
            state,
            [TranscriptionRetryRequired("shutdown_requested", audio_path)],
        ),
        FakeGlpiClient(state, []),
    )

    outcome = worker.run_once()

    assert outcome == InterruptedOutcome(
        audio_uuid=AUDIO_UUID,
        stage=JobStage.TRANSCRIPTION,
    )
    assert "schedule_retry" not in repository_call_names(state)
    assert terminal_repository_calls(state) == []
    assert state.eligible is True
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_provider_identifier_is_not_required_by_the_domain_result(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(transcription_provider_job_id="provider-persisted"))
    audio_path = write_audio(tmp_path)
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        glpi_client,
    )

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert len(glpi_client.calls) == 1
    assert "advance_after_transcription" in repository_call_names(state)


@pytest.mark.parametrize("transcription", ["", "   ", "unsafe\x00content"])
def test_invalid_transcription_content_creates_an_explanatory_ticket(
    tmp_path: Path,
    transcription: str,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    result = TranscriptionResult(
        audio_path=audio_path,
        audio_uuid=AUDIO_UUID,
        transcription=transcription,
    )
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [result]),
        glpi_client,
    )

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    command = glpi_client.calls[0]
    assert "transcription automatique" in command.transcription
    assert "invalid_transcription" in command.transcription
    assert transcription not in command.transcription or not transcription.strip()
    assert terminal_repository_calls(state) == ["mark_completed"]
    assert any(
        call[-1].get("event_type").value == "transcription_failed"
        for call in state.event_calls
    )


@pytest.mark.parametrize(
    "technical_code",
    ["submission_rejected", "remote_job_failed", "http_500"],
)
def test_terminal_provider_failure_creates_an_explanatory_ticket(
    tmp_path: Path,
    technical_code: str,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(
            state,
            [TranscriptionTerminalError(technical_code, audio_path)],
        ),
        glpi_client,
    )

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    command = glpi_client.calls[0]
    assert technical_code in command.transcription
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_in_memory_transcription_is_excluded_from_repr() -> None:
    success = TranscriptionSuccess(transcription=TRANSCRIPTION)

    assert TRANSCRIPTION not in repr(success)
