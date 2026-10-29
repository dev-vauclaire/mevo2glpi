"""Tests for retry, terminal and completion transitions."""

from datetime import timedelta
from pathlib import Path

from glpi_ticketing_database import ErrorScope, JobStage
from glpi_ticketing_glpi import (
    GlpiOperation,
    GlpiTransportError,
)
from glpi_ticketing_worker_automation.transcription import (
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)
from glpi_ticketing_worker_automation.worker import (
    CompletedOutcome,
    RetryOutcome,
)

from ._pipeline_fakes import (
    NOW,
    SYNTHETIC_AUDIO,
    FakeGlpiClient,
    FakeJob,
    FakeState,
    FakeTranscriber,
    build_worker,
    repository_call_names,
    transcription_result,
    write_audio,
)


def test_unavailable_transcription_is_scheduled_with_capped_backoff(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(consecutive_retry_count=2))
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(
        state,
        [TranscriptionRetryRequired("submission_unavailable", audio_path)],
    )
    worker = build_worker(
        tmp_path,
        state,
        transcriber,
        FakeGlpiClient(state, []),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryOutcome)
    assert outcome.next_attempt_at == NOW + timedelta(seconds=20)
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert state.repository_calls[-1][0] == "schedule_retry"


def test_terminal_transcription_creates_fallback_ticket_without_sidecar_or_move(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(
            state,
            [TranscriptionTerminalError("remote_job_failed", audio_path)],
        ),
        FakeGlpiClient(state, [42]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert list(tmp_path.glob("*.json")) == []
    assert state.repository_calls[-1][0] == "mark_completed"
    assert state.retention_calls[-1][-1]["hard_delete_at"] == NOW + timedelta(days=90)


def test_retry_backoff_series_is_shared_across_transcription_and_glpi(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(consecutive_retry_count=1))
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [GlpiTransportError(GlpiOperation.CREATE_TICKET)]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryOutcome)
    assert outcome.stage is JobStage.GLPI_CREATION
    assert outcome.next_attempt_at == NOW + timedelta(seconds=10)


def test_processing_job_with_ticket_finalizes_without_external_call(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(glpi_ticket_id=42), source_received_at=None)
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert transcriber.calls == []
    assert glpi_client.calls == []
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert repository_call_names(state) == [
        "resume_or_claim_next",
        "mark_completed",
    ]
