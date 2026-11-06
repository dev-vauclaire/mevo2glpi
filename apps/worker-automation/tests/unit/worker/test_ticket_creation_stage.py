"""Tests for GLPI ticket stage classification and persistence."""

from pathlib import Path

import pytest

from mevo2glpi_database import ErrorScope, JobStage
from mevo2glpi_glpi import (
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiErrorCategory,
    GlpiHttpError,
    GlpiOperation,
    GlpiProtocolError,
    GlpiTicketRejectedError,
)
from mevo2glpi_worker_automation.worker import (
    BlockedOutcome,
    FailedOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)

from ._pipeline_fakes import (
    SYNTHETIC_AUDIO,
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


def test_duplicate_external_id_is_job_blocked_and_worker_can_continue(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [GlpiDuplicateExternalIdError()]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.scope is ErrorScope.JOB
    assert outcome.failure.technical_code == "duplicate_external_id"
    assert state.repository_calls[-1][0] == "mark_blocked"
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_explicit_glpi_ticket_rejection_is_failed(tmp_path: Path) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [GlpiTicketRejectedError()]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, FailedOutcome)
    assert outcome.stage is JobStage.GLPI_CREATION
    assert outcome.failure.technical_code == "ticket_input_rejected"
    assert terminal_repository_calls(state) == ["mark_failed"]
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_unknown_glpi_creation_outcome_is_scheduled_for_reconciliation(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(
            state,
            [
                GlpiCreateOutcomeUnknownError(
                    category=GlpiErrorCategory.UNAVAILABLE,
                    technical_code="create_response_unusable",
                )
            ],
        ),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryOutcome)
    assert outcome.stage is JobStage.GLPI_CREATION
    assert outcome.failure.technical_code == "create_response_unusable"
    assert state.repository_calls[-1][0] == "schedule_retry"
    assert "record_ticket_confirmed" not in repository_call_names(state)
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_exhausted_glpi_retries_block_the_job_and_release_the_queue(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(consecutive_retry_count=3))
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(
            state,
            [
                GlpiCreateOutcomeUnknownError(
                    category=GlpiErrorCategory.UNAVAILABLE,
                    technical_code="create_response_unusable",
                )
            ],
        ),
        maximum_consecutive_retries=3,
    )

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.stage is JobStage.GLPI_CREATION
    assert outcome.failure.scope is ErrorScope.JOB
    assert outcome.failure.category == "retry_limit"
    assert outcome.failure.technical_code == "glpi_retry_exhausted"
    assert "schedule_retry" not in repository_call_names(state)
    assert terminal_repository_calls(state) == ["mark_blocked"]
    assert state.eligible is False
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


@pytest.mark.parametrize(
    "error",
    [
        GlpiProtocolError(GlpiOperation.FIND_TICKET, "invalid_json"),
        GlpiProtocolError(GlpiOperation.FIND_TICKET, "mismatched_external_id"),
        GlpiHttpError(
            operation=GlpiOperation.AUTHENTICATE,
            category=GlpiErrorCategory.AUTHENTICATION,
            status_code=401,
            retryable=False,
        ),
    ],
)
def test_global_glpi_contract_or_auth_error_is_system_blocked(
    tmp_path: Path,
    error: Exception,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [error]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.stage is JobStage.GLPI_CREATION
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert outcome.automatic_probe is False
    assert terminal_repository_calls(state) == []
    assert state.eligible is True


def test_invalid_ticket_command_is_system_blocked_without_calling_glpi(
    tmp_path: Path,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        glpi_client,
        audio_url_for=lambda _audio_uuid: "not-an-absolute-url",
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.failure.technical_code == "invalid_ticket_command"
    assert glpi_client.calls == []
    assert terminal_repository_calls(state) == []


@pytest.mark.parametrize("ticket_id", [0, -1, True])
def test_invalid_ticket_id_is_system_blocked(
    tmp_path: Path,
    ticket_id: object,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        FakeGlpiClient(state, [ticket_id]),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, SystemBlockedOutcome)
    assert outcome.failure.technical_code == "invalid_ticket_id"
    assert "record_ticket_confirmed" not in repository_call_names(state)
    assert terminal_repository_calls(state) == []
