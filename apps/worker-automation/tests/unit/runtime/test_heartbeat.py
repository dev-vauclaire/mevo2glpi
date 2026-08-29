"""Unit tests for runtime outcome-to-heartbeat translation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

import glpi_ticketing_worker_automation.runtime.heartbeat as heartbeat_module
from glpi_ticketing_database import (
    ErrorScope,
    JobStage,
    WorkerRuntimeState,
    WorkerServiceName,
)
from glpi_ticketing_worker_automation.runtime.heartbeat import (
    RuntimeHeartbeatReporter,
)
from glpi_ticketing_worker_automation.worker import (
    CompletedOutcome,
    FailureDetails,
    IdleOutcome,
    InterruptedOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
INSTANCE_ID = UUID("123e4567-e89b-42d3-a456-426614174001")
NOW = datetime(2026, 8, 28, 14, tzinfo=UTC)


class _MutableMonotonic:
    def __init__(self, current: float = 100.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


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
    monotonic: _MutableMonotonic | None = None,
) -> RuntimeHeartbeatReporter:
    return RuntimeHeartbeatReporter(
        advisory_lock=cast(Any, lock),
        heartbeat_interval_seconds=10,
        instance_id=INSTANCE_ID,
        now=lambda: NOW,
        monotonic=monotonic or _MutableMonotonic(),
    )


def test_retry_degraded_state_remains_sticky_while_worker_is_idle(
    heartbeat_records: list[dict[str, object]],
) -> None:
    lock = _FakeLock()
    monotonic = _MutableMonotonic()
    reporter = _reporter(lock, monotonic)
    retry = RetryOutcome(
        audio_uuid=AUDIO_UUID,
        stage=JobStage.TRANSCRIPTION,
        failure=FailureDetails(
            scope=ErrorScope.SYSTEM,
            category="unavailable",
            technical_code="submission_unavailable",
        ),
        next_attempt_at=NOW,
    )

    reporter.report_outcome(retry)
    reporter.report_outcome(IdleOutcome(wait_seconds=1))

    assert len(heartbeat_records) == 1

    monotonic.advance(10)
    reporter.report_outcome(IdleOutcome(wait_seconds=1))

    assert reporter.state is WorkerRuntimeState.DEGRADED
    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.DEGRADED,
        WorkerRuntimeState.DEGRADED,
    ]
    assert all(
        record["error_category"] == "unavailable"
        and record["error_code"] == "submission_unavailable"
        for record in heartbeat_records
    )
    assert heartbeat_records[1]["current_job_uuid"] is None
    assert all(record["now"] == NOW for record in heartbeat_records)
    assert all(record["instance_id"] == INSTANCE_ID for record in heartbeat_records)
    assert all(
        record["service_name"] is WorkerServiceName.AUTOMATION
        for record in heartbeat_records
    )
    assert all(session.closed for session in lock.sessions)


def test_system_block_remains_sticky_until_real_job_boundary(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MutableMonotonic()
    reporter = _reporter(_FakeLock(), monotonic)
    blocked = SystemBlockedOutcome(
        audio_uuid=AUDIO_UUID,
        stage=JobStage.GLPI_CREATION,
        failure=FailureDetails(
            scope=ErrorScope.SYSTEM,
            category="unknown_outcome",
            technical_code="ticket_creation_unknown",
        ),
        probe_after_seconds=30,
        automatic_probe=False,
    )

    reporter.report_outcome(blocked)
    monotonic.advance(10)
    reporter.report_outcome(IdleOutcome(wait_seconds=1))
    reporter.report_outcome(CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42))

    assert [record["state"] for record in heartbeat_records] == [
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.BLOCKED,
        WorkerRuntimeState.RUNNING,
    ]
    assert heartbeat_records[0]["current_job_uuid"] == AUDIO_UUID
    assert heartbeat_records[1]["current_job_uuid"] is None
    assert heartbeat_records[1]["error_category"] == "unknown_outcome"
    assert heartbeat_records[2]["error_category"] is None
    assert heartbeat_records[2]["error_code"] is None


def test_pulse_is_throttled_until_the_exact_monotonic_interval(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MutableMonotonic()
    reporter = _reporter(_FakeLock(), monotonic)

    reporter.record_state(WorkerRuntimeState.RUNNING)
    monotonic.advance(9.999)

    assert reporter.pulse_if_due() is False
    assert len(heartbeat_records) == 1

    monotonic.advance(0.001)

    assert reporter.pulse_if_due() is True
    assert len(heartbeat_records) == 2
    assert heartbeat_records[-1]["state"] is WorkerRuntimeState.RUNNING


def test_job_uuid_is_published_after_acquisition_and_kept_while_blocked(
    heartbeat_records: list[dict[str, object]],
) -> None:
    monotonic = _MutableMonotonic()
    reporter = _reporter(_FakeLock(), monotonic)
    reporter.record_state(WorkerRuntimeState.RUNNING)

    reporter.start_job(AUDIO_UUID)
    reporter.report_outcome(
        SystemBlockedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
            failure=FailureDetails(
                scope=ErrorScope.SYSTEM,
                category="authentication",
                technical_code="http_401",
            ),
            probe_after_seconds=30,
        )
    )
    monotonic.advance(10)

    assert reporter.pulse_if_due() is True
    assert [record["current_job_uuid"] for record in heartbeat_records] == [
        None,
        AUDIO_UUID,
        AUDIO_UUID,
        AUDIO_UUID,
    ]

    reporter.report_outcome(CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42))

    assert heartbeat_records[-1]["current_job_uuid"] is None
    assert heartbeat_records[-1]["state"] is WorkerRuntimeState.RUNNING


@pytest.mark.parametrize("invalid_interval", [True, 0, -1, float("inf")])
def test_heartbeat_interval_must_be_positive_and_finite(
    invalid_interval: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        RuntimeHeartbeatReporter(
            advisory_lock=cast(Any, _FakeLock()),
            heartbeat_interval_seconds=cast(Any, invalid_interval),
        )


def test_interruption_keeps_current_job_on_running_heartbeat(
    heartbeat_records: list[dict[str, object]],
) -> None:
    reporter = _reporter(_FakeLock())

    reporter.report_outcome(
        InterruptedOutcome(
            audio_uuid=AUDIO_UUID,
            stage=JobStage.TRANSCRIPTION,
        )
    )

    assert heartbeat_records == [
        {
            "service_name": WorkerServiceName.AUTOMATION,
            "instance_id": INSTANCE_ID,
            "state": WorkerRuntimeState.RUNNING,
            "now": NOW,
            "current_job_uuid": AUDIO_UUID,
            "error_category": None,
            "error_code": None,
        }
    ]
