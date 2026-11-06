from dataclasses import fields
from datetime import UTC, datetime
from uuid import UUID

import pytest

from mevo2glpi_worker_automation.worker import (
    BlockedOutcome,
    CompletedOutcome,
    ErrorScope,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    InterruptedOutcome,
    JobStage,
    RetryOutcome,
    RunStatus,
    SystemBlockedOutcome,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


def test_outcomes_expose_persistent_boundaries_without_filesystem_paths() -> None:
    job_failure = FailureDetails(
        scope=ErrorScope.JOB,
        category="terminal",
        technical_code="remote_job_failed",
    )
    outcomes = (
        IdleOutcome(wait_seconds=1),
        InterruptedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
        ),
        RetryOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category="unavailable",
                technical_code="transport_error",
            ),
            next_attempt_at=NOW,
        ),
        CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42),
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=job_failure,
        ),
        BlockedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.GLPI_CREATION,
            failure=FailureDetails(
                scope=ErrorScope.JOB,
                category="conflict",
                technical_code="duplicate_external_id",
            ),
        ),
        SystemBlockedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category="authentication",
                technical_code="http_401",
            ),
            probe_after_seconds=60,
        ),
    )

    assert [outcome.status for outcome in outcomes] == [
        RunStatus.IDLE,
        RunStatus.INTERRUPTED,
        RunStatus.RETRY_SCHEDULED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.SYSTEM_BLOCKED,
    ]
    forbidden_fragments = ("path", "sidecar", "transcription")
    for outcome in outcomes:
        field_names = {field.name for field in fields(outcome)}
        assert not any(
            fragment in field_name
            for fragment in forbidden_fragments
            for field_name in field_names
        )


@pytest.mark.parametrize(
    ("category", "technical_code"),
    [("Unsafe", "safe"), ("safe", "contains-secret-value"), ("", "safe")],
)
def test_failure_details_only_accept_sanitized_identifiers(
    category: str,
    technical_code: str,
) -> None:
    with pytest.raises(ValueError):
        FailureDetails(
            scope=ErrorScope.JOB,
            category=category,
            technical_code=technical_code,
        )


def test_failed_outcome_is_always_job_scoped() -> None:
    with pytest.raises(ValueError, match="scoped to one job"):
        FailedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category="protocol",
                technical_code="contract_changed",
            ),
        )


def test_system_blocked_outcome_exposes_probe_delay_to_runner() -> None:
    outcome = SystemBlockedOutcome(
        audio_uuid=AUDIO_UUID,
        stage=JobStage.TRANSCRIPTION,
        failure=FailureDetails(
            scope=ErrorScope.SYSTEM,
            category="authentication",
            technical_code="http_401",
        ),
        probe_after_seconds=60,
    )

    assert outcome.probe_after_seconds == 60
    assert outcome.automatic_probe is True


def test_latched_system_block_requires_boolean_probe_policy() -> None:
    with pytest.raises(TypeError, match="automatic_probe"):
        SystemBlockedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category="persistence",
                technical_code="submission_persistence_failed",
            ),
            probe_after_seconds=60,
            automatic_probe="false",  # type: ignore[arg-type]
        )


def test_finalization_block_requires_confirmed_ticket_id() -> None:
    with pytest.raises(ValueError, match="must expose ticket_id"):
        BlockedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.FINALIZATION,
            failure=FailureDetails(
                scope=ErrorScope.JOB,
                category="persistence",
                technical_code="finalization_failed",
            ),
        )


@pytest.mark.parametrize("value", [0, -1, True, "42"])
def test_completed_outcome_requires_positive_ticket_id(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=value)  # type: ignore[arg-type]
