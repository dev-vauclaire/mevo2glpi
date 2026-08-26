from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_worker_automation.worker import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    ProcessingStage,
    RetryOutcome,
    RunStatus,
    WorkerDirectories,
)

AUDIO_UUID = UUID("88ac95f5-e207-4aa4-a824-2bd4a3269814")
AUDIO_FILENAME = f"{AUDIO_UUID}.wav"
SYNTHETIC_TRANSCRIPTION = "Patient secret qui ne doit jamais sortir du worker"
SYNTHETIC_SECRET = "synthetic-api-key-that-must-not-be-exposed"


def test_worker_directories_are_immutable_and_distinct(tmp_path: Path) -> None:
    directories = WorkerDirectories(
        pending=tmp_path / "pending",
        processing=tmp_path / "processing",
        completed=tmp_path / "completed",
        failed=tmp_path / "failed",
    )

    with pytest.raises(FrozenInstanceError):
        directories.pending = tmp_path / "other"  # type: ignore[misc]

    with pytest.raises(ValueError, match="must be distinct"):
        WorkerDirectories(
            pending=tmp_path / "pending",
            processing=tmp_path / "pending" / ".." / "processing",
            completed=tmp_path / "processing",
            failed=tmp_path / "failed",
        )


def test_outcomes_expose_fixed_discriminating_statuses(tmp_path: Path) -> None:
    processing_path = tmp_path / "processing" / AUDIO_FILENAME
    failure = FailureDetails(
        category="unavailable",
        technical_code="transport_error",
    )
    outcomes = (
        IdleOutcome(wait_seconds=1),
        RetryOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=processing_path,
            stage=ProcessingStage.TICKETING,
            failure=failure,
            retry_after_seconds=2,
        ),
        CompletedOutcome(
            audio_uuid=AUDIO_UUID,
            completed_path=tmp_path / "completed" / AUDIO_FILENAME,
            ticket_id=42,
        ),
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            failed_path=tmp_path / "failed" / AUDIO_FILENAME,
            error_sidecar_path=(tmp_path / "failed" / f"{AUDIO_UUID}.error.json"),
            stage=ProcessingStage.TRANSCRIPTION,
            failure=FailureDetails(
                category="terminal",
                technical_code="remote_job_failed",
            ),
        ),
        BlockedOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=processing_path,
            stage=ProcessingStage.TICKETING,
            failure=FailureDetails(
                category="conflict",
                technical_code="duplicate_external_id",
            ),
        ),
    )

    assert tuple(outcome.status for outcome in outcomes) == tuple(RunStatus)
    assert outcomes[0].wait_seconds == 1.0
    assert outcomes[1].retry_after_seconds == 2.0
    for outcome in outcomes:
        with pytest.raises(FrozenInstanceError):
            outcome.status = RunStatus.IDLE  # type: ignore[misc,union-attr]


@pytest.mark.parametrize("value", [True, "1", 0, -1, float("inf"), float("nan")])
def test_wait_durations_must_be_positive_finite_numbers(value: object) -> None:
    with pytest.raises(TypeError if isinstance(value, bool | str) else ValueError):
        IdleOutcome(wait_seconds=value)  # type: ignore[arg-type]

    with pytest.raises(TypeError if isinstance(value, bool | str) else ValueError):
        RetryOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=Path("processing") / AUDIO_FILENAME,
            stage=ProcessingStage.TRANSCRIPTION,
            failure=FailureDetails(
                category="unavailable",
                technical_code="submission_unavailable",
            ),
            retry_after_seconds=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("category", "technical_code"),
    [
        ("Unavailable", "transport_error"),
        ("unavailable", "raw response body"),
        ("unavailable", SYNTHETIC_TRANSCRIPTION),
        ("unavailable", SYNTHETIC_SECRET),
        ("unavailable", "x" * 65),
    ],
)
def test_failure_details_only_accept_sanitized_identifiers(
    category: str,
    technical_code: str,
) -> None:
    with pytest.raises(ValueError, match="lowercase snake_case"):
        FailureDetails(category=category, technical_code=technical_code)


