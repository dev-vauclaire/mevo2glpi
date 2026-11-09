"""Behavioral tests for one pure retention-worker iteration."""

import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest

from mevo2glpi_common.audio_purge import (
    AudioPurgeErrorCode,
    AudioPurgeFilesystemError,
    UnsafeAudioPurgeStateError,
)
from mevo2glpi_common.backoff import ExponentialBackoffPolicy
from mevo2glpi_database import (
    DeletionReason,
    ErrorScope,
    RetentionCandidateKind,
    RetentionStatus,
)
from mevo2glpi_glpi import (
    GlpiErrorCategory,
    GlpiHttpError,
    GlpiOperation,
    GlpiTicketNotFoundError,
    GlpiTransportError,
    TicketStatus,
)
from mevo2glpi_worker_retention.retention.lifecycle import (
    StaleRetentionCandidateError,
)
from mevo2glpi_worker_retention.retention.outcomes import (
    AudioDeletedOutcome,
    CheckRecoveredOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RetentionBlockedOutcome,
    RetentionOperation,
    RetryScheduledOutcome,
    StaleCandidateOutcome,
    SystemBlockedOutcome,
)
from mevo2glpi_worker_retention.retention.policies import RetentionCheckPolicy
from mevo2glpi_worker_retention.retention.ports import (
    CheckRecoveryWork,
    DatabasePurgeWork,
)
from mevo2glpi_worker_retention.retention.service import RetentionWorker
from mevo2glpi_worker_retention.retention.stages import GlpiCheckStage

from ._fakes import (
    JOB_UUID,
    NOW,
    STORAGE_KEY,
    FakeAudioPurger,
    FakeLifecycle,
    FakeTicketReader,
    ScriptedClock,
    glpi_work,
    purge_work,
    retention_candidate,
    retention_snapshot,
    ticket_snapshot,
)

POLL_SECONDS = 60.0
PROBE_SECONDS = 45.0


class FakeRejectedIngestionRetention:
    def __init__(self, outcome: object | None) -> None:
        self.outcome = outcome
        self.calls: list[object] = []

    def run_if_due(self, now: object) -> object | None:
        self.calls.append(now)
        return self.outcome


def _worker(
    lifecycle: FakeLifecycle,
    *,
    glpi_response: object | BaseException | None = None,
    purge_result: object | BaseException | None = None,
    clock: ScriptedClock,
    on_job_started: Callable[[UUID], None] = lambda _job_uuid: None,
    rejected_ingestion_retention: FakeRejectedIngestionRetention | None = None,
) -> tuple[RetentionWorker, FakeAudioPurger, FakeTicketReader]:
    reader = FakeTicketReader(
        glpi_response
        if isinstance(glpi_response, BaseException)
        else glpi_response or ticket_snapshot()
    )
    purger = FakeAudioPurger(purge_result or object())
    return (
        RetentionWorker(
            lifecycle=lifecycle,
            glpi_check_stage=GlpiCheckStage(reader),
            audio_purger=purger,
            retry_policy=ExponentialBackoffPolicy(
                initial_seconds=5,
                maximum_seconds=300,
            ),
            check_policy=RetentionCheckPolicy(check_interval=timedelta(hours=6)),
            rejected_ingestion_retention=rejected_ingestion_retention,
            poll_interval_seconds=POLL_SECONDS,
            system_probe_interval_seconds=PROBE_SECONDS,
            now=clock,
            on_job_started=on_job_started,
        ),
        purger,
        reader,
    )


def _call_names(lifecycle: FakeLifecycle) -> list[object]:
    return [call[0] for call in lifecycle.calls]


def test_run_once_returns_idle_when_no_operation_or_probe_exists() -> None:
    lifecycle = FakeLifecycle(candidate=None)
    worker, purger, reader = _worker(lifecycle, clock=ScriptedClock(NOW))

    outcome = worker.run_once()

    assert outcome == IdleOutcome(wait_seconds=POLL_SECONDS)
    assert _call_names(lifecycle) == [
        "find_next_candidate",
        "get_glpi_probe_gate",
    ]
    assert purger.storage_keys == []
    assert reader.ticket_ids == []


