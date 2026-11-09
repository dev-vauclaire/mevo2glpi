"""Validation tests for sanitized, application-facing retention outcomes."""

from datetime import timedelta

import pytest

from mevo2glpi_database import DeletionReason, ErrorScope
from mevo2glpi_glpi import TicketStatus
from mevo2glpi_worker_retention.retention.outcomes import (
    AudioDeletedOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RetentionBlockedOutcome,
    RetentionFailure,
    RetentionOperation,
    RetentionRunStatus,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)

from ._fakes import JOB_UUID, NOW


def test_outcomes_expose_stable_typed_boundaries() -> None:
    system_failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="unavailable",
        technical_code="transport_error",
    )
    job_failure = RetentionFailure(
        scope=ErrorScope.JOB,
        category="conflict",
        technical_code="external_id_mismatch",
    )

    outcomes = [
        IdleOutcome(wait_seconds=2),
        DependencyWaitingOutcome(
            job_uuid=JOB_UUID,
            failure=system_failure,
            condition=DependencyCondition.DEGRADED,
            wait_seconds=2,
        ),
        GlpiCheckRescheduledOutcome(
            job_uuid=JOB_UUID,
            ticket_status=TicketStatus.SOLVED,
            next_check_at=NOW + timedelta(hours=6),
        ),
        RetryScheduledOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=system_failure,
            next_attempt_at=NOW + timedelta(seconds=5),
        ),
        SystemBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=system_failure,
            next_probe_at=NOW + timedelta(seconds=60),
        ),
        RetentionBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=job_failure,
        ),
        AudioDeletedOutcome(
            job_uuid=JOB_UUID,
            reason=DeletionReason.TICKET_CLOSED,
        ),
        RejectedIngestionsPurgedOutcome(
            purged_count=3,
            cutoff=NOW - timedelta(days=90),
            next_scan_at=NOW + timedelta(hours=6),
        ),
    ]

    assert [outcome.status for outcome in outcomes] == [
        RetentionRunStatus.IDLE,
        RetentionRunStatus.DEPENDENCY_WAITING,
        RetentionRunStatus.GLPI_CHECK_RESCHEDULED,
        RetentionRunStatus.RETRY_SCHEDULED,
        RetentionRunStatus.SYSTEM_BLOCKED,
        RetentionRunStatus.RETENTION_BLOCKED,
        RetentionRunStatus.AUDIO_DELETED,
        RetentionRunStatus.REJECTED_INGESTIONS_PURGED,
    ]


@pytest.mark.parametrize(
    ("field_name", "value", "exception"),
    [
        ("category", "HTTP 500: secret response", ValueError),
        ("category", "Unavailable", ValueError),
        ("category", "", ValueError),
        ("technical_code", "upstream/body", ValueError),
        ("technical_code", "x" * 65, ValueError),
        ("technical_code", 500, TypeError),
    ],
)
def test_failure_rejects_raw_or_non_identifier_metadata(
    field_name: str,
    value: object,
    exception: type[Exception],
) -> None:
    values: dict[str, object] = {
        "scope": ErrorScope.SYSTEM,
        "category": "unavailable",
        "technical_code": "transport_error",
    }
    values[field_name] = value

    with pytest.raises(exception):
        RetentionFailure(**values)  # type: ignore[arg-type]


def test_outcome_scope_is_part_of_its_contract() -> None:
    job_failure = RetentionFailure(
        scope=ErrorScope.JOB,
        category="not_found",
        technical_code="ticket_not_found",
    )
    system_failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="unavailable",
        technical_code="transport_error",
    )

    with pytest.raises(ValueError, match="system"):
        RetryScheduledOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=job_failure,
            next_attempt_at=NOW + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="job"):
        RetentionBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=system_failure,
        )


@pytest.mark.parametrize("wait_seconds", [True, 0, -1, float("nan"), float("inf")])
def test_wait_outcomes_require_a_positive_finite_duration(
    wait_seconds: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        IdleOutcome(wait_seconds=wait_seconds)  # type: ignore[arg-type]


def test_failure_repr_contains_no_arbitrary_provider_payload() -> None:
    secret = "temporary-secret"
    transcript = "private transcript"
    failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="protocol",
        technical_code="invalid_payload",
    )

    representation = repr(failure)

    assert secret not in representation
    assert transcript not in representation
    assert "invalid_payload" in representation