def test_outcomes_have_no_transcription_channel(tmp_path: Path) -> None:
    outcome = RetryOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=tmp_path / "processing" / AUDIO_FILENAME,
        stage=ProcessingStage.TICKETING,
        failure=FailureDetails(
            category="unavailable",
            technical_code="transport_error",
        ),
        retry_after_seconds=2,
    )

    field_names = {model_field.name for model_field in fields(outcome)}
    serialized_fields = asdict(outcome)
    assert "transcription" not in field_names
    assert "transcription" not in serialized_fields
    assert SYNTHETIC_TRANSCRIPTION not in repr(outcome)
    assert SYNTHETIC_SECRET not in repr(outcome)

    with pytest.raises(TypeError, match="transcription"):
        RetryOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=tmp_path / "processing" / AUDIO_FILENAME,
            stage=ProcessingStage.TICKETING,
            failure=outcome.failure,
            retry_after_seconds=2,
            transcription=SYNTHETIC_TRANSCRIPTION,  # type: ignore[call-arg]
        )


def test_audio_path_must_match_audio_uuid(tmp_path: Path) -> None:
    other_uuid = UUID("123e4567-e89b-42d3-a456-426614174000")

    with pytest.raises(ValueError, match="must identify audio_uuid"):
        CompletedOutcome(
            audio_uuid=AUDIO_UUID,
            completed_path=tmp_path / "completed" / f"{other_uuid}.wav",
            ticket_id=42,
        )


@pytest.mark.parametrize("ticket_id", [True, "42", 0, -1])
def test_completed_outcome_requires_a_positive_ticket_id(ticket_id: object) -> None:
    expected_error = TypeError if isinstance(ticket_id, bool | str) else ValueError

    with pytest.raises(expected_error):
        CompletedOutcome(
            audio_uuid=AUDIO_UUID,
            completed_path=Path("completed") / AUDIO_FILENAME,
            ticket_id=ticket_id,  # type: ignore[arg-type]
        )


def test_failed_outcome_requires_a_matching_sidecar_path(tmp_path: Path) -> None:
    failure = FailureDetails(
        category="terminal",
        technical_code="remote_job_failed",
    )
    failed_path = tmp_path / "failed" / AUDIO_FILENAME

    with pytest.raises(ValueError, match="must end with"):
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            failed_path=failed_path,
            error_sidecar_path=tmp_path / "failed" / "wrong.error.json",
            stage=ProcessingStage.TRANSCRIPTION,
            failure=failure,
        )

    with pytest.raises(ValueError, match="must share a directory"):
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            failed_path=failed_path,
            error_sidecar_path=tmp_path / "other" / f"{AUDIO_UUID}.error.json",
            stage=ProcessingStage.TRANSCRIPTION,
            failure=failure,
        )


def test_completion_transition_error_cannot_be_classified_as_failed(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be blocked"):
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            failed_path=tmp_path / "failed" / AUDIO_FILENAME,
            error_sidecar_path=(tmp_path / "failed" / f"{AUDIO_UUID}.error.json"),
            stage=ProcessingStage.FINALIZATION,
            failure=FailureDetails(
                category="filesystem",
                technical_code="publish_failed",
            ),
        )


def test_finalization_block_requires_the_confirmed_ticket_id(tmp_path: Path) -> None:
    failure = FailureDetails(
        category="filesystem",
        technical_code="completed_publish_failed",
    )

    with pytest.raises(ValueError, match="must expose ticket_id"):
        BlockedOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=tmp_path / "processing" / AUDIO_FILENAME,
            stage=ProcessingStage.FINALIZATION,
            failure=failure,
        )

    outcome = BlockedOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=tmp_path / "processing" / AUDIO_FILENAME,
        stage=ProcessingStage.FINALIZATION,
        failure=failure,
        ticket_id=42,
    )

    assert outcome.ticket_id == 42


def test_block_without_processing_path_exposes_inspection_paths(
    tmp_path: Path,
) -> None:
    failure = FailureDetails(
        category="filesystem",
        technical_code="failed_publish_failed",
    )

    with pytest.raises(ValueError, match="must expose inspection_paths"):
        BlockedOutcome(
            audio_uuid=AUDIO_UUID,
            processing_path=None,
            stage=ProcessingStage.TRANSCRIPTION,
            failure=failure,
        )

    inspection_path = tmp_path / "failed" / AUDIO_FILENAME
    outcome = BlockedOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=None,
        stage=ProcessingStage.TRANSCRIPTION,
        failure=failure,
        inspection_paths=(inspection_path,),
    )

    assert outcome.processing_path is None
    assert outcome.inspection_paths == (inspection_path,)