def test_rejected_ingestion_scan_runs_before_waiting_for_glpi_probe() -> None:
    lifecycle = FakeLifecycle(
        candidate=None,
        gate=retention_snapshot(
            status=RetentionStatus.RETRY_SCHEDULED,
            next_check_at=NOW + timedelta(minutes=1),
            last_error_category="unavailable",
            last_error_code="transport_error",
        ),
    )
    expected = RejectedIngestionsPurgedOutcome(
        purged_count=3,
        cutoff=NOW - timedelta(days=90),
        next_scan_at=NOW + timedelta(hours=6),
    )
    rejected_retention = FakeRejectedIngestionRetention(expected)
    worker, purger, reader = _worker(
        lifecycle,
        clock=ScriptedClock(NOW),
        rejected_ingestion_retention=rejected_retention,
    )

    outcome = worker.run_once()

    assert outcome is expected
    assert rejected_retention.calls == [NOW]
    assert _call_names(lifecycle) == ["find_next_candidate"]
    assert purger.storage_keys == []
    assert reader.ticket_ids == []


def test_audio_retention_candidate_has_priority_over_rejected_ingestion_scan() -> None:
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK),
        prepared_work=CheckRecoveryWork(job_uuid=JOB_UUID),
    )
    rejected_retention = FakeRejectedIngestionRetention(
        RejectedIngestionsPurgedOutcome(
            purged_count=3,
            cutoff=NOW - timedelta(days=90),
            next_scan_at=NOW + timedelta(hours=6),
        )
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        clock=ScriptedClock(NOW),
        rejected_ingestion_retention=rejected_retention,
    )

    outcome = worker.run_once()

    assert outcome == CheckRecoveredOutcome(job_uuid=JOB_UUID)
    assert rejected_retention.calls == []


def test_run_once_recovers_an_interrupted_check_without_external_call() -> None:
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK),
        prepared_work=CheckRecoveryWork(job_uuid=JOB_UUID),
    )
    worker, purger, reader = _worker(lifecycle, clock=ScriptedClock(NOW))

    outcome = worker.run_once()

    assert outcome == CheckRecoveredOutcome(job_uuid=JOB_UUID)
    assert purger.storage_keys == []
    assert reader.ticket_ids == []


def test_selected_candidate_is_reported_before_it_is_prepared() -> None:
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK),
        prepared_work=CheckRecoveryWork(job_uuid=JOB_UUID),
    )

    def record_started_job(job_uuid: UUID) -> None:
        lifecycle.calls.append(("job_started", job_uuid))

    worker, _purger, _reader = _worker(
        lifecycle,
        clock=ScriptedClock(NOW),
        on_job_started=record_started_job,
    )

    worker.run_once()

    assert _call_names(lifecycle)[:3] == [
        "find_next_candidate",
        "job_started",
        "prepare_candidate",
    ]
    assert lifecycle.calls[1][1] == JOB_UUID


def test_closed_ticket_starts_and_completes_ticket_closed_purge() -> None:
    work = glpi_work()
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=work,
    )
    worker, purger, _reader = _worker(
        lifecycle,
        glpi_response=ticket_snapshot(status=TicketStatus.CLOSED),
        clock=ScriptedClock(
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
        ),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=JOB_UUID,
        reason=DeletionReason.TICKET_CLOSED,
    )
    assert _call_names(lifecycle) == [
        "find_next_candidate",
        "prepare_candidate",
        "start_deletion_after_check",
        "purge_database_aggregate",
    ]
    deletion_call = lifecycle.calls[2]
    assert deletion_call[2] is DeletionReason.TICKET_CLOSED
    assert deletion_call[3] is TicketStatus.CLOSED
    assert purger.storage_keys == [STORAGE_KEY]


@pytest.mark.parametrize("status", [TicketStatus.ASSIGNED, TicketStatus.SOLVED])
def test_open_or_solved_ticket_keeps_audio_and_schedules_next_check(
    status: TicketStatus,
) -> None:
    decision_time = NOW + timedelta(seconds=1)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(),
    )
    worker, purger, _reader = _worker(
        lifecycle,
        glpi_response=ticket_snapshot(status=status),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert outcome == GlpiCheckRescheduledOutcome(
        job_uuid=JOB_UUID,
        ticket_status=status,
        next_check_at=decision_time + timedelta(hours=6),
    )
    assert _call_names(lifecycle) == [
        "find_next_candidate",
        "prepare_candidate",
        "reschedule_glpi_check",
    ]
    assert purger.storage_keys == []


@pytest.mark.parametrize(
    ("glpi_response", "expected_observed_status"),
    [
        (ticket_snapshot(status=TicketStatus.ASSIGNED), TicketStatus.ASSIGNED),
        (ticket_snapshot(status=TicketStatus.CLOSED), TicketStatus.CLOSED),
        (GlpiTransportError(GlpiOperation.GET_TICKET), None),
    ],
)
def test_deadline_crossed_during_glpi_call_takes_priority_over_result(
    glpi_response: object,
    expected_observed_status: TicketStatus | None,
) -> None:
    hard_delete_at = NOW + timedelta(seconds=10)
    decision_time = hard_delete_at + timedelta(milliseconds=1)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(hard_delete_at=hard_delete_at),
    )
    worker, purger, _reader = _worker(
        lifecycle,
        glpi_response=glpi_response,
        clock=ScriptedClock(NOW, decision_time, decision_time),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=JOB_UUID,
        reason=DeletionReason.DEADLINE_EXPIRED,
    )
    assert _call_names(lifecycle) == [
        "find_next_candidate",
        "prepare_candidate",
        "start_deletion_after_check",
        "purge_database_aggregate",
    ]
    assert lifecycle.calls[2][2] is DeletionReason.DEADLINE_EXPIRED
    assert lifecycle.calls[2][3] is expected_observed_status
    assert purger.storage_keys == [STORAGE_KEY]


