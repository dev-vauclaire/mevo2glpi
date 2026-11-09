"""Unit tests for retention outcome-to-heartbeat translation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

import mevo2glpi_worker_retention.runtime.heartbeat as heartbeat_module
from mevo2glpi_database import (
    DeletionReason,
    ErrorScope,
    WorkerRuntimeState,
    WorkerServiceName,
)
from mevo2glpi_glpi import TicketStatus
from mevo2glpi_worker_retention.retention import (
    AudioDeletedOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RejectedIngestionsReportedOutcome,
    RetentionBlockedOutcome,
    RetentionFailure,
    RetentionOperation,
    RetryScheduledOutcome,
    StaleCandidateOutcome,
    SystemBlockedOutcome,
)
from mevo2glpi_worker_retention.runtime.heartbeat import (
    RetentionRuntimeHeartbeatReporter,
)

JOB_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
INSTANCE_ID = UUID("123e4567-e89b-42d3-a456-426614174001")
NOW = datetime(2026, 8, 28, 14, tzinfo=UTC)


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __enter__(self) -> _FakeSession:
        self._session.transaction_active = True
        return self._session

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self._session.transaction_active = False


class _FakeSession:
    def __init__(self) -> None:
        self.transaction_active = False
        self.closed = False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def close(self) -> None:
        assert not self.transaction_active
        self.closed = True


class _FakeLock:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def open_session(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


class _MonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def heartbeat_records(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    class RecordingRepository:
        def __init__(self, session: _FakeSession) -> None:
            self._session = session

        def record_heartbeat(self, **kwargs: object) -> None:
            assert self._session.transaction_active
            records.append(kwargs)

    monkeypatch.setattr(
        heartbeat_module,
        "WorkerRuntimeStatusRepository",
        RecordingRepository,
    )
    return records


def _reporter(
    lock: _FakeLock,
    *,
    monotonic: Callable[[], float] | None = None,
    interval_seconds: float = 10.0,
) -> RetentionRuntimeHeartbeatReporter:
    return RetentionRuntimeHeartbeatReporter(
        advisory_lock=cast(Any, lock),
        heartbeat_interval_seconds=interval_seconds,
        instance_id=INSTANCE_ID,
        now=lambda: NOW,
        monotonic=monotonic or _MonotonicClock(),
    )


def _system_failure(
    category: str = "unavailable",
    technical_code: str = "dependency_unavailable",
) -> RetentionFailure:
    return RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category=category,
        technical_code=technical_code,
    )


def _job_failure() -> RetentionFailure:
    return RetentionFailure(
        scope=ErrorScope.JOB,
        category="conflict",
        technical_code="external_id_mismatch",
    )


def test_retry_state_remains_sticky_while_worker_is_idle(
    heartbeat_records: list[dict[str, object]],
) -> None:
    lock = _FakeLock()
    monotonic = _MonotonicClock()
    reporter = _reporter(lock, monotonic=monotonic)
    retry = RetryScheduledOutcome(
        job_uuid=JOB_UUID,
        operation=RetentionOperation.GLPI_CHECK,
        failure=_system_failure(),
        next_attempt_at=NOW + timedelta(minutes=1),
    )

    reporter.report_outcome(retry)
    reporter.report_outcome(IdleOutcome(wait_seconds=60))
    assert len(heartbeat_records) == 1

    monotonic.advance(10)
    reporter.report_outcome(IdleOutcome(wait_seconds=60))

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.DEGRADED,
        WorkerRuntimeState.DEGRADED,
    ]
    assert heartbeat_records[0]["current_job_uuid"] is None
    assert heartbeat_records[1]["current_job_uuid"] is None
    assert all(
        record["error_category"] == "unavailable" for record in heartbeat_records
    )
    assert all(
        record["error_code"] == "dependency_unavailable" for record in heartbeat_records
    )
    assert all(
        record["service_name"] is WorkerServiceName.RETENTION
        for record in heartbeat_records
    )
    assert all(record["instance_id"] == INSTANCE_ID for record in heartbeat_records)
    assert all(record["now"] == NOW for record in heartbeat_records)
    assert all(session.closed for session in lock.sessions)


def test_pulse_is_throttled_just_before_and_due_at_interval(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MonotonicClock(100)
    reporter = _reporter(_FakeLock(), monotonic=monotonic)
    reporter.record_state(WorkerRuntimeState.RUNNING)

    monotonic.advance(9.999)
    assert reporter.pulse_if_due() is False
    monotonic.advance(0.001)
    assert reporter.pulse_if_due() is True

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.RUNNING,
        WorkerRuntimeState.RUNNING,
    ]
    assert heartbeat_records[-1]["current_job_uuid"] is None


def test_selected_job_remains_current_during_pulse_then_outcome_clears_it(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MonotonicClock()
    reporter = _reporter(_FakeLock(), monotonic=monotonic)
    reporter.record_state(WorkerRuntimeState.RUNNING)
    reporter.start_job(JOB_UUID)

    monotonic.advance(10)
    assert reporter.pulse_if_due() is True
    reporter.report_outcome(StaleCandidateOutcome(job_uuid=JOB_UUID))

    assert [record["current_job_uuid"] for record in heartbeat_records] == [
        None,
        JOB_UUID,
        JOB_UUID,
        None,
    ]


def test_selected_job_requires_uuid_and_unknown_outcome_is_rejected() -> None:
    reporter = _reporter(_FakeLock())

    with pytest.raises(TypeError, match="job_uuid"):
        reporter.start_job(cast(Any, "not-a-uuid"))
    with pytest.raises(AssertionError, match="unreachable"):
        reporter.report_outcome(cast(Any, object()))


@pytest.mark.parametrize(
    "outcome",
    [
        RejectedIngestionsReportedOutcome(
            eligible_count=3,
            cutoff=NOW - timedelta(days=90),
            next_scan_at=NOW + timedelta(hours=6),
        ),
        RejectedIngestionsPurgedOutcome(
            purged_count=3,
            cutoff=NOW - timedelta(days=90),
            next_scan_at=NOW + timedelta(hours=6),
        ),
    ],
)
def test_rejected_ingestion_housekeeping_preserves_dependency_health(
    heartbeat_records: list[dict[str, object]],
    outcome: RejectedIngestionsReportedOutcome | RejectedIngestionsPurgedOutcome,
) -> None:
    reporter = _reporter(_FakeLock())
    reporter.report_outcome(
        RetryScheduledOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=_system_failure(),
            next_attempt_at=NOW + timedelta(minutes=1),
        )
    )

    reporter.report_outcome(outcome)

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.DEGRADED,
        WorkerRuntimeState.DEGRADED,
    ]


def test_failures_are_sticky_and_cleared_independently_by_dependency(
    heartbeat_records: list[dict[str, object]],
) -> None:
    reporter = _reporter(_FakeLock())

    reporter.report_outcome(
        RetryScheduledOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=_system_failure(),
            next_attempt_at=NOW + timedelta(minutes=1),
        )
    )
    reporter.report_outcome(
        SystemBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.AUDIO_PURGE,
            failure=_system_failure("storage_unavailable", "purge_forbidden"),
            next_probe_at=NOW + timedelta(minutes=1),
        )
    )
    reporter.report_outcome(
        GlpiCheckRescheduledOutcome(
            job_uuid=JOB_UUID,
            ticket_status=TicketStatus.SOLVED,
            next_check_at=NOW + timedelta(hours=6),
        )
    )
    reporter.report_outcome(
        RetentionBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.AUDIO_PURGE,
            failure=_job_failure(),
        )
    )

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.DEGRADED,
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.RUNNING,
    ]
    assert heartbeat_records[2]["error_category"] == "storage_unavailable"
    assert heartbeat_records[3]["error_category"] is None
    assert heartbeat_records[3]["error_code"] is None


def test_audio_deletion_only_clears_glpi_failure_when_ticket_is_closed(
    heartbeat_records: list[dict[str, object]],
) -> None:
    reporter = _reporter(_FakeLock())
    reporter.report_outcome(
        SystemBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=_system_failure("authentication", "token_rejected"),
            next_probe_at=NOW + timedelta(minutes=1),
        )
    )
    reporter.report_outcome(
        RetryScheduledOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.AUDIO_PURGE,
            failure=_system_failure("storage_unavailable", "purge_unavailable"),
            next_attempt_at=NOW + timedelta(minutes=1),
        )
    )

    reporter.report_outcome(
        AudioDeletedOutcome(
            job_uuid=JOB_UUID,
            reason=DeletionReason.DEADLINE_EXPIRED,
        )
    )
    reporter.report_outcome(
        AudioDeletedOutcome(
            job_uuid=JOB_UUID,
            reason=DeletionReason.TICKET_CLOSED,
        )
    )

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.RUNNING,
    ]
    assert heartbeat_records[2]["error_code"] == "token_rejected"
    assert heartbeat_records[3]["error_code"] is None


@pytest.mark.parametrize(
    ("condition", "expected_state"),
    [
        (DependencyCondition.DEGRADED, WorkerRuntimeState.DEGRADED),
        (DependencyCondition.BLOCKED, WorkerRuntimeState.BLOCKED),
    ],
)
def test_dependency_waiting_restores_persisted_glpi_condition(
    heartbeat_records: list[dict[str, object]],
    condition: DependencyCondition,
    expected_state: WorkerRuntimeState,
) -> None:
    reporter = _reporter(_FakeLock())

    reporter.report_outcome(
        DependencyWaitingOutcome(
            job_uuid=JOB_UUID,
            failure=_system_failure("authentication", "probe_not_due"),
            condition=condition,
            wait_seconds=30,
        )
    )
    reporter.report_outcome(StaleCandidateOutcome(job_uuid=JOB_UUID))

    assert [record["state"] for record in heartbeat_records] == [
        expected_state,
        expected_state,
    ]
    assert heartbeat_records[1]["error_code"] == "probe_not_due"


def test_explicit_running_state_clears_sticky_failures(
    heartbeat_records: list[dict[str, object]],
) -> None:
    reporter = _reporter(_FakeLock())
    reporter.report_outcome(
        SystemBlockedOutcome(
            job_uuid=JOB_UUID,
            operation=RetentionOperation.GLPI_CHECK,
            failure=_system_failure("authentication", "token_rejected"),
            next_probe_at=NOW + timedelta(minutes=1),
        )
    )

    reporter.record_state(WorkerRuntimeState.RUNNING)

    assert heartbeat_records[-1]["state"] is WorkerRuntimeState.RUNNING
    assert heartbeat_records[-1]["error_category"] is None
    assert heartbeat_records[-1]["error_code"] is None
    with pytest.raises(TypeError, match="WorkerRuntimeState"):
        reporter.record_state(cast(Any, "running"))


@pytest.mark.parametrize(
    "invalid_interval",
    [True, 0, -1, float("inf"), float("nan")],
)
def test_heartbeat_interval_must_be_positive_and_finite(
    invalid_interval: object,
) -> None:
    with pytest.raises(TypeError if invalid_interval is True else ValueError):
        _reporter(
            _FakeLock(),
            interval_seconds=cast(Any, invalid_interval),
        )


@pytest.mark.parametrize("invalid_value", [True, float("inf"), float("nan")])
def test_monotonic_clock_must_return_a_finite_number(
    invalid_value: object,
) -> None:
    reporter = _reporter(
        _FakeLock(),
        monotonic=lambda: cast(Any, invalid_value),
    )

    with pytest.raises(TypeError if invalid_value is True else ValueError):
        reporter.record_state(WorkerRuntimeState.RUNNING)


def test_monotonic_clock_must_not_move_backwards(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MonotonicClock(10)
    reporter = _reporter(_FakeLock(), monotonic=monotonic)
    reporter.record_state(WorkerRuntimeState.RUNNING)
    monotonic.value = 9

    with pytest.raises(ValueError, match="must not move backwards"):
        reporter.pulse_if_due()

    assert len(heartbeat_records) == 1
