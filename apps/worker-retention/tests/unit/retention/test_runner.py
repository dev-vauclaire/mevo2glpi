"""Tests for cooperative retention-worker scheduling."""

from datetime import timedelta

import pytest

from mevo2glpi_database import DeletionReason, ErrorScope
from mevo2glpi_worker_retention.retention.outcomes import (
    AudioDeletedOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RejectedIngestionsReportedOutcome,
    RetentionFailure,
    RetentionOperation,
    RetryScheduledOutcome,
)
from mevo2glpi_worker_retention.retention.runner import run_forever

from ._fakes import JOB_UUID, NOW, ScriptedWorker


class StopLoop(Exception):
    """Stop an otherwise unbounded test loop at an observable boundary."""


def _system_failure() -> RetentionFailure:
    return RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="unavailable",
        technical_code="transport_error",
    )


@pytest.mark.parametrize(
    "outcome",
    [
        IdleOutcome(wait_seconds=2.5),
        DependencyWaitingOutcome(
            job_uuid=JOB_UUID,
            failure=_system_failure(),
            condition=DependencyCondition.DEGRADED,
            wait_seconds=0.001,
        ),
    ],
)
def test_runner_uses_the_wait_exposed_by_idle_or_dependency_gate(
    outcome: IdleOutcome | DependencyWaitingOutcome,
) -> None:
    worker = ScriptedWorker([outcome])
    sleep_calls: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopLoop

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=stop_after_sleep)

    assert worker.calls == 1
    assert sleep_calls == [outcome.wait_seconds]


def test_runner_does_not_sleep_after_completed_or_retry_boundaries() -> None:
    worker = ScriptedWorker(
        [
            RejectedIngestionsReportedOutcome(
                eligible_count=2,
                cutoff=NOW - timedelta(days=90),
                next_scan_at=NOW + timedelta(hours=6),
            ),
            RejectedIngestionsPurgedOutcome(
                purged_count=2,
                cutoff=NOW - timedelta(days=90),
                next_scan_at=NOW + timedelta(hours=6),
            ),
            RetryScheduledOutcome(
                job_uuid=JOB_UUID,
                operation=RetentionOperation.GLPI_CHECK,
                failure=_system_failure(),
                next_attempt_at=NOW + timedelta(seconds=5),
            ),
            AudioDeletedOutcome(
                job_uuid=JOB_UUID,
                reason=DeletionReason.TICKET_CLOSED,
            ),
            IdleOutcome(wait_seconds=10),
        ]
    )
    sleep_calls: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopLoop

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=stop_after_sleep)

    assert worker.calls == 5
    assert sleep_calls == [10]


def test_runner_honors_cooperative_stop_without_polling_worker() -> None:
    worker = ScriptedWorker([IdleOutcome(wait_seconds=10)])

    run_forever(worker, should_stop=lambda: True)

    assert worker.calls == 0