@pytest.mark.parametrize(
    "glpi_response",
    [
        GlpiTicketNotFoundError(),
        ticket_snapshot(external_id="323e4567-e89b-42d3-a456-426614174000"),
    ],
)
def test_missing_or_mismatched_ticket_blocks_only_its_retention(
    glpi_response: object,
) -> None:
    decision_time = NOW + timedelta(seconds=1)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(),
    )
    worker, purger, _reader = _worker(
        lifecycle,
        glpi_response=glpi_response,
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetentionBlockedOutcome)
    assert outcome.job_uuid == JOB_UUID
    assert outcome.operation is RetentionOperation.GLPI_CHECK
    assert outcome.failure.scope is ErrorScope.JOB
    assert _call_names(lifecycle)[-1] == "mark_blocked"
    assert purger.storage_keys == []


def test_unavailable_glpi_schedules_a_capped_retry() -> None:
    decision_time = NOW + timedelta(seconds=1)
    work = glpi_work(consecutive_retry_count=2)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=work,
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        glpi_response=GlpiTransportError(GlpiOperation.GET_TICKET),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert outcome == RetryScheduledOutcome(
        job_uuid=JOB_UUID,
        operation=RetentionOperation.GLPI_CHECK,
        failure=outcome.failure,
        next_attempt_at=decision_time + timedelta(seconds=20),
    )
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert outcome.failure.category == "unavailable"
    assert _call_names(lifecycle)[-1] == "schedule_retry"


def test_glpi_retry_is_never_scheduled_after_hard_deadline() -> None:
    decision_time = NOW + timedelta(seconds=1)
    hard_delete_at = decision_time + timedelta(seconds=2)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(hard_delete_at=hard_delete_at),
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        glpi_response=GlpiTransportError(GlpiOperation.GET_TICKET),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.next_attempt_at == hard_delete_at
    assert lifecycle.calls[-1][3] == hard_delete_at


def test_non_retryable_system_error_persists_one_future_probe() -> None:
    decision_time = NOW + timedelta(seconds=1)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(),
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        glpi_response=GlpiHttpError(
            operation=GlpiOperation.GET_TICKET,
            category=GlpiErrorCategory.AUTHENTICATION,
            status_code=401,
            retryable=False,
        ),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert outcome == SystemBlockedOutcome(
        job_uuid=JOB_UUID,
        operation=RetentionOperation.GLPI_CHECK,
        failure=outcome.failure,
        next_probe_at=decision_time + timedelta(seconds=PROBE_SECONDS),
    )
    assert outcome.failure.category == "authentication"
    assert outcome.failure.technical_code == "http_401"
    assert _call_names(lifecycle)[-1] == "schedule_retry"


@pytest.mark.parametrize(
    ("category", "next_check_delta", "condition", "expected_wait"),
    [
        ("unavailable", timedelta(seconds=30), DependencyCondition.DEGRADED, 30),
        ("authentication", timedelta(minutes=2), DependencyCondition.BLOCKED, 60),
        ("unavailable", timedelta(seconds=-1), DependencyCondition.DEGRADED, 0.001),
    ],
)
def test_persisted_probe_gate_waits_without_fanning_out_or_busy_loop(
    category: str,
    next_check_delta: timedelta,
    condition: DependencyCondition,
    expected_wait: float,
) -> None:
    gate = retention_snapshot(
        status=RetentionStatus.RETRY_SCHEDULED,
        next_check_at=NOW + next_check_delta,
        consecutive_retry_count=1,
        last_error_category=category,
        last_error_code="transport_error",
    )
    lifecycle = FakeLifecycle(candidate=None, gate=gate)
    worker, purger, reader = _worker(lifecycle, clock=ScriptedClock(NOW))

    outcome = worker.run_once()

    assert isinstance(outcome, DependencyWaitingOutcome)
    assert outcome.job_uuid == JOB_UUID
    assert outcome.condition is condition
    assert outcome.wait_seconds == expected_wait
    assert reader.ticket_ids == []
    assert purger.storage_keys == []


