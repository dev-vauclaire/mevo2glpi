"""Characterization tests for the top-level worker orchestration."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_worker_automation.glpi import (
    GlpiOperation,
    GlpiTransportError,
)
from glpi_ticketing_worker_automation.worker import (
    CompletedOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)

from ._pipeline_fakes import (
    AUDIO_UUID,
    SECRET,
    SYNTHETIC_AUDIO,
    TRANSCRIPTION,
    FailOwnershipGuardOnCall,
    FakeGlpiClient,
    FakeState,
    FakeTranscriber,
    OwnershipLostError,
    build_worker,
    repository_call_names,
    transcription_result,
    write_audio,
)


def test_success_persists_ticket_before_completion_and_keeps_audio(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [transcription_result(audio_path)])
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert outcome == CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42)
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert [call[0] for call in state.repository_calls] == [
        "resume_or_claim_next",
        "record_transcription_succeeded",
        "record_ticket_confirmed",
        "mark_completed",
    ]
    assert state.transaction_outcomes == ["commit"] * 4


def test_ownership_loss_after_transcription_prevents_glpi_and_transitions(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [transcription_result(audio_path)])
    glpi_client = FakeGlpiClient(state, [42])
    guard = FailOwnershipGuardOnCall(failing_call=4)
    worker = build_worker(
        tmp_path,
        state,
        transcriber,
        glpi_client,
        ownership_guard=guard,
    )

    with pytest.raises(OwnershipLostError, match="advisory lock was lost"):
        worker.run_once()

    assert guard.calls == 4
    assert transcriber.calls == [audio_path]
    assert glpi_client.calls == []
    assert repository_call_names(state) == ["resume_or_claim_next"]
    assert state.eligible is True


def test_glpi_retry_does_not_retain_transcription_between_attempts(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(
        state,
        [transcription_result(audio_path), transcription_result(audio_path)],
    )
    glpi_client = FakeGlpiClient(
        state,
        [GlpiTransportError(GlpiOperation.CREATE_TICKET), 42],
    )
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    first_outcome = worker.run_once()
    state.eligible = True  # emulate the repository selecting the retry when due
    second_outcome = worker.run_once()

    assert isinstance(first_outcome, RetryOutcome)
    assert isinstance(second_outcome, CompletedOutcome)
    assert transcriber.calls == [audio_path, audio_path]


def test_worker_logs_uuid_but_never_transcription(
    tmp_path: Path,
    caplog,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [42]),
    )

    with caplog.at_level(logging.INFO):
        worker.run_once()

    assert str(AUDIO_UUID) in caplog.text
    assert TRANSCRIPTION not in caplog.text


def test_worker_never_logs_caught_exception_message_or_secret(
    tmp_path: Path,
    caplog,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)

    def reject_audio_url(_audio_uuid: UUID) -> str:
        raise ValueError(f"invalid configuration contains {SECRET}")

    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, []),
        audio_url_for=reject_audio_url,
    )

    with caplog.at_level(logging.ERROR):
        outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert str(AUDIO_UUID) in caplog.text
    assert TRANSCRIPTION not in caplog.text
    assert SECRET not in caplog.text
    assert "invalid configuration contains" not in caplog.text
