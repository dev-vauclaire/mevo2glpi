"""Characterization tests for the top-level worker orchestration."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import pytest

from mevo2glpi_common import OriginatingPhoneNumberCipher
from mevo2glpi_database import ErrorScope, JobStage, JobStatus
from mevo2glpi_glpi import (
    GlpiOperation,
    GlpiTicketCreateCommand,
    GlpiTransportError,
)
from mevo2glpi_worker_automation.worker import (
    BlockedOutcome,
    CompletedOutcome,
    IdleOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)

from ._pipeline_fakes import (
    AUDIO_UUID,
    SECRET,
    SOURCE_RECEIVED_AT,
    SYNTHETIC_AUDIO,
    TICKET_DEFAULTS,
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
        "get_ingestion_by_uuid",
        "advance_after_transcription",
        "record_ticket_confirmed",
        "mark_completed",
    ]
    assert state.transaction_outcomes == ["commit"] * 4
    assert glpi_client.calls == [
        GlpiTicketCreateCommand(
            audio_uuid=AUDIO_UUID,
            transcription=TRANSCRIPTION,
            audio_url=f"https://audio.test/audios/{AUDIO_UUID}",
            opened_at=SOURCE_RECEIVED_AT,
            defaults=TICKET_DEFAULTS,
        )
    ]


def test_success_adds_originating_number_and_erases_protected_metadata(
    tmp_path: Path,
) -> None:
    phone_number = "0678374363"
    cipher = OriginatingPhoneNumberCipher(b"k" * 32)
    state = FakeState(
        originating_phone_number_ciphertext=cipher.encrypt(
            phone_number,
            AUDIO_UUID,
        )
    )
    audio_path = write_audio(tmp_path)
    glpi_client = FakeGlpiClient(state, [42])
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        glpi_client,
        originating_phone_number_cipher=cipher,
    )

    outcome = worker.run_once()

    assert outcome == CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42)
    assert glpi_client.calls[0].originating_phone_number == phone_number
    assert state.originating_phone_number_ciphertext is None
    assert "clear_originating_phone_number_ciphertext" in repository_call_names(state)


def test_untrusted_originating_number_blocks_before_external_calls(
    tmp_path: Path,
) -> None:
    state = FakeState(originating_phone_number_ciphertext=b"untrusted")
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(
        tmp_path,
        state,
        transcriber,
        glpi_client,
        originating_phone_number_cipher=OriginatingPhoneNumberCipher(b"k" * 32),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.automatic_probe is False
    assert outcome.failure.category == "data_protection"
    assert (
        outcome.failure.technical_code == "originating_phone_number_decryption_failed"
    )
    assert transcriber.calls == []
    assert glpi_client.calls == []
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_job_started_hook_runs_after_acquisition_transaction(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    started_jobs: list[UUID] = []

    def record_started_job(audio_uuid: UUID) -> None:
        assert state.active_transactions == 0
        assert repository_call_names(state) == [
            "resume_or_claim_next",
            "get_ingestion_by_uuid",
        ]
        started_jobs.append(audio_uuid)

    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [42]),
        on_job_started=record_started_job,
    )

    assert isinstance(worker.run_once(), CompletedOutcome)
    assert started_jobs == [AUDIO_UUID]


def test_job_started_hook_is_not_called_when_worker_is_idle(tmp_path: Path) -> None:
    state = FakeState()
    state.eligible = False
    started_jobs: list[UUID] = []
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, []),
        FakeGlpiClient(state, []),
        on_job_started=started_jobs.append,
    )

    assert worker.run_once() == IdleOutcome(wait_seconds=1)
    assert started_jobs == []


def test_job_started_hook_failure_stops_before_external_calls(tmp_path: Path) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [transcription_result(audio_path)])
    glpi_client = FakeGlpiClient(state, [42])
    expected_error = RuntimeError("synthetic heartbeat persistence failure")

    def fail_job_started(_audio_uuid: UUID) -> None:
        assert state.active_transactions == 0
        assert repository_call_names(state) == [
            "resume_or_claim_next",
            "get_ingestion_by_uuid",
        ]
        raise expected_error

    worker = build_worker(
        tmp_path,
        state,
        transcriber,
        glpi_client,
        on_job_started=fail_job_started,
    )

    with pytest.raises(RuntimeError) as captured_error:
        worker.run_once()

    assert captured_error.value is expected_error
    assert transcriber.calls == []
    assert glpi_client.calls == []
    assert state.job.status is JobStatus.PROCESSING
    assert state.transaction_outcomes == ["commit"]


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
    assert repository_call_names(state) == [
        "resume_or_claim_next",
        "get_ingestion_by_uuid",
    ]
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
    assert [call.opened_at for call in glpi_client.calls] == [
        SOURCE_RECEIVED_AT,
        SOURCE_RECEIVED_AT,
    ]


def test_missing_source_date_blocks_before_audio_and_external_calls(
    tmp_path: Path,
) -> None:
    state = FakeState(source_received_at=None)
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is JobStage.TRANSCRIPTION
    assert outcome.failure.scope is ErrorScope.JOB
    assert outcome.failure.category == "data_integrity"
    assert outcome.failure.technical_code == "source_received_at_missing"
    assert transcriber.calls == []
    assert glpi_client.calls == []
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert repository_call_names(state) == [
        "resume_or_claim_next",
        "get_ingestion_by_uuid",
        "mark_blocked",
    ]


def test_worker_logs_uuid_but_never_transcription(
    tmp_path: Path,
    caplog,
) -> None:
    phone_number = "0678374363"
    cipher = OriginatingPhoneNumberCipher(b"k" * 32)
    state = FakeState(
        originating_phone_number_ciphertext=cipher.encrypt(
            phone_number,
            AUDIO_UUID,
        )
    )
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [42]),
        originating_phone_number_cipher=cipher,
    )

    with caplog.at_level(logging.INFO):
        worker.run_once()

    assert str(AUDIO_UUID) in caplog.text
    assert TRANSCRIPTION not in caplog.text
    assert phone_number not in caplog.text
    assert "06 78 37 43 63" not in caplog.text


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