def test_successful_prepared_purge_removes_audio_and_database_aggregate() -> None:
    work = purge_work(reason=DeletionReason.FAILED_TTL)
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(
            RetentionCandidateKind.RESUME_DELETION,
        ),
        prepared_work=work,
    )
    worker, purger, reader = _worker(
        lifecycle,
        clock=ScriptedClock(NOW),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=JOB_UUID,
        reason=DeletionReason.FAILED_TTL,
    )
    assert _call_names(lifecycle)[-1] == "purge_database_aggregate"
    assert purger.storage_keys == [STORAGE_KEY]
    assert reader.ticket_ids == []


def test_legacy_deleted_aggregate_is_purged_without_touching_storage() -> None:
    work = DatabasePurgeWork(
        job_uuid=JOB_UUID,
        reason=DeletionReason.FAILED_TTL,
    )
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(
            RetentionCandidateKind.PURGE_DELETED_AGGREGATE,
        ),
        prepared_work=work,
    )
    worker, purger, reader = _worker(
        lifecycle,
        clock=ScriptedClock(NOW),
    )

    outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=JOB_UUID,
        reason=DeletionReason.FAILED_TTL,
    )
    assert _call_names(lifecycle)[-1] == "purge_database_aggregate"
    assert purger.storage_keys == []
    assert reader.ticket_ids == []


def test_retryable_purge_failure_preserves_deletion_context_and_schedules_retry() -> (
    None
):
    decision_time = NOW + timedelta(seconds=1)
    work = purge_work(
        reason=DeletionReason.BLOCKED_TTL,
        consecutive_retry_count=1,
    )
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(
            RetentionCandidateKind.RESUME_DELETION,
        ),
        prepared_work=work,
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        purge_result=AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.UNLINK_FAILED,
            retryable=True,
            errno=5,
        ),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetryScheduledOutcome)
    assert outcome.operation is RetentionOperation.AUDIO_PURGE
    assert outcome.failure.scope is ErrorScope.SYSTEM
    assert outcome.failure.category == "storage_unavailable"
    assert outcome.failure.technical_code == "purge_unlink_failed"
    assert outcome.next_attempt_at == decision_time + timedelta(seconds=10)
    assert lifecycle.calls[-1][1] is work


def test_unsafe_purge_state_blocks_only_the_retention() -> None:
    decision_time = NOW + timedelta(seconds=1)
    work = purge_work()
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(
            RetentionCandidateKind.RESUME_DELETION,
        ),
        prepared_work=work,
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        purge_result=UnsafeAudioPurgeStateError(
            code=AudioPurgeErrorCode.AMBIGUOUS_STATE,
            retryable=False,
        ),
        clock=ScriptedClock(NOW, decision_time),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, RetentionBlockedOutcome)
    assert outcome.operation is RetentionOperation.AUDIO_PURGE
    assert outcome.failure.scope is ErrorScope.JOB
    assert outcome.failure.category == "storage"
    assert outcome.failure.technical_code == "purge_ambiguous_state"
    assert _call_names(lifecycle)[-1] == "mark_blocked"


def test_stale_candidate_never_reaches_glpi_or_storage() -> None:
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=StaleRetentionCandidateError(JOB_UUID),
    )
    worker, purger, reader = _worker(lifecycle, clock=ScriptedClock(NOW))

    outcome = worker.run_once()

    assert outcome == StaleCandidateOutcome(job_uuid=JOB_UUID)
    assert reader.ticket_ids == []
    assert purger.storage_keys == []


def test_failure_logs_contain_uuid_and_only_sanitized_error_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "temporary-secret-must-not-be-logged"
    transcript = "confidential transcript must not be logged"
    raw_response = '{"password":"raw-upstream-response"}'
    provider_error = GlpiTransportError(GlpiOperation.GET_TICKET)
    provider_error.api_key = secret
    provider_error.transcript = transcript
    provider_error.raw_response = raw_response
    lifecycle = FakeLifecycle(
        candidate=retention_candidate(),
        prepared_work=glpi_work(),
    )
    worker, _purger, _reader = _worker(
        lifecycle,
        glpi_response=provider_error,
        clock=ScriptedClock(NOW, NOW + timedelta(seconds=1)),
    )

    with caplog.at_level(
        logging.INFO,
        logger="mevo2glpi_worker_retention.retention.service",
    ):
        worker.run_once()

    rendered = caplog.text
    assert str(JOB_UUID) in rendered
    assert "category=unavailable" in rendered
    assert "technical_code=transport_error" in rendered
    assert secret not in rendered
    assert transcript not in rendered
    assert raw_response not in rendered
